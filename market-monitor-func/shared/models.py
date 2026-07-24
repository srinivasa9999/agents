from dataclasses import dataclass
from datetime import datetime


@dataclass
class Alert:
    condition_key: str  # stable id used for state/cooldown tracking
    category: str  # "level_cross" | "vix" | "position_pnl" | "position_ltp" | "news"
    title: str
    current_value: str
    threshold: str
    detail: str = ""
    timestamp: datetime | None = None
