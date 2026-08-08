#!/usr/bin/env python3
"""Black-Scholes-Merton option pricing, implied volatility, and Greeks.

Standalone calculator: no network access, no account data. Caller supplies
spot, strike, expiry, rate, dividend yield, and either a market premium
(to solve implied volatility) or a volatility (to compute Greeks directly).
"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone

def cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def pdf(x): return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)

def years_until(expiry, close_hour_utc):
    d = datetime.strptime(expiry, "%Y-%m-%d").date()
    close = datetime(d.year, d.month, d.day, close_hour_utc, tzinfo=timezone.utc)
    result = (close - datetime.now(timezone.utc)).total_seconds() / 31536000
    if result <= 0:
        raise ValueError("expiry must be in the future")
    return result

def price_and_greeks(s, k, t, r, q, v, kind):
    rt = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + v * v / 2) * t) / (v * rt)
    d2 = d1 - v * rt
    disc_r, disc_q = math.exp(-r * t), math.exp(-q * t)
    gamma = disc_q * pdf(d1) / (s * v * rt)
    vega = s * disc_q * pdf(d1) * rt / 100
    if kind == "call":
        price = s * disc_q * cdf(d1) - k * disc_r * cdf(d2)
        delta = disc_q * cdf(d1)
        theta = (-s * disc_q * pdf(d1) * v / (2 * rt) - r * k * disc_r * cdf(d2)
                  + q * s * disc_q * cdf(d1)) / 365
        rho = k * t * disc_r * cdf(d2) / 100
    else:
        price = k * disc_r * cdf(-d2) - s * disc_q * cdf(-d1)
        delta = -disc_q * cdf(-d1)
        theta = (-s * disc_q * pdf(d1) * v / (2 * rt) + r * k * disc_r * cdf(-d2)
                  - q * s * disc_q * cdf(-d1)) / 365
        rho = -k * t * disc_r * cdf(-d2) / 100
    return {"price": price, "delta": delta, "gamma": gamma,
            "theta_per_day": theta, "vega_per_vol_point": vega, "rho_per_rate_point": rho}

def implied_vol(s, k, t, r, q, premium, kind):
    disc_r, disc_q = math.exp(-r * t), math.exp(-q * t)
    if kind == "call":
        lower, upper = max(0.0, s * disc_q - k * disc_r), s * disc_q
    else:
        lower, upper = max(0.0, k * disc_r - s * disc_q), k * disc_r
    if not lower <= premium <= upper:
        raise ValueError(
            f"premium must be between {lower:.4f} and {upper:.4f} "
            f"(no-arbitrage bounds at rate={r}, dividend={q})"
        )
    lo, hi = 1e-5, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if price_and_greeks(s, k, t, r, q, mid, kind)["price"] < premium:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--spot", type=float, required=True, help="Underlying spot price")
    p.add_argument("--strike", type=float, required=True, help="Option strike price")
    p.add_argument("--expiry", required=True, help="Expiry date, YYYY-MM-DD")
    p.add_argument("--type", choices=("call", "put"), required=True)
    p.add_argument("--rate", type=float, default=None,
                    help="Annualized risk-free rate, e.g. 0.06 for 6%% (default 0.06 if omitted)")
    p.add_argument("--dividend", "-q", type=float, default=None,
                    help="Annualized continuous dividend yield, e.g. 0.02 for 2%% (default 0 if omitted)")
    p.add_argument("--close-hour-utc", type=int, default=10,
                    help="Expiry settlement hour in UTC (default 10 = 15:30 IST, NSE close)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--premium", type=float, help="Market premium: solve implied volatility from this")
    mode.add_argument("--vol", type=float, help="Volatility (e.g. 0.20 for 20%%): compute Greeks directly, skip IV solve")
    p.add_argument("--json", action="store_true", help="Emit structured JSON instead of key=value lines")
    a = p.parse_args()

    if min(a.spot, a.strike) <= 0:
        p.error("spot and strike must be positive")
    if a.premium is not None and a.premium <= 0:
        p.error("premium must be positive")
    if a.vol is not None and a.vol <= 0:
        p.error("vol must be positive")

    assumptions = [
        "European exercise assumed (Black-Scholes-Merton); no early-exercise premium is modeled for American-style options",
    ]
    rate = a.rate if a.rate is not None else 0.06
    if a.rate is None:
        assumptions.append(f"--rate not supplied: defaulted to {rate:.4f} ({rate:.2%})")
    dividend = a.dividend if a.dividend is not None else 0.0
    if a.dividend is None:
        assumptions.append(f"--dividend not supplied: defaulted to {dividend:.4f} ({dividend:.2%}) — treat results for dividend-paying stock options as approximate")
    if a.close_hour_utc == 10:
        assumptions.append("expiry settlement time assumed 15:30 IST (NSE close); pass --close-hour-utc to override for other exchanges")

    try:
        t = years_until(a.expiry, a.close_hour_utc)
        if a.vol is not None:
            volatility, solved = a.vol, False
        else:
            volatility, solved = implied_vol(a.spot, a.strike, t, rate, dividend, a.premium, a.type), True
        greeks = price_and_greeks(a.spot, a.strike, t, rate, dividend, volatility, a.type)
    except ValueError as e:
        if a.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error={e}")
        sys.exit(1)

    result = {
        "inputs": {"spot": a.spot, "strike": a.strike, "expiry": a.expiry, "type": a.type,
                    "rate": rate, "dividend": dividend},
        "time_to_expiry_years": t,
        "volatility": volatility,
        "volatility_percent": volatility * 100,
        "volatility_source": "solved_from_premium" if solved else "supplied_directly",
        **greeks,
        "assumptions": assumptions,
    }

    if a.json:
        print(json.dumps(result, indent=2))
    else:
        flat = {k: v for k, v in result.items() if k not in ("inputs", "assumptions", "volatility_source")}
        for key, value in flat.items():
            print(f"{key}={value:.8f}")
        print(f"volatility_source={result['volatility_source']}")
        for note in assumptions:
            print(f"# assumption: {note}")

if __name__ == "__main__":
    main()
