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
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from azure.data.tables import TableServiceClient, UpdateMode
from kiteconnect import KiteConnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_kite_login")

LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"
CONNECT_LOGIN_URL = "https://kite.zerodha.com/connect/login"


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
