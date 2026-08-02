#!/usr/bin/env python3
"""Run this on your VM (same place as daily_kite_login.py) to check a stock
against docs/TRADING_PLAN.md's Setup A / Setup B rules, right now, during
market hours.

    python3 scripts/analyze_stock.py RELIANCE
    python3 scripts/analyze_stock.py NSE:RELIANCE --capital 500000

WHAT THIS DOES AND DOES NOT DO
===============================================================================
Fetches today's candles + yesterday's OHLC from Kite (read-only - the same
`kite.quote()` / `kite.historical_data()` calls the Function App and the
daily login script already use), computes VWAP/EMA9/EMA20/RSI14, and prints
a rule-by-rule checklist for both setups against the *current* moment. It
does not scan history for a setup that already came and went, and it does
NOT place, modify, or suggest placing an order via any Kite API call - see
shared/market_data.py's docstring for why this repo stays structurally
read-only. Reading the printed checklist and deciding what to do about it
is still yours.

The "pullback high/low" used for the stop is a documented proxy - the
lowest low / highest high over the last PULLBACK_LOOKBACK_CANDLES candles
(shared/setup_rules.py), not real swing-point detection. Sanity-check it
against the chart before trusting it, the same way you'd sanity-check any
indicator.

REQUIRES
===============================================================================
Same Kite session as the Function App: this script reads today's
access_token from the same Azure Table Storage row daily_kite_login.py
already wrote (it does not log in itself, and never touches api_secret).

    KITE_API_KEY                      - same value as the Function App's
    AZURE_STORAGE_CONNECTION_STRING   - same storage account
    KITE_STATE_TABLE                  - defaults to "KiteAuthState"

Needs Zerodha's Historical API add-on (kite.historical_data()) - same
requirement as the pivot-point computation in daily_kite_login.py.
"""
import argparse
import os
import sys
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.state_store import StateStore  # noqa: E402
from shared.setup_rules import evaluate_setup_a, evaluate_setup_b, position_size, RISK_PCT_OF_CAPITAL  # noqa: E402
from shared.pivot_points import compute_classic_pivots, pivots_to_watch_levels  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)


def get_kite_client(api_key: str, conn_str: str, table_name: str) -> KiteConnect:
    """Same validation as shared/kite_auth.get_kite_client, duplicated (not
    imported) because that module is written for the Function's dependency
    injection style - this script just needs a quick fail-fast check."""
    store = StateStore(conn_str, kite_table=table_name, alert_table="AlertState", news_table="SeenHeadlines")
    session = store.get_kite_session()
    if not session:
        sys.exit("No Kite session found. Run daily_kite_login.py first today.")

    access_token = session.get("access_token")
    generated_at = session.get("generated_at")
    generated_date = datetime.fromisoformat(generated_at).astimezone(IST).date()
    if generated_date != datetime.now(tz=IST).date():
        sys.exit(f"Kite access token is stale (from {generated_date}). Run daily_kite_login.py again.")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    try:
        kite.margins()
    except KiteException as exc:
        sys.exit(f"Kite rejected the stored access token: {exc}")
    return kite


def fetch_prev_close_and_levels(kite: KiteConnect, kite_symbol: str) -> tuple[float, list[dict]]:
    """Previous trading day's close (for the % move filter) plus a Setup B
    level set built from that same prior day's H/L/C: the classic pivots
    and the prior day's high/low themselves. Works for any NSE equity, not
    just NIFTY/BANKNIFTY - unlike PivotLevels table, which daily_kite_login.py
    only populates for the two indices."""
    quote = kite.quote([kite_symbol])[kite_symbol]
    instrument_token = quote["instrument_token"]

    to_date = date.today()
    from_date = to_date - timedelta(days=10)
    daily_candles = kite.historical_data(instrument_token, from_date, to_date, interval="day")
    if not daily_candles:
        sys.exit(f"No historical daily candle returned for {kite_symbol}.")
    prior = daily_candles[-1]

    pivots = compute_classic_pivots(prior["high"], prior["low"], prior["close"])
    levels = pivots_to_watch_levels(pivots)
    levels.append({"label": "Prior day high", "price": prior["high"]})
    levels.append({"label": "Prior day low", "price": prior["low"]})
    return prior["close"], levels, instrument_token


