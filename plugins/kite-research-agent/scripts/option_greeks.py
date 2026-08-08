#!/usr/bin/env python3
"""Estimate Black-Scholes implied volatility and European option Greeks."""
import argparse
import math
from datetime import datetime, timezone

def cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def pdf(x): return math.exp(-x*x/2) / math.sqrt(2*math.pi)

def years_until(expiry):
    d = datetime.strptime(expiry, "%Y-%m-%d").date()
    close = datetime(d.year, d.month, d.day, 10, tzinfo=timezone.utc)  # 15:30 IST
    result = (close - datetime.now(timezone.utc)).total_seconds() / 31536000
    if result <= 0: raise ValueError("expiry must be in the future")
    return result

def greeks(s, k, t, r, v, kind):
    rt = math.sqrt(t); d1 = (math.log(s/k) + (r + v*v/2)*t)/(v*rt); d2 = d1-v*rt; disc = math.exp(-r*t)
    gamma = pdf(d1)/(s*v*rt); vega = s*pdf(d1)*rt/100
    if kind == "call":
        price = s*cdf(d1)-k*disc*cdf(d2); delta = cdf(d1)
        theta = (-s*pdf(d1)*v/(2*rt)-r*k*disc*cdf(d2))/365; rho = k*t*disc*cdf(d2)/100
    else:
        price = k*disc*cdf(-d2)-s*cdf(-d1); delta = cdf(d1)-1
        theta = (-s*pdf(d1)*v/(2*rt)+r*k*disc*cdf(-d2))/365; rho = -k*t*disc*cdf(-d2)/100
    return price, delta, gamma, theta, vega, rho

def iv(s, k, t, r, premium, kind):
    lower = max(0, s-k*math.exp(-r*t)) if kind == "call" else max(0, k*math.exp(-r*t)-s)
    upper = s if kind == "call" else k*math.exp(-r*t)
    if not lower <= premium <= upper: raise ValueError(f"premium must be between {lower:.4f} and {upper:.4f}")
    lo, hi = 1e-5, 10
    for _ in range(200):
        mid = (lo+hi)/2
        if greeks(s,k,t,r,mid,kind)[0] < premium: lo = mid
        else: hi = mid
    return (lo+hi)/2

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--spot", type=float, required=True); p.add_argument("--strike", type=float, required=True)
p.add_argument("--expiry", required=True); p.add_argument("--premium", type=float, required=True)
p.add_argument("--type", choices=("call", "put"), required=True); p.add_argument("--rate", type=float, default=.06)
a = p.parse_args()
if min(a.spot,a.strike,a.premium) <= 0: p.error("spot, strike, and premium must be positive")
t = years_until(a.expiry); volatility = iv(a.spot,a.strike,t,a.rate,a.premium,a.type)
price, delta, gamma, theta, vega, rho = greeks(a.spot,a.strike,t,a.rate,volatility,a.type)
for key, value in (("time_to_expiry_years",t),("implied_volatility",volatility),("implied_volatility_percent",volatility*100),("model_price",price),("delta",delta),("gamma",gamma),("theta_per_day",theta),("vega_per_vol_point",vega),("rho_per_rate_point",rho)):
    print(f"{key}={value:.8f}")
