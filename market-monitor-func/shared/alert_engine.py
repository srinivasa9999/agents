"""Threshold / crossing logic. Pure-ish functions that take current market
data + config + a StateStore and return a list of Alert objects for
whatever tripped *this run*, applying hysteresis (for price levels) and a
tripped/re-arm state machine (for VIX and position checks) so we alert on
new crossings, not on every tick while a condition remains true.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import Alert
from .state_store import StateStore

logger = logging.getLogger("market_monitor.alert_engine")
IST = ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_level_crossings(
    symbol_name: str,
    current_price: float,
    levels: list[dict],
    hysteresis_points: float,
    cooldown_minutes: int,
    state_store: StateStore,
) -> list[Alert]:
    alerts = []
    half_band = hysteresis_points / 2

    for level in levels:
        label = level["label"]
        price = float(level["price"])
        condition_key = f"level:{symbol_name}:{label}"
        state = state_store.get_alert_state(condition_key)
        prev_side = state.get("side") if state else None

        if prev_side is None:
            side_now = "above" if current_price >= price else "below"
            state_store.set_alert_state(condition_key, side=side_now)
            continue

        if prev_side == "below":
            side_now = "above" if current_price >= price + half_band else "below"
        else:
            side_now = "below" if current_price <= price - half_band else "above"

        if side_now == prev_side:
            continue

        direction = "crossed above" if side_now == "above" else "crossed below"
        if state_store.cooldown_elapsed(condition_key, cooldown_minutes):
            alerts.append(Alert(
                condition_key=condition_key,
                category="level_cross",
                title=f"{symbol_name} {direction} {label}",
                current_value=f"{current_price:,.2f}",
                threshold=f"{price:,.2f}",
                detail=f"{symbol_name} {direction} level '{label}' ({price:,.2f})",
                timestamp=datetime.now(tz=IST),
                on_delivered=lambda ck=condition_key, s=side_now: state_store.record_alert_sent(ck, side=s),
            ))
        else:
            logger.info("Level cross for %s suppressed by cooldown", condition_key)
            state_store.set_alert_state(condition_key, side=side_now)

    return alerts


def check_vix_move(
    current_vix: float,
    threshold_pct: float,
    cooldown_minutes: int,
    state_store: StateStore,
) -> list[Alert]:
    condition_key = "vix_intraday_move"
    today = datetime.now(tz=IST).strftime("%Y-%m-%d")
    state = state_store.get_alert_state(condition_key)

    if not state or state.get("reference_date") != today:
        state_store.set_alert_state(
            condition_key, reference_date=today, reference_value=current_vix, tripped=False,
        )
        return []

    reference_value = float(state["reference_value"])
    tripped_prev = bool(state.get("tripped", False))
    if reference_value == 0:
        return []
    pct_move = (current_vix - reference_value) / reference_value * 100

    alerts = []
    if abs(pct_move) >= threshold_pct and not tripped_prev:
        if state_store.cooldown_elapsed(condition_key, cooldown_minutes):
            direction = "up" if pct_move > 0 else "down"
            alerts.append(Alert(
                condition_key=condition_key,
                category="vix",
                title=f"India VIX moved {direction} {abs(pct_move):.1f}% intraday",
                current_value=f"{current_vix:.2f}",
                threshold=f"{threshold_pct:.1f}% (from day open {reference_value:.2f})",
                detail=f"India VIX {direction} {abs(pct_move):.1f}% from today's first reading ({reference_value:.2f} -> {current_vix:.2f})",
                timestamp=datetime.now(tz=IST),
                on_delivered=lambda: state_store.record_alert_sent(
                    condition_key, reference_date=today, reference_value=reference_value, tripped=True,
                ),
            ))
        else:
            state_store.set_alert_state(condition_key, reference_date=today, reference_value=reference_value, tripped=True)
    elif abs(pct_move) < threshold_pct and tripped_prev:
        # re-arm: allow a fresh alert if it breaches again later today
        state_store.set_alert_state(condition_key, reference_date=today, reference_value=reference_value, tripped=False)

    return alerts


def check_positions(
    positions: list[dict],
    positions_config: dict,
    cooldown_minutes: int,
    state_store: StateStore,
) -> list[Alert]:
    alerts = []

    for pos in positions:
        symbol = pos.get("tradingsymbol", "UNKNOWN")
        quantity = pos.get("quantity", 0)
        average_price = pos.get("average_price", 0)
        last_price = pos.get("last_price", 0)
        pnl = pos.get("pnl", 0)
        position_cost = abs(average_price * quantity)

        if positions_config.get("pnl_absolute_enabled"):
            threshold = float(positions_config.get("pnl_absolute_threshold_rupees", 0))
            alerts.extend(_check_magnitude(
                condition_key=f"position_pnl_abs:{symbol}",
                category="position_pnl",
                title=f"{symbol} unrealized P&L crossed ₹{threshold:,.0f}",
                current_value=f"₹{pnl:,.0f}",
                threshold=f"₹{threshold:,.0f}",
                detail=f"{symbol} unrealized P&L is ₹{pnl:,.0f} (qty {quantity}, avg {average_price}, LTP {last_price})",
                value=pnl,
                threshold_value=threshold,
                cooldown_minutes=cooldown_minutes,
                state_store=state_store,
            ))

        if positions_config.get("pnl_percentage_enabled") and position_cost > 0:
            threshold = float(positions_config.get("pnl_percentage_threshold", 0))
            pct = pnl / position_cost * 100
            alerts.extend(_check_magnitude(
                condition_key=f"position_pnl_pct:{symbol}",
                category="position_pnl",
                title=f"{symbol} unrealized P&L crossed {threshold:.0f}% of position cost",
                current_value=f"{pct:.1f}%",
                threshold=f"{threshold:.0f}%",
                detail=f"{symbol} unrealized P&L is {pct:.1f}% of position cost (₹{pnl:,.0f} on ₹{position_cost:,.0f})",
                value=pct,
                threshold_value=threshold,
                cooldown_minutes=cooldown_minutes,
                state_store=state_store,
            ))

        if positions_config.get("ltp_move_enabled") and average_price:
            threshold = float(positions_config.get("ltp_move_pct_threshold", 0))
            pct = (last_price - average_price) / average_price * 100
            alerts.extend(_check_magnitude(
                condition_key=f"position_ltp_move:{symbol}",
                category="position_ltp",
                title=f"{symbol} LTP moved {threshold:.0f}% from entry",
                current_value=f"LTP {last_price} ({pct:+.1f}% vs avg {average_price})",
                threshold=f"{threshold:.0f}%",
                detail=f"{symbol} LTP {last_price} is {pct:+.1f}% away from your average entry price {average_price}",
                value=pct,
                threshold_value=threshold,
                cooldown_minutes=cooldown_minutes,
                state_store=state_store,
            ))

    return alerts


def _check_magnitude(
    condition_key: str,
    category: str,
    title: str,
    current_value: str,
    threshold: str,
    detail: str,
    value: float,
    threshold_value: float,
    cooldown_minutes: int,
    state_store: StateStore,
) -> list[Alert]:
    """Generic |value| >= threshold state machine with trip/re-arm, used for
    VIX-style magnitude checks on positions."""
    if threshold_value <= 0:
        return []

    state = state_store.get_alert_state(condition_key)
    tripped_prev = bool(state.get("tripped", False)) if state else False
    breached = abs(value) >= threshold_value

    if breached and not tripped_prev:
        if state_store.cooldown_elapsed(condition_key, cooldown_minutes):
            return [Alert(
                condition_key=condition_key,
                category=category,
                title=title,
                current_value=current_value,
                threshold=threshold,
                detail=detail,
                timestamp=datetime.now(tz=IST),
                on_delivered=lambda: state_store.record_alert_sent(condition_key, tripped=True),
            )]
        state_store.set_alert_state(condition_key, tripped=True)
    elif not breached and tripped_prev:
        state_store.set_alert_state(condition_key, tripped=False)

    return []