def fetch_today_candles(kite: KiteConnect, instrument_token: int, interval: str = "3minute") -> list[dict]:
    now = datetime.now(tz=IST)
    session_open = datetime.combine(now.date(), MARKET_OPEN, tzinfo=IST)
    if now < session_open:
        sys.exit("Market hasn't opened yet today.")

    raw = kite.historical_data(instrument_token, session_open, now, interval=interval)
    return [
        {
            "time": c["date"].astimezone(IST) if c["date"].tzinfo else c["date"].replace(tzinfo=IST),
            "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c["volume"],
        }
        for c in raw
    ]


def _fmt(x):
    return "-" if x is None else (f"{x:.2f}" if isinstance(x, float) else str(x))


def print_report(symbol: str, capital: float, prev_close: float, result_a: dict, result_b: dict) -> None:
    print(f"\n{'=' * 60}\n{symbol}  (prev close {prev_close:.2f})\n{'=' * 60}")

    for result in (result_a, result_b):
        setup = result.get("setup", "?")
        print(f"\n--- Setup {setup} ---")
        if "reason" in result:
            print(f"  Not evaluated: {result['reason']}")
            continue

        print(f"  Direction candidate: {result['direction'] or '-'}")
        print(f"  Day move so far: {result['day_move_pct']}%")
        print("  Checks:")
        for name, passed in result["checks"].items():
            print(f"    [{'PASS' if passed else 'fail'}] {name}")
        if result.get("vwap_basis"):
            print(f"  VWAP basis: {result['vwap_basis']}")
        ind = result["indicators"]
        print(f"  Indicators: close={_fmt(ind.get('close'))} rsi={_fmt(ind.get('rsi'))} "
              + " ".join(f"{k}={_fmt(v)}" for k, v in ind.items() if k not in ("close", "rsi")))
        if result.get("nearby_levels"):
            print(f"  Nearby levels: {result['nearby_levels']}")

        if result["eligible"]:
            trade = result["trade"]
            entry, stop = trade["entry"], trade["stop"]
            qty = position_size(capital, entry, stop)
            risk_rupees = round(abs(entry - stop) * qty, 2)
            print(f"  *** ELIGIBLE *** entry={entry} stop={stop} qty={qty} "
                  f"(risk ~Rs {risk_rupees}, {RISK_PCT_OF_CAPITAL}% of Rs {capital:,.0f})")
            print(f"  Targets: {({k: v for k, v in trade.items() if k not in ('entry', 'stop')})}")
        else:
            print("  Not eligible right now - see failed checks above.")

    print(f"\n{'=' * 60}")
    print("This is a checklist, not an order. Nothing here places, modifies, "
          "or cancels anything on Kite. Rule 7 (no trading 09:15-09:30) and "
          "rule 4B (one attempt per stock per day) still apply on top of this.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("symbol", help="Trading symbol, with or without exchange prefix, e.g. RELIANCE or NSE:RELIANCE")
    parser.add_argument("--capital", type=float, default=500000, help="Trading capital in rupees (default 500000)")
    parser.add_argument("--interval", default="3minute", help="Candle interval (default 3minute)")
    args = parser.parse_args()

    kite_symbol = args.symbol if ":" in args.symbol else f"NSE:{args.symbol}"

    missing = [name for name in ("KITE_API_KEY", "AZURE_STORAGE_CONNECTION_STRING") if name not in os.environ]
    if missing:
        sys.exit(f"Missing required environment variable(s): {', '.join(missing)}. "
                  f"See the REQUIRES section in this script's --help.")
    api_key = os.environ["KITE_API_KEY"]
    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    table_name = os.environ.get("KITE_STATE_TABLE", "KiteAuthState")

    kite = get_kite_client(api_key, conn_str, table_name)
    prev_close, levels, instrument_token = fetch_prev_close_and_levels(kite, kite_symbol)
    candles = fetch_today_candles(kite, instrument_token, args.interval)

    if not candles:
        sys.exit(f"No candles yet today for {kite_symbol} - too early, or market data delayed.")

    result_a = evaluate_setup_a(candles, prev_close)
    result_b = evaluate_setup_b(candles, prev_close, levels)
    print_report(kite_symbol, args.capital, prev_close, result_a, result_b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
