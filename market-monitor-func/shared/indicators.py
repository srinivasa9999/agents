"""EMA, RSI and VWAP - pure functions, no I/O and no Kite/Azure imports.

Same convention as pivot_points.py and option_chain.py: callers fetch
candles themselves (scripts/analyze_stock.py) and pass plain lists in, so
these stay cheap to import and cheap to unit test.

Every function takes a chronologically-ordered list of candles and returns
a same-length list of floats, with `None` in the positions that don't have
enough history yet to compute a value - callers should not have to
separately track "how many bars are `period` warm-up."
"""


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average. The first `period - 1` entries are None;
    entry `period - 1` is seeded with the simple average of the first
    `period` values (the standard EMA seeding convention), and every entry
    after that follows the usual EMA recurrence."""
    if period < 1:
        raise ValueError("period must be >= 1")

    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    seed = sum(values[:period]) / period
    result[period - 1] = seed

    multiplier = 2 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        result[i] = prev
    return result


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI. The first `period` entries are None (need `period`
    price changes, i.e. `period + 1` closes, before the first value); from
    there on, gains/losses are smoothed with Wilder's running average
    (equivalent to an EMA with alpha = 1/period), not a plain moving
    average of the last `period` changes."""
    if period < 1:
        raise ValueError("period must be >= 1")

    result: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return result

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def vwap(highs: list[float], lows: list[float], closes: list[float], volumes: list[float]) -> list[float | None]:
    """Session-cumulative volume-weighted typical price ((H+L+C)/3),
    reset implicitly by only ever being called with one session's candles.
    None at any index where cumulative volume so far is still zero."""
    n = len(closes)
    if not (len(highs) == len(lows) == len(volumes) == n):
        raise ValueError("highs, lows, closes and volumes must be the same length")

    result: list[float | None] = [None] * n
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(n):
        typical_price = (highs[i] + lows[i] + closes[i]) / 3
        cum_pv += typical_price * volumes[i]
        cum_v += volumes[i]
        result[i] = (cum_pv / cum_v) if cum_v > 0 else None
    return result
