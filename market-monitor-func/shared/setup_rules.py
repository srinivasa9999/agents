"""Evaluates a candle series against docs/TRADING_PLAN.md's Setup A / Setup B
rules. Pure functions, no I/O and no Kite/Azure imports - scripts/analyze_stock.py
fetches candles and passes them in.

This module reports what the rules say about the *current* moment (the last
candle in the series) - it does not scan history looking for setups that
already came and went, and it does not place or size an order on your
behalf. It is deliberately a checklist generator, not a decision-maker: see
the "Safety" section of the repo README for why this app stays read-only
and human-in-the-loop by design.

Every rule constant below has a comment pointing at the TRADING_PLAN.md
section it comes from, so a future change to the plan has an obvious place
to land here too.
"""
from .indicators import ema, rsi, vwap

# --- constants, all traceable to docs/TRADING_PLAN.md -----------------------
RISK_PCT_OF_CAPITAL = 1.0          # rule 3.1
MIN_REWARD_RISK = 1.5              # rule 3.5
DAILY_LOSS_LIMIT_PCT = 2.0         # rule 3.4
WEEKLY_LOSS_LIMIT_PCT = 5.0        # rule 3.6

MOVE_FILTER_PCT = 3.0              # Setup A/B filter: "moved >= 3-4%"; using the
                                    # conservative (lower) bound of the stated range
RULE7_NO_TRADE_MINUTES = 15        # no trading 09:15-09:30

EMA_FAST_PERIOD = 9                # Setup A trend filter
EMA_SLOW_PERIOD = 20
RSI_PERIOD = 14

SETUP_A_RSI_CHASE_LONG = 75        # Setup A: skip a long if RSI already >= this
SETUP_A_RSI_CHASE_SHORT = 25       # Setup A: skip a short if RSI already <= this
SETUP_B_RSI_OVERBOUGHT = 70        # Setup B: fading strength needs RSI >= this
SETUP_B_RSI_OVERSOLD = 30          # Setup B: fading weakness needs RSI <= this

SETUP_A_TARGET_R = 2.0             # Setup A target, rule 4A
SETUP_B_SCALE1_R = 1.0             # Setup B first scale-out, rule 4B
SETUP_B_TARGET_R = 2.0             # Setup B remainder target, rule 4B

PULLBACK_LOOKBACK_CANDLES = 5      # see module docstring in analyze_stock.py:
                                    # a documented proxy for "the pullback high/low",
                                    # not real swing-point detection


