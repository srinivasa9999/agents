"""Option-chain PCR (put-call ratio) and top-OI-strike analysis.

Pure functions, no Kite/Azure I/O - callers fetch the option chain
(scripts/daily_kite_login.py resolves which strikes/expiry to track once a
day; function_app.py quotes them each cycle and calls summarize_chain()).
"""


def select_atm_strikes(strikes: list[float], spot_price: float, strikes_each_side: int) -> list[float]:
    """Given every listed strike for one expiry, returns the ATM strike plus
    `strikes_each_side` strikes above and below it, sorted ascending. Input
    need not be sorted or de-duplicated."""
    unique_sorted = sorted(set(strikes))
    if not unique_sorted:
        return []
    atm_index = min(range(len(unique_sorted)), key=lambda i: abs(unique_sorted[i] - spot_price))
    low = max(0, atm_index - strikes_each_side)
    high = min(len(unique_sorted), atm_index + strikes_each_side + 1)
    return unique_sorted[low:high]


def compute_pcr(total_put_oi: float, total_call_oi: float) -> float | None:
    """Put-Call Ratio = total put OI / total call OI across the selected
    strikes. None if call OI is zero (can't divide, and a zero-OI chain
    means the numbers aren't meaningful yet anyway)."""
    if total_call_oi <= 0:
        return None
    return total_put_oi / total_call_oi


def summarize_chain(chain_quotes: list[dict]) -> dict:
    """chain_quotes: [{"strike": float, "ce_oi": float, "pe_oi": float}, ...]

    Returns total call/put OI, the PCR, and which strike currently holds
    the highest call OI / put OI - the strikes option writers have the
    most skin in, commonly read as the market's working
    resistance/support."""
    total_call_oi = sum(c["ce_oi"] for c in chain_quotes)
    total_put_oi = sum(c["pe_oi"] for c in chain_quotes)
    max_call = max(chain_quotes, key=lambda c: c["ce_oi"], default=None)
    max_put = max(chain_quotes, key=lambda c: c["pe_oi"], default=None)
    return {
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "pcr": compute_pcr(total_put_oi, total_call_oi),
        "max_call_oi_strike": max_call["strike"] if max_call else None,
        "max_call_oi": max_call["ce_oi"] if max_call else None,
        "max_put_oi_strike": max_put["strike"] if max_put else None,
        "max_put_oi": max_put["pe_oi"] if max_put else None,
    }
