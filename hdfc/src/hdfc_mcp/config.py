"""Runtime configuration for the HDFC Securities InvestRight MCP server.

All values come from environment variables so no secret ever lives in code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://developer.hdfcsec.com"
DEFAULT_WS_URL = "wss://developer.hdfcsec.com/wsapi/v1/session"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
DEFAULT_SESSION_PATH = Path.home() / ".hdfc_investright" / "session.json"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    base_url: str = DEFAULT_BASE_URL
    ws_url: str = DEFAULT_WS_URL
    user_agent: str = DEFAULT_USER_AGENT
    session_path: Path = DEFAULT_SESSION_PATH
    http_timeout_seconds: float = 10.0
    max_concurrency: int = 5
    require_order_confirm: bool = True
    static_access_token: str | None = None
    # Safety rails
    read_only: bool = False
    max_order_value: float = 0.0  # rupees; 0 disables the cap
    max_order_qty: int = 0  # 0 disables the cap
    duplicate_order_window_seconds: float = 10.0

    @staticmethod
    def load() -> "Settings":
        api_key = os.environ.get("HDFC_API_KEY", "").strip()
        api_secret = os.environ.get("HDFC_API_SECRET", "").strip()
        if not api_key:
            raise ConfigError(
                "HDFC_API_KEY is not set. Export it (or put it in a .env file) "
                "before starting the server."
            )
        if not api_secret:
            raise ConfigError(
                "HDFC_API_SECRET is not set. Export it (or put it in a .env file) "
                "before starting the server."
            )

        session_path_str = os.environ.get("HDFC_SESSION_PATH", "").strip()
        session_path = (
            Path(session_path_str).expanduser() if session_path_str else DEFAULT_SESSION_PATH
        )

        return Settings(
            api_key=api_key,
            api_secret=api_secret,
            base_url=os.environ.get("HDFC_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            ws_url=os.environ.get("HDFC_WS_URL", DEFAULT_WS_URL),
            user_agent=os.environ.get("HDFC_USER_AGENT", DEFAULT_USER_AGENT),
            session_path=session_path,
            http_timeout_seconds=float(os.environ.get("HDFC_HTTP_TIMEOUT", "10")),
            max_concurrency=int(os.environ.get("HDFC_MAX_CONCURRENCY", "5")),
            require_order_confirm=os.environ.get("HDFC_REQUIRE_ORDER_CONFIRM", "true").lower()
            not in ("0", "false", "no"),
            static_access_token=os.environ.get("HDFC_ACCESS_TOKEN", "").strip() or None,
            read_only=os.environ.get("HDFC_READ_ONLY", "false").lower() in ("1", "true", "yes"),
            max_order_value=float(os.environ.get("HDFC_MAX_ORDER_VALUE", "0") or 0),
            max_order_qty=int(os.environ.get("HDFC_MAX_ORDER_QTY", "0") or 0),
            duplicate_order_window_seconds=float(
                os.environ.get("HDFC_DUPLICATE_ORDER_WINDOW", "10") or 10
            ),
        )
