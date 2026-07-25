"""Kite Connect session handling for the *function side only*.

IMPORTANT - read this before you touch auth:

Kite Connect access tokens are valid until ~06:00 IST the next day and can
only be minted by completing an interactive login (or the TOTP-automated
equivalent - see scripts/daily_kite_login.py) that needs your `api_secret`.

This function NEVER holds your api_secret and NEVER generates a session.
It only reads a token that something else (the daily login script, run on
your VM) already generated and wrote to Table Storage. This keeps the
higher-privilege credential (api_secret, your Kite password, your TOTP
secret) off the Function App entirely - the Function App only needs the
low-privilege `api_key` and a short-lived access token it cannot renew
itself.

If the token is missing, stale (not from today), or rejected by Kite as
invalid, `get_kite_client` returns None and a human-readable reason. The
caller is expected to send a Telegram alert telling you to check the daily
login script and then skip the Kite-dependent checks for that run - it must
NOT try to log in on its own.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from .state_store import StateStore

logger = logging.getLogger("market_monitor.kite_auth")

IST = ZoneInfo("Asia/Kolkata")


def get_kite_client(state_store: StateStore, api_key: str) -> tuple[KiteConnect | None, str | None]:
    """Returns (kite_client, error). Exactly one of the two is None."""
    session = state_store.get_kite_session()
    if not session:
        return None, (
            "No Kite session found in state store. The daily login script "
            "(scripts/daily_kite_login.py) has not run yet today, or has "
            "never run."
        )

    access_token = session.get("access_token")
    generated_at = session.get("generated_at")
    if not access_token or not generated_at:
        return None, "Kite session entity is malformed (missing access_token/generated_at)."

    generated_date = datetime.fromisoformat(generated_at).astimezone(IST).date()
    today = datetime.now(tz=IST).date()
    if generated_date != today:
        return None, (
            f"Kite access token is stale (generated {generated_date}, today is {today}). "
            "Kite tokens expire daily - the daily login script needs to run again."
        )

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    try:
        kite.margins()  # cheap read-only call used purely to validate the token
    except KiteException as exc:
        return None, f"Kite rejected the stored access token ({type(exc).__name__}: {exc})."
    except Exception as exc:  # network errors etc.
        logger.exception("Unexpected error validating Kite session")
        return None, f"Could not validate Kite session due to an unexpected error: {exc}"

    return kite, None