def position_size(capital: float, entry: float, stop: float, risk_pct: float = RISK_PCT_OF_CAPITAL) -> int:
    """qty = (capital * risk%) / |entry - stop|, rounded down - rule 3.1.
    Never rounds up: rounding up would silently risk more than risk_pct."""
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        raise ValueError("entry and stop must differ")
    return int((capital * risk_pct / 100) // risk_per_share)


def reward_risk_ratio(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        raise ValueError("entry and stop must differ")
    return reward / risk


def _minutes_since_open(candle: dict, session_open_minutes: int = 9 * 60 + 15) -> int:
    """candle['time'] must be a datetime; returns minutes since 09:15 IST.
    Callers pass IST-local datetimes (this doesn't do timezone conversion)."""
    t = candle["time"]
    return (t.hour * 60 + t.minute) - session_open_minutes


def _day_extreme_move_pct(candles: list[dict], prev_close: float) -> float:
    """Largest absolute % move from prev_close across every candle's
    high/low (not just closes) - catches an intrabar spike a close-only
    check would miss, per the Hyundai/Bajaj Finance backtests."""
    extremes = [abs(c["high"] - prev_close) for c in candles] + [abs(c["low"] - prev_close) for c in candles]
    return max(extremes) / prev_close * 100 if extremes else 0.0


def evaluate_setup_a(candles: list[dict], prev_close: float) -> dict:
    """candles: chronological list of {"time": datetime, "open", "high",
    "low", "close", "volume"} for the current session, from market open.
    Returns a checklist dict - see the `checks` key for the pass/fail on
    each rule, and `eligible` (all checks passed on the *last* candle) plus
    suggested entry/stop/target if so."""
    warm_up = max(EMA_SLOW_PERIOD, RSI_PERIOD + 1)
    if len(candles) < warm_up:
        return {"eligible": False, "reason": f"Need >= {warm_up} candles for EMA/RSI warm-up, have {len(candles)}."}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    ema_fast = ema(closes, EMA_FAST_PERIOD)
    ema_slow = ema(closes, EMA_SLOW_PERIOD)
    rsi_series = rsi(closes, RSI_PERIOD)
    vwap_series = vwap(highs, lows, closes, volumes)

    i = len(candles) - 1
    now, prev = candles[i], candles[i - 1]
    direction = "long" if now["close"] >= prev_close else "short"

    checks = {}

    checks["rule_7_clear"] = _minutes_since_open(now) >= RULE7_NO_TRADE_MINUTES

    day_move_pct = _day_extreme_move_pct(candles, prev_close)
    checks["moved_enough"] = day_move_pct >= MOVE_FILTER_PCT

    v_now, v_prev = vwap_series[i], vwap_series[i - 1]
    if direction == "long":
        checks["vwap_cross"] = v_prev is not None and v_now is not None and prev["close"] <= v_prev and now["close"] > v_now
    else:
        checks["vwap_cross"] = v_prev is not None and v_now is not None and prev["close"] >= v_prev and now["close"] < v_now

    fast, slow = ema_fast[i], ema_slow[i]
    if fast is None or slow is None:
        checks["ema_aligned"] = False
    else:
        checks["ema_aligned"] = fast > slow if direction == "long" else fast < slow

    r = rsi_series[i]
    if r is None:
        checks["rsi_not_exhausted"] = False
    else:
        checks["rsi_not_exhausted"] = r < SETUP_A_RSI_CHASE_LONG if direction == "long" else r > SETUP_A_RSI_CHASE_SHORT

    lookback = candles[max(0, i - PULLBACK_LOOKBACK_CANDLES):i]
    if direction == "long":
        pullback_ref = min((c["low"] for c in lookback), default=now["low"])
        stop = pullback_ref
        entry = now["close"]
        target = entry + SETUP_A_TARGET_R * (entry - stop)
    else:
        pullback_ref = max((c["high"] for c in lookback), default=now["high"])
        stop = pullback_ref
        entry = now["close"]
        target = entry - SETUP_A_TARGET_R * (stop - entry)

    checks["min_reward_risk"] = False
    try:
        checks["min_reward_risk"] = reward_risk_ratio(entry, stop, target) >= MIN_REWARD_RISK
    except ValueError:
        pass  # entry == stop, e.g. a flat pullback window; falls through as not eligible

    eligible = all(checks.values())
    result = {
        "setup": "A",
        "direction": direction,
        "eligible": eligible,
        "checks": checks,
        "day_move_pct": round(day_move_pct, 2),
        "indicators": {
            "close": now["close"],
            "vwap": v_now,
            "ema_fast": fast,
            "ema_slow": slow,
            "rsi": r,
        },
    }
    if eligible:
        result["trade"] = {
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "reward_risk": round(reward_risk_ratio(entry, stop, target), 2),
        }
    return result


def evaluate_setup_b(candles: list[dict], prev_close: float, levels: list[dict]) -> dict:
    """levels: [{"label": str, "price": float}, ...] - pivots, prior day
    high/low, top-OI strikes. Requires price to be within `level_tolerance_pct`
    of one of these when the reversal bar prints, per rule 4B's "into a
    pre-identified level" filter - a move with no nearby level is not a fade
    candidate no matter how extended it looks."""
    warm_up = RSI_PERIOD + 1
    if len(candles) < warm_up:
        return {"eligible": False, "reason": f"Need >= {warm_up} candles for RSI warm-up, have {len(candles)}."}
    if not levels:
        return {"eligible": False, "reason": "No pre-identified levels supplied - not a fade candidate (rule 4B)."}

    closes = [c["close"] for c in candles]
    rsi_series = rsi(closes, RSI_PERIOD)

    i = len(candles) - 1
    now, prev = candles[i], candles[i - 1]

    level_tolerance_pct = 0.3
    nearby = [
        lv for lv in levels
        if abs(now["close"] - lv["price"]) / lv["price"] * 100 <= level_tolerance_pct
    ]

    checks = {}
    checks["rule_7_clear"] = _minutes_since_open(now) >= RULE7_NO_TRADE_MINUTES

    day_move_pct = _day_extreme_move_pct(candles, prev_close)
    checks["moved_enough"] = day_move_pct >= MOVE_FILTER_PCT
    checks["at_a_level"] = bool(nearby)

    # Fading strength (short) if price is above the level and pulling back
    # below it; fading weakness (long) if below and reclaiming it.
    direction = None
    if nearby:
        level_price = nearby[0]["price"]
        reversed_down = prev["close"] >= level_price and now["close"] < level_price
        reversed_up = prev["close"] <= level_price and now["close"] > level_price
        if reversed_down:
            direction = "short"
        elif reversed_up:
            direction = "long"
    checks["reversal_bar"] = direction is not None

    r = rsi_series[i]
    if r is None or direction is None:
        checks["rsi_exhausted"] = False
    else:
        checks["rsi_exhausted"] = r >= SETUP_B_RSI_OVERBOUGHT if direction == "short" else r <= SETUP_B_RSI_OVERSOLD

    eligible = all(checks.values())
    result = {
        "setup": "B",
        "direction": direction,
        "eligible": eligible,
        "checks": checks,
        "day_move_pct": round(day_move_pct, 2),
        "nearby_levels": nearby,
        "indicators": {"close": now["close"], "rsi": r},
    }
    if eligible:
        entry = now["close"]
        stop = max(c["high"] for c in candles) if direction == "short" else min(c["low"] for c in candles)
        risk = (stop - entry) if direction == "short" else (entry - stop)
        sign = -1 if direction == "short" else 1
        scale1 = entry + sign * SETUP_B_SCALE1_R * risk
        target = entry + sign * SETUP_B_TARGET_R * risk
        result["trade"] = {
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "scale1_target": round(scale1, 2),
            "final_target": round(target, 2),
        }
    return result
