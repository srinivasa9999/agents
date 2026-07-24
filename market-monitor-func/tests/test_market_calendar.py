from datetime import datetime
from zoneinfo import ZoneInfo

from shared.market_calendar import is_market_open_now

CONFIG = {"market_hours": {"start": "09:15", "end": "15:30", "timezone": "Asia/Kolkata"}}
IST = ZoneInfo("Asia/Kolkata")


def _utc(ist_year, ist_month, ist_day, ist_hour, ist_minute):
    return datetime(ist_year, ist_month, ist_day, ist_hour, ist_minute, tzinfo=IST).astimezone(ZoneInfo("UTC"))


def test_open_during_market_hours_on_weekday():
    # Wednesday 2026-07-22, 11:00 IST
    now = _utc(2026, 7, 22, 11, 0)
    is_open, _ = is_market_open_now(CONFIG, holidays=set(), now_utc=now)
    assert is_open is True


def test_closed_before_market_open():
    now = _utc(2026, 7, 22, 9, 0)
    is_open, reason = is_market_open_now(CONFIG, holidays=set(), now_utc=now)
    assert is_open is False
    assert "outside market hours" in reason


def test_closed_after_market_close():
    now = _utc(2026, 7, 22, 16, 0)
    is_open, reason = is_market_open_now(CONFIG, holidays=set(), now_utc=now)
    assert is_open is False
    assert "outside market hours" in reason


def test_closed_on_weekend():
    # Saturday 2026-07-25
    now = _utc(2026, 7, 25, 11, 0)
    is_open, reason = is_market_open_now(CONFIG, holidays=set(), now_utc=now)
    assert is_open is False
    assert "weekend" in reason


def test_closed_on_configured_holiday():
    now = _utc(2026, 10, 2, 11, 0)  # Gandhi Jayanti, a Friday
    is_open, reason = is_market_open_now(CONFIG, holidays={"2026-10-02"}, now_utc=now)
    assert is_open is False
    assert "holiday" in reason
