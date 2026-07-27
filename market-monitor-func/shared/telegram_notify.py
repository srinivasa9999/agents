"""Sends alerts to Telegram via the Bot API. A failed send is logged, never
raised, so one bad Telegram call doesn't take down the rest of the run."""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .models import Alert

logger = logging.getLogger("market_monitor.telegram")
IST = ZoneInfo("Asia/Kolkata")

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 10

_CATEGORY_EMOJI = {
    "level_cross": "📈",
    "vix": "⚡",
    "position_pnl": "💰",
    "position_ltp": "📊",
    "news": "📰",
    "pcr": "⚖️",
    "oi_top_shift": "🧲",
}

# Legacy Telegram "Markdown" parse mode only treats these four characters as
# special outside of an existing entity - escape them in interpolated text
# (headlines, links, labels) so they don't break parsing. News headlines/URLs
# routinely contain "_" and "*", which without this made sendMessage fail
# with a 400 "can't parse entities" for a lot of real-world headlines.
_MARKDOWN_SPECIAL_CHARS = ("_", "*", "`", "[")


def _escape_markdown(text: str) -> str:
    for ch in _MARKDOWN_SPECIAL_CHARS:
        text = text.replace(ch, f"\\{ch}")
    return text


def format_alert(alert: Alert) -> str:
    emoji = _CATEGORY_EMOJI.get(alert.category, "🔔")
    ts = (alert.timestamp or datetime.now(tz=IST)).strftime("%Y-%m-%d %H:%M:%S IST")
    lines = [
        f"{emoji} *{_escape_markdown(alert.title)}*",
        f"Current: {_escape_markdown(alert.current_value)}",
        f"Threshold: {_escape_markdown(alert.threshold)}",
    ]
    if alert.detail:
        lines.append(_escape_markdown(alert.detail))
    lines.append(f"_{ts}_")
    return "\n".join(lines)


def send_telegram_message(bot_token: str, chat_id: str, text: str, retries: int = 1) -> bool:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}

    attempt = 0
    while attempt <= retries:
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                return True
            logger.error("Telegram send failed (status %s): %s", resp.status_code, resp.text)
        except Exception:
            logger.exception("Telegram send raised an exception")
        attempt += 1
        if attempt <= retries:
            time.sleep(2)
    return False


def send_alerts(bot_token: str, chat_id: str, alerts: list[Alert]) -> None:
    for alert in alerts:
        text = format_alert(alert)
        ok = send_telegram_message(bot_token, chat_id, text)
        if ok:
            if alert.on_delivered:
                try:
                    alert.on_delivered()
                except Exception:
                    logger.exception("Failed to persist delivered state for alert: %s", alert.condition_key)
        else:
            logger.error("Giving up on Telegram delivery for alert: %s", alert.condition_key)
