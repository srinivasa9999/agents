#!/usr/bin/env python3
"""Automated Kite login via a real headless Chromium browser (Playwright),
driving the actual login UI instead of hitting undocumented API endpoints
directly with raw HTTP (that approach, in daily_kite_login.py's
automated_login(), is blocked by Zerodha's Cloudflare bot management at
the 2FA step even with correct credentials - a real browser executing real
JS passes where a spoofed HTTP client doesn't).

Notes on Kite's login SPA, reverse-engineered by trial and error:
  - Step 1 (userid/password) and step 2 (TOTP) reuse the same field id
    ("#userid") for different inputs - step 2's "#userid" is actually the
    6-digit TOTP field (type=number), not a userid re-entry.
  - The TOTP field auto-submits on the 6th digit. It must receive real
    keystroke events (element.type()) - Playwright's .fill() sets the
    value via JS and does not reliably trigger the auto-submit handler.
  - After a valid TOTP, Kite issues a real HTTP redirect chain ending at
    your app's registered redirect_url with ?request_token=... . If that
    redirect_url isn't a reachable server (e.g. http://127.0.0.1:5000/...
    used only as a placeholder for manual/local flows), the browser will
    fail to load it - that's expected. We capture the token from the
    redirect response's Location header before that final hop, so it
    doesn't matter whether the destination is reachable.

Requires the same env vars as daily_kite_login.py's full-auto mode:
  KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD,
  KITE_TOTP_SECRET, AZURE_STORAGE_CONNECTION_STRING, KITE_STATE_TABLE (optional)
"""
import os
import sys
from datetime import datetime, timezone

import pyotp
from playwright.sync_api import sync_playwright
from kiteconnect import KiteConnect
from azure.data.tables import TableServiceClient, UpdateMode


def get_request_token(api_key: str, user_id: str, password: str, totp_secret: str) -> str:
    redirects: list[str] = []

    def on_response(resp):
        if 300 <= resp.status < 400:
            location = resp.headers.get("location")
            if location:
                redirects.append(location)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_context().new_page()
            page.on("response", on_response)

            page.goto(f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3", wait_until="networkidle")
            page.fill("#userid", user_id)
            page.fill("#password", password)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")

            otp_input = page.wait_for_selector("input[type=number]#userid", timeout=15000)
            totp_code = pyotp.TOTP(totp_secret).now()
            otp_input.click()
            try:
                otp_input.type(totp_code, delay=120)
            except Exception:
                pass  # auto-submit navigation can interrupt type() mid-keystroke; expected
            page.wait_for_timeout(4000)
        finally:
            browser.close()

    matches = [r for r in redirects if "request_token=" in r]
    if not matches:
        raise RuntimeError(f"No request_token found in redirect chain. Redirects seen: {redirects}")

    from urllib.parse import urlparse, parse_qs
    return parse_qs(urlparse(matches[-1]).query)["request_token"][0]


def store_access_token(connection_string: str, table_name: str, api_key: str, access_token: str) -> None:
    service = TableServiceClient.from_connection_string(connection_string)
    try:
        service.create_table(table_name)
    except Exception:
        pass
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
    api_key = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    user_id = os.environ["KITE_USER_ID"]
    password = os.environ["KITE_PASSWORD"]
    totp_secret = os.environ["KITE_TOTP_SECRET"]
    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    table_name = os.environ.get("KITE_STATE_TABLE", "KiteAuthState")

    request_token = get_request_token(api_key, user_id, password, totp_secret)

    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]

    store_access_token(conn_str, table_name, api_key, access_token)
    print(f"Stored fresh Kite access token for {session_data.get('user_name')} (expires ~06:00 IST tomorrow).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
