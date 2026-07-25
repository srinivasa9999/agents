"""Timer-triggered Azure Function: market monitoring / alerting only.

This function is READ-ONLY. It never places, modifies, or cancels any
order - it only reads quotes/positions and sends Telegram messages. See
shared/market_data.py (ReadOnlyKite) for how that's enforced structurally,
not just by convention.

The timer fires every few minutes, all day, every day (see TimerSchedule
app setting) - the actual "is it a trading session right now" decision is
made in code via shared.market_calendar.is_market_open_now, which checks
weekday, configured market hours, and the NSE holiday list. This is more
robust than trying to encode IST market hours directly into an NCRONTAB
expression.
"""
import logging
import os

import azure.functions as func

from shared.alert_engine import check_level_crossings, check_positions, check_vix_move
from shared.config_loader import load_config, load_holidays
from shared.kite_auth import get_kite_client
from shared.market_calendar import is_market_open_now
from shared.market_data import MarketDataError, ReadOnlyKite, get_index_quotes, get_open_positions
from shared.models import Alert
from shared.news_check import check_news
from shared.state_store import StateStore
from shared.telegram_notify import send_alerts, send_telegram_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market_monitor")

app = func.FunctionApp()


def _get_state_store() -> StateStore:
    conn_str = os.environ["AzureWebJobsStorage"]
    return StateStore(
        connection_string=conn_str,
        kite_table=os.environ.get("KITE_STATE_TABLE", "KiteAuthState"),
        alert_table=os.environ.get("ALERT_STATE_TABLE", "AlertState"),
        news_table=os.environ.get("NEWS_SEEN_TABLE", "SeenHeadlines"),
    )


def _notify_system_issue(state_store: StateStore, bot_token: str, chat_id: str, condition_key: str, message: str, cooldown_minutes: int = 60) -> None:
    """Same cooldown mechanism as market alerts, so a persistent failure
    (e.g. stale Kite token all day) pings you once an hour, not every tick."""
    if state_store.cooldown_elapsed(condition_key, cooldown_minutes):
        send_telegram_message(bot_token, chat_id, f"⚠️ Market monitor issue:\n{message}")
        state_store.record_alert_sent(condition_key)
    else:
        logger.info("System issue notification suppressed by cooldown: %s", message)


@app.timer_trigger(schedule="%TimerSchedule%", arg_name="timer", run_on_startup=False, use_monitor=True)
def market_monitor_timer(timer: func.TimerRequest) -> None:
    logger.info("Timer fired (past_due=%s)", timer.past_due)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured - cannot send alerts, aborting run.")
        return

    try:
        config = load_config()
        holidays = load_holidays()
    except Exception:
        logger.exception("Failed to load config/holidays - aborting run")
        return

    is_open, reason = is_market_open_now(config, holidays)
    if not is_open:
        logger.info("Market closed (%s) - skipping checks.", reason)
        return

    try:
        state_store = _get_state_store()
    except Exception:
        logger.exception("Failed to connect to Table Storage - aborting run")
        return

    cooldown_minutes = config.get("alerting", {}).get("cooldown_minutes", 15)
    all_alerts: list[Alert] = []

    # ---- Kite-backed checks: price levels, VIX, open positions ----
    api_key = os.environ.get("KITE_API_KEY")
    if not api_key:
        auth_error = "KITE_API_KEY app setting is not configured."
        kite_client = None
    else:
        kite_client, auth_error = get_kite_client(state_store, api_key)

    if auth_error:
        logger.error("Kite auth unavailable: %s", auth_error)
        _notify_system_issue(state_store, bot_token, chat_id, "system_kite_auth_failure", auth_error)
    else:
        kite = ReadOnlyKite(kite_client)
        try:
            all_alerts.extend(_run_kite_checks(kite, config, cooldown_minutes, state_store))
        except Exception:
            logger.exception("Kite-backed checks failed unexpectedly")
            _notify_system_issue(
                state_store, bot_token, chat_id, "system_kite_checks_failure",
                "Kite-backed checks (levels/VIX/positions) raised an unexpected error - see function logs.",
            )

    # ---- News/headline check (independent of Kite) ----
    try:
        all_alerts.extend(check_news(config.get("news", {}), state_store))
    except Exception:
        logger.exception("News check failed unexpectedly")
        _notify_system_issue(
            state_store, bot_token, chat_id, "system_news_failure",
            "News/headline check raised an unexpected error - see function logs.",
        )

    if all_alerts:
        logger.info("Sending %d alert(s)", len(all_alerts))
        send_alerts(bot_token, chat_id, all_alerts)
    else:
        logger.info("No conditions tripped this run.")


def _run_kite_checks(kite: ReadOnlyKite, config: dict, cooldown_minutes: int, state_store: StateStore) -> list[Alert]:
    alerts: list[Alert] = []
    symbols_config = config.get("symbols", {})
    hysteresis = config.get("level_cross_hysteresis_points", 0)
    vix_config = config.get("vix", {})

    kite_symbols = [s["kite_symbol"] for s in symbols_config.values()]
    if vix_config.get("kite_symbol"):
        kite_symbols.append(vix_config["kite_symbol"])

    try:
        quotes = get_index_quotes(kite, kite_symbols)
    except MarketDataError:
        logger.exception("Failed to fetch index quotes")
        quotes = {}

    for symbol_name, symbol_config in symbols_config.items():
        quote = quotes.get(symbol_config["kite_symbol"])
        if not quote:
            logger.warning("No quote returned for %s (%s)", symbol_name, symbol_config["kite_symbol"])
            continue
        alerts.extend(check_level_crossings(
            symbol_name=symbol_name,
            current_price=quote["last_price"],
            levels=symbol_config.get("watch_levels", []),
            hysteresis_points=hysteresis,
            cooldown_minutes=cooldown_minutes,
            state_store=state_store,
        ))

    if vix_config.get("kite_symbol"):
        vix_quote = quotes.get(vix_config["kite_symbol"])
        if vix_quote:
            alerts.extend(check_vix_move(
                current_vix=vix_quote["last_price"],
                threshold_pct=vix_config.get("intraday_move_pct_threshold", 5.0),
                cooldown_minutes=cooldown_minutes,
                state_store=state_store,
            ))
        else:
            logger.warning("No quote returned for India VIX")

    try:
        positions = get_open_positions(kite)
    except MarketDataError:
        logger.exception("Failed to fetch positions")
        positions = []

    alerts.extend(check_positions(
        positions=positions,
        positions_config=config.get("positions", {}),
        cooldown_minutes=cooldown_minutes,
        state_store=state_store,
    ))

    return alerts
