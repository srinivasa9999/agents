from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Alert:
    condition_key: str  # stable id used for state/cooldown tracking
    category: str  # "level_cross" | "vix" | "position_pnl" | "position_ltp" | "news" | "pcr" | "oi_top_shift"
    title: str
    current_value: str
    threshold: str
    detail: str = ""
    timestamp: datetime | None = None
    # Called once this alert is *confirmed delivered* to Telegram. This is how
    # "seen"/"tripped"/"last_alert_at" state gets persisted - deliberately not
    # done at alert-creation time, so a failed send doesn't get silently
    # marked as handled (see send_alerts in telegram_notify.py).
    on_delivered: Callable[[], None] | None = None
