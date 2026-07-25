"""Decides whether the current moment is inside NSE market hours."""
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

logger = logging.getLogger("market_monitor.calendar")

IST = ZoneInfo("Asia/Kolkata")


def is_market_open_now(config: dict, holidays: set, now_utc: datetime | None = None) -> tuple[bool, str]:
    """Returns (is_open, reason). `now_utc` is injectable for tests."""
    now = (now_utc or datetime.now(tz=ZoneInfo("UTC"))).astimezone(IST)

    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False, f"weekend ({now.strftime('%A')})"

    date_str = now.strftime("%Y-%m-%d")
    if date_str in holidays:
        return False, f"NSE holiday ({date_str})"

    hours = config.get("market_hours", {})
    start_str = hours.get("start", "09:15")
    end_str = hours.get("end", "15:30")
    start_h, start_m = (int(x) for x in start_str.split(":"))
    end_h, end_m = (int(x) for x in end_str.split(":"))
    start_t = time(start_h, start_m)
    end_t = time(end_h, end_m)

    now_t = now.time()
    if now_t < start_t or now_t > end_t:
        return False, f"outside market hours ({now_t.strftime('%H:%M')} IST, window {start_str}-{end_str} IST)"

    return True, "market open"
