from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shared.setup_rules import evaluate_setup_a, evaluate_setup_b, position_size, reward_risk_ratio

IST = ZoneInfo("Asia/Kolkata")


def make_candles(prices, start_minute=0, interval=3):
    """prices: list of (open, high, low, close, volume) tuples, one per
    candle, `interval` minutes apart starting `start_minute` minutes after
    09:15 IST."""
    base = datetime(2026, 7, 31, 9, 15, tzinfo=IST) + timedelta(minutes=start_minute)
    return [
        {
            "time": base + timedelta(minutes=i * interval),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        }
        for i, (o, h, l, c, v) in enumerate(prices)
    ]


# ---------------------------------------------------------------- position sizing

def test_position_size_rounds_down():
    # 1% of 500000 = 5000; risk/share = 1.4 -> 3571.4 -> rounds down to 3571
    assert position_size(500000, entry=288.80, stop=290.20) == 3571


def test_position_size_rejects_equal_entry_and_stop():
    with pytest.raises(ValueError):
        position_size(500000, entry=100, stop=100)


def test_reward_risk_ratio():
    assert reward_risk_ratio(entry=100, stop=98, target=106) == 3.0


# ------------------------------------------------------------------------ Setup A

def test_setup_a_not_enough_candles():
    candles = make_candles([(100, 101, 99, 100, 500)] * 5)
    result = evaluate_setup_a(candles, prev_close=100)
    assert result["eligible"] is False
    assert "warm-up" in result["reason"]


def test_setup_a_full_eligible_long_scenario():
    """Gap-up spike (4%) inside the rule-7 window, short pullback, a noisy
    but net-up recovery that keeps price below VWAP and RSI moderate, then
    a breakout candle that crosses VWAP, aligns the 9/20 EMA, and clears
    2R against the pullback low - every Setup A condition passes at once.
    Verified by running evaluate_setup_a() directly while constructing this
    (see PR description), not hand-derived."""
    prices = [
        (100, 104, 100, 103, 2000),
        (103, 103.5, 102.5, 103, 500),
        (103, 103.2, 102.8, 103, 500),
        (103, 103.2, 102.7, 102.9, 500),
        (102.9, 103, 102.5, 102.8, 500),
    ]
    c = 102.8
    for _ in range(5):
        c -= 0.1
        prices.append((c + 0.1, c + 0.15, c - 0.15, c, 300))
    for s in [0.08, -0.02, 0.06, -0.02, 0.05, -0.02, 0.05, -0.01, 0.04]:
        c += s
        prices.append((c - s, max(c, c - s) + 0.03, min(c, c - s) - 0.03, c, 400))
    last_close = prices[-1][3]
    prices.append((last_close, last_close + 0.9, last_close - 0.05, last_close + 0.75, 5000))

    candles = make_candles(prices)
    result = evaluate_setup_a(candles, prev_close=100.0)

    assert result["eligible"] is True
    assert result["direction"] == "long"
    assert all(result["checks"].values()), result["checks"]
    assert result["trade"]["reward_risk"] >= 1.5
    assert result["trade"]["stop"] < result["trade"]["entry"] < result["trade"]["target"]


def test_setup_a_rejects_when_move_too_small():
    # Flat day: never moves >= MOVE_FILTER_PCT (3%) from prev_close
    prices = [(100, 100.3, 99.8, 100.1, 500) for _ in range(20)]
    candles = make_candles(prices)
    result = evaluate_setup_a(candles, prev_close=100.0)
    assert result["eligible"] is False
    assert result["checks"]["moved_enough"] is False


def test_setup_a_rejects_chasing_an_already_exhausted_rsi():
    # Same shape as the eligible scenario but with the noisy recovery phase
    # replaced by a straight, uninterrupted climb - RSI pins near 100,
    # which the rsi_not_exhausted check (rule: skip if RSI >= 75 for a
    # long) must catch even though price, VWAP and EMA all still line up.
    prices = [
        (100, 104, 100, 103, 2000),
        (103, 103.5, 102.5, 103, 500),
        (103, 103.2, 102.8, 103, 500),
        (103, 103.2, 102.7, 102.9, 500),
        (102.9, 103, 102.5, 102.8, 500),
    ]
    c = 102.8
    for _ in range(5):
        c -= 0.1
        prices.append((c + 0.1, c + 0.15, c - 0.15, c, 300))
    for _ in range(10):
        c += 0.2
        prices.append((c - 0.2, c + 0.05, c - 0.25, c, 400))

    candles = make_candles(prices)
    result = evaluate_setup_a(candles, prev_close=100.0)
    assert result["checks"]["rsi_not_exhausted"] is False
    assert result["eligible"] is False


# ------------------------------------------------------------------------ Setup B

def test_setup_b_no_levels_supplied():
    candles = make_candles([(100, 101, 99, 100, 500)] * 20)
    result = evaluate_setup_b(candles, prev_close=100, levels=[])
    assert result["eligible"] is False
    assert "pre-identified" in result["reason"]


def test_setup_b_full_eligible_short_scenario():
    """Steady rally (RSI pinned high) runs into a resistance level and
    prints a reversal bar back below it - Setup B's location + reversal +
    RSI-exhaustion conditions all pass together. Verified by running
    evaluate_setup_b() directly while constructing this, not hand-derived."""
    prices = [(100, 101, 100, 100.8, 1000)]
    c = 100.8
    for _ in range(14):
        c += 0.35
        prices.append((c - 0.35, c + 0.1, c - 0.4, c, 500))
    level_price = c - 0.2
    prices.append((c, c + 0.15, level_price - 0.3, level_price - 0.2, 900))

    candles = make_candles(prices)
    levels = [{"label": "Resistance", "price": level_price}]
    result = evaluate_setup_b(candles, prev_close=100.0, levels=levels)

    assert result["eligible"] is True
    assert result["direction"] == "short"
    assert all(result["checks"].values()), result["checks"]
    trade = result["trade"]
    # for a short: target below scale1, scale1 below entry, entry below stop
    assert trade["final_target"] < trade["scale1_target"] < trade["entry"] < trade["stop"]


def test_setup_b_rejects_when_no_level_nearby():
    # Same rally, but the only known level is far away from where price
    # actually is - "at_a_level" must fail even though the move is real.
    prices = [(100, 101, 100, 100.8, 1000)]
    c = 100.8
    for _ in range(14):
        c += 0.35
        prices.append((c - 0.35, c + 0.1, c - 0.4, c, 500))
    candles = make_candles(prices)
    levels = [{"label": "Far away resistance", "price": 200.0}]
    result = evaluate_setup_b(candles, prev_close=100.0, levels=levels)
    assert result["checks"]["at_a_level"] is False
    assert result["eligible"] is False
