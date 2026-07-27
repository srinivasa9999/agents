#!/usr/bin/env python3
"""Runs on YOUR AZURE VM (not in the Function App) once per trading day,
before market open, to mint a fresh Kite access_token and store it in the
same Azure Table Storage the Function reads from.

============================================================================
WHY THIS EXISTS / THE LIMITATION IT WORKS AROUND
============================================================================
Kite Connect access tokens expire daily (~06:00 IST) and the *official*
way to get a new one is Zerodha's hosted login page, which requires you to
interactively enter your user ID, password and 2FA (TOTP) in a browser and
then exchange the resulting `request_token` for an `access_token` using
`kite.generate_session(request_token, api_secret)`. There is no official
headless/unattended login API - Zerodha does this deliberately for account
security. A serverless timer-triggered Function cannot do an interactive
browser login, so it cannot mint its own token.

============================================================================
WHAT THIS SCRIPT DOES INSTEAD, AND THE RISK YOU ARE ACCEPTING
============================================================================
This script automates the *interactive* login by driving Zerodha's
internal (undocumented, unofficial) login endpoints directly with
`requests`, using your Kite user ID + password + TOTP secret, in place of a
human clicking through the browser form. This is a widely-used pattern in
the retail algo-trading community, but be aware:

  - These endpoints are NOT part of the documented Kite Connect API and can
    change or break at any time without notice.
  - You are storing your Kite login password and TOTP secret (not just API
    keys) on your VM. Anyone with access to that VM/Key Vault can log in
    to your Zerodha account. Lock this down (Key Vault + managed identity,
    restrictive file permissions, no logging of secrets).
  - This may sit in a gray area of Zerodha's Terms of Service around
    automated access. You are responsible for reviewing and accepting that
    risk - this script is provided as-is for personal, single-account use.

If you'd rather not automate this at all, run this script with
`--manual` and it will print the login URL, wait for you to log in in a
browser yourself and paste back the `request_token` - no password/TOTP
storage needed. That's the safer default; only switch to full automation
once you're comfortable with the tradeoff above.

============================================================================
SETUP
============================================================================
Schedule via cron on the VM, e.g. crontab -e:
    50 8 * * 1-5 /usr/bin/python3 /path/to/daily_kite_login.py >> /var/log/kite_login.log 2>&1

Required environment variables (put these in Key Vault / a restricted
.env file readable only by the service account running cron, chmod 600):
    KITE_API_KEY        - same value as the Function App's KITE_API_KEY
    KITE_API_SECRET      - NEVER put this in the Function App
    KITE_USER_ID          - Zerodha client ID, e.g. AB1234
    KITE_PASSWORD          - Zerodha login password           (full-auto mode only)
    KITE_TOTP_SECRET         - base32 TOTP secret from your Kite 2FA setup (full-auto mode only)
    AZURE_STORAGE_CONNECTION_STRING - same storage account the Function App uses
    KITE_STATE_TABLE           - defaults to "KiteAuthState"
    PIVOT_LEVELS_TABLE          - defaults to "PivotLevels" (see pivot section below)
    OPTION_CHAIN_TABLE           - defaults to "OptionChainInstruments" (see option chain section below)

============================================================================
DAILY PIVOT POINTS (auto-computed support/resistance)
============================================================================
After logging in, this script also computes classic floor-trader pivot
points (P, R1-R3, S1-S3) for NIFTY and BANKNIFTY from the previous trading
day's high/low/close and writes them to the `PIVOT_LEVELS_TABLE` table. The
Function App merges these in alongside your manually-configured
`watch_levels` in config.yaml (see shared/state_store.py's
get_pivot_levels and function_app.py's _run_kite_checks) - no redeploy
needed since this happens at read time.

This step requires Zerodha's Historical API add-on subscription (a paid
add-on to the base Kite Connect API plan, sold separately) because it
calls kite.historical_data(). If you don't have that add-on, this step
logs a warning and is skipped - it will NOT fail the login itself, since
minting today's access token is this script's primary job.

============================================================================
DAILY OPTION CHAIN (PCR + top-OI-strike)
============================================================================
Also resolves, once a day, an ATM-centered window of NIFTY/BANKNIFTY option
strikes for the nearest expiry (via kite.instruments("NFO") - a large
one-time dump, which is why this only runs here, pre-market, and not inside
the Function's 5-10 minute timer) and writes the resulting
strike/tradingsymbol list to the `OPTION_CHAIN_TABLE` table. The Function
App quotes just that curated list each cycle (piggybacking on API calls it
already makes - no extra Kite subscription cost) to compute PCR (put-call
ratio) and flag when the highest-OI call/put strike shifts. See
shared/option_chain.py and function_app.py's _run_option_chain_checks.
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from azure.data.tables import TableServiceClient, UpdateMode
from kiteconnect import KiteConnect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.pivot_points import compute_classic_pivots  # noqa: E402
from shared.option_chain import select_atm_strikes  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_kite_login")

LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"
CONNECT_LOGIN_URL = "https://kite.zerodha.com/connect/login"

# Symbols to compute daily pivot levels for. Keep the kite_symbol values in
# sync with config/config.yaml's symbols.*.kite_symbol - they're duplicated
# here (rather than read from config.yaml) so this VM script stays
# self-contained and doesn't need PyYAML or the Function's config file.
PIVOT_SYMBOLS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
}

# Option-chain window per symbol: how many strikes above/below ATM to track
# for PCR/OI. "name" must match the `name` column in Kite's NFO instrument
# dump (the underlying's name, not its NSE index tradingsymbol).
OPTION_CHAIN_SYMBOLS = {
    "NIFTY": {"kite_symbol": "NSE:NIFTY 50", "name": "NIFTY", "strikes_each_side": 4},
    "BANKNIFTY": {"kite_symbol": "NSE:NIFTY BANK", "name": "BANKNIFTY", "strikes_each_side": 6},
}


def automated_login(api_key: str, user_id: str, password: str, totp_secret: str) -> str:
    """Drives Zerodha's unofficial login endpoints to obtain a request_token.
    See module docstring for the risk tradeoffs of doing this."""
    session = requests.Session()

    resp = session.post(LOGIN_URL, data={"user_id": user_id, "password": password}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Kite login step 1 (password) failed: {payload}")
    request_id = payload["data"]["request_id"]

    totp_code = pyotp.TOTP(totp_secret).now()
    resp = session.post(
        TWOFA_URL,
        data={"user_id": user_id, "request_id": request_id, "twofa_value": totp_code, "twofa_type": "totp"},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Kite login step 2 (TOTP) failed: {payload}")

    resp = session.get(CONNECT_LOGIN_URL, params={"api_key": api_key, "v": "3"}, allow_redirects=False, timeout=15)
    # Zerodha issues one or more redirects that eventually land on your
    # registered redirect URL with ?request_token=...&status=success
    location = resp.headers.get("Location")
    hops = 0
    while location and "request_token" not in location and hops < 5:
        resp = session.get(location, allow_redirects=False, timeout=15)
        location = resp.headers.get("Location")
        hops += 1

    if not location or "request_token" not in location:
        raise RuntimeError("Could not obtain request_token from Kite redirect chain - login flow may have changed.")

    request_token = parse_qs(urlparse(location).query)["request_token"][0]
    return request_token


def manual_login(api_key: str) -> str:
    kite = KiteConnect(api_key=api_key)
    print("Open this URL, log in, and paste the full redirect URL (or just the request_token) below:")
    print(kite.login_url())
    pasted = input("Redirect URL or request_token: ").strip()
    if "request_token=" in pasted:
        return parse_qs(urlparse(pasted).query)["request_token"][0]
    return pasted


def store_access_token(connection_string: str, table_name: str, api_key: str, access_token: str) -> None:
    service = TableServiceClient.from_connection_string(connection_string)
    try:
        service.create_table(table_name)
    except Exception:
        pass  # table already exists
    table = service.get_table_client(table_name)
    table.upsert_entity(
        {
            "PartitionKey": "kite",
            "RowKey": "session",
            "api_key": api_key,
            "access_token": access_token,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        mode=UpdateMode.MERGE,
    )


def fetch_previous_day_ohlc(kite: KiteConnect, kite_symbol: str) -> tuple[float, float, float] | None:
    """Returns (high, low, close) for the most recently completed trading
    day for kite_symbol, via kite.historical_data() - the daily "day" candle
    right before today's (this script runs pre-market, so today has no
    candle yet and the last one returned is yesterday's session).

    Returns None if no candle came back. Raises whatever kite.quote() /
    kite.historical_data() raise on failure (e.g. Historical API add-on not
    subscribed) - the caller decides whether that's fatal.
    """
    quote = kite.quote([kite_symbol])[kite_symbol]
    instrument_token = quote["instrument_token"]

    to_date = date.today()
    from_date = to_date - timedelta(days=10)  # covers weekends/holiday gaps
    candles = kite.historical_data(instrument_token, from_date, to_date, interval="day")
    if not candles:
        return None

    last_candle = candles[-1]
    return last_candle["high"], last_candle["low"], last_candle["close"]


def store_pivot_levels(
    connection_string: str,
    table_name: str,
    symbol_name: str,
    pivots: dict[str, float],
    source_high: float,
    source_low: float,
    source_close: float,
) -> None:
    service = TableServiceClient.from_connection_string(connection_string)
    try:
        service.create_table(table_name)
    except Exception:
        pass  # table already exists
    table = service.get_table_client(table_name)
    entity = {
        "PartitionKey": "pivot",
        "RowKey": symbol_name,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source_high": source_high,
        "source_low": source_low,
        "source_close": source_close,
    }
    for level_name, price in pivots.items():
        entity[f"level_{level_name}"] = price
    table.upsert_entity(entity, mode=UpdateMode.MERGE)


def compute_and_store_daily_pivots(kite: KiteConnect, conn_str: str, table_name: str) -> None:
    """Best-effort: logs and moves on for any symbol that fails, since a
    missing Historical API subscription (or a transient error) here must
    never take down the daily login this script exists to perform."""
    for symbol_name, kite_symbol in PIVOT_SYMBOLS.items():
        try:
            ohlc = fetch_previous_day_ohlc(kite, kite_symbol)
            if ohlc is None:
                logger.warning("No historical candle returned for %s (%s) - skipping pivot calc.", symbol_name, kite_symbol)
                continue
            high, low, close = ohlc
            pivots = compute_classic_pivots(high, low, close)
            store_pivot_levels(conn_str, table_name, symbol_name, pivots, high, low, close)
            logger.info(
                "Stored pivot levels for %s: P=%.2f R1=%.2f S1=%.2f (from prev day H=%.2f L=%.2f C=%.2f)",
                symbol_name, pivots["P"], pivots["R1"], pivots["S1"], high, low, close,
            )
        except Exception:
            logger.exception(
                "Pivot point calculation failed for %s (%s) - if this is a "
                "permissions/403 error, you likely don't have Zerodha's "
                "Historical API add-on enabled on this Kite Connect app. "
                "Non-fatal, continuing.",
                symbol_name, kite_symbol,
            )


def resolve_option_chain(kite: KiteConnect, name: str, kite_symbol: str, strikes_each_side: int) -> dict | None:
    """Picks the nearest unexpired NFO expiry for `name` (e.g. "NIFTY"),
    selects `strikes_each_side` strikes above/below the current ATM strike,
    and returns {"expiry": "YYYY-MM-DD", "strikes": [{"strike":, "ce_symbol":,
    "ce_token":, "pe_symbol":, "pe_token":}, ...]}.

    Returns None if no matching contracts are found (e.g. between expiry
    rollovers). Raises whatever kite.quote()/kite.instruments() raise on
    failure - the caller decides whether that's fatal.
    """
    quote = kite.quote([kite_symbol])[kite_symbol]
    spot = quote["last_price"]

    instruments = kite.instruments("NFO")
    rows = [row for row in instruments if row.get("name") == name and row.get("instrument_type") in ("CE", "PE")]
    if not rows:
        return None

    today = date.today()
    expiries = sorted({row["expiry"] for row in rows if row["expiry"] >= today})
    if not expiries:
        return None
    nearest_expiry = expiries[0]

    rows = [row for row in rows if row["expiry"] == nearest_expiry]
    selected_strikes = select_atm_strikes([row["strike"] for row in rows], spot, strikes_each_side)

    by_strike: dict[float, dict] = {}
    for row in rows:
        if row["strike"] not in selected_strikes:
            continue
        entry = by_strike.setdefault(row["strike"], {"strike": row["strike"]})
        if row["instrument_type"] == "CE":
            entry["ce_symbol"] = f"NFO:{row['tradingsymbol']}"
            entry["ce_token"] = row["instrument_token"]
        else:
            entry["pe_symbol"] = f"NFO:{row['tradingsymbol']}"
            entry["pe_token"] = row["instrument_token"]

    strikes = sorted(by_strike.values(), key=lambda e: e["strike"])
    return {"expiry": nearest_expiry.isoformat(), "strikes": strikes}


def store_option_chain(connection_string: str, table_name: str, symbol_name: str, chain: dict) -> None:
    service = TableServiceClient.from_connection_string(connection_string)
    try:
        service.create_table(table_name)
    except Exception:
        pass  # table already exists
    table = service.get_table_client(table_name)
    table.upsert_entity(
        {
            "PartitionKey": "chain",
            "RowKey": symbol_name,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "expiry": chain["expiry"],
            "strikes_json": json.dumps(chain["strikes"]),
        },
        mode=UpdateMode.MERGE,
    )


def compute_and_store_option_chains(kite: KiteConnect, conn_str: str, table_name: str) -> None:
    """Best-effort, same rationale as compute_and_store_daily_pivots: a
    missing permission or a transient error here must never take down the
    daily login this script exists to perform."""
    for symbol_name, cfg in OPTION_CHAIN_SYMBOLS.items():
        try:
            chain = resolve_option_chain(kite, cfg["name"], cfg["kite_symbol"], cfg["strikes_each_side"])
            if chain is None:
                logger.warning("No option chain contracts found for %s - skipping.", symbol_name)
                continue
            store_option_chain(conn_str, table_name, symbol_name, chain)
            logger.info(
                "Stored option chain for %s: expiry=%s strikes=%d",
                symbol_name, chain["expiry"], len(chain["strikes"]),
            )
        except Exception:
            logger.exception(
                "Option chain resolution failed for %s - if this is a permissions error, "
                "check your Kite Connect app's F&O/derivatives access. Non-fatal, continuing.",
                symbol_name,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", action="store_true", help="Prompt for interactive browser login instead of full TOTP automation.")
    args = parser.parse_args()

    api_key = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    table_name = os.environ.get("KITE_STATE_TABLE", "KiteAuthState")

    if args.manual:
        request_token = manual_login(api_key)
    else:
        user_id = os.environ["KITE_USER_ID"]
        password = os.environ["KITE_PASSWORD"]
        totp_secret = os.environ["KITE_TOTP_SECRET"]
        request_token = automated_login(api_key, user_id, password, totp_secret)

    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]

    store_access_token(conn_str, table_name, api_key, access_token)
    logger.info("Stored fresh Kite access token (expires ~06:00 IST tomorrow).")

    pivot_table_name = os.environ.get("PIVOT_LEVELS_TABLE", "PivotLevels")
    compute_and_store_daily_pivots(kite, conn_str, pivot_table_name)

    option_chain_table_name = os.environ.get("OPTION_CHAIN_TABLE", "OptionChainInstruments")
    compute_and_store_option_chains(kite, conn_str, option_chain_table_name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
