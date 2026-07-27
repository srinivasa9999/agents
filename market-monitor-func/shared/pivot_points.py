"""Classic (floor-trader) pivot point formulas.

Pure functions, no I/O and no Kite/Azure imports - callers (currently
scripts/daily_kite_login.py) fetch the previous trading day's high/low/close
themselves and pass it in. Kept dependency-free so both the VM script and
the Function App's shared/ package can import it cheaply.
"""


def compute_classic_pivots(high: float, low: float, close: float) -> dict[str, float]:
    """Standard 3-level classic pivot formula, computed from one prior day's
    H/L/C. Returns the pivot (P) plus resistances R1-R3 and supports S1-S3."""
    pivot = (high + low + close) / 3
    price_range = high - low
    return {
        "P": pivot,
        "R1": 2 * pivot - low,
        "S1": 2 * pivot - high,
        "R2": pivot + price_range,
        "S2": pivot - price_range,
        "R3": high + 2 * (pivot - low),
        "S3": low - 2 * (high - pivot),
    }


def pivots_to_watch_levels(pivots: dict[str, float], label_prefix: str = "Pivot") -> list[dict]:
    """Reshapes a pivots dict into the {label, price} form config.yaml's
    symbols.*.watch_levels already uses, so alert_engine.check_level_crossings
    can treat auto-computed and manually-configured levels identically."""
    order = ["R3", "R2", "R1", "P", "S1", "S2", "S3"]
    return [
        {"label": f"{label_prefix} {name}", "price": round(pivots[name], 2)}
        for name in order
        if name in pivots
    ]
