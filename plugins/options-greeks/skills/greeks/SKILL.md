---
name: greeks
description: Compute Black-Scholes-Merton option pricing, implied volatility, delta, gamma, theta, vega, and rho from spot price, strike, expiry, and either a market premium or an assumed volatility. Use when the user asks to price an option, or to compute implied volatility, delta, gamma, theta, vega, rho, or "the Greeks" for a call or put — for any underlying, not just a specific broker's account. This skill has no market-data access of its own; it is a pure calculator.
---

# Options Greeks Calculator

This skill wraps `scripts/option_greeks.py`, a dependency-free Black-Scholes-Merton
calculator. It makes no network calls and has no account or broker access — every
input must come from the user or from another skill/tool that already has live data
(e.g. a broker MCP server).

## Before running the script

Never invent spot price, strike, expiry, or premium. If any required input is
missing or ambiguous:
- Ask the user for it, or
- If a market-data tool/skill is available in this session (e.g. a broker's quote
  or instrument-search tool), use that to fetch the exact figure and tell the user
  where each number came from and its timestamp.

Do not proceed with stale, guessed, or rounded-from-memory prices.

## Running the calculator

The script lives at `${CLAUDE_PLUGIN_ROOT}/scripts/option_greeks.py` — always invoke it
by that path (not a relative path), since your working directory is not the plugin
directory.

Two mutually exclusive modes:

- **Solve implied volatility from a market premium:**
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/option_greeks.py" --spot S --strike K --expiry YYYY-MM-DD --type call|put --premium P [--rate R] [--dividend Q]`
- **Compute Greeks directly from an assumed/given volatility** (no premium needed):
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/option_greeks.py" --spot S --strike K --expiry YYYY-MM-DD --type call|put --vol V [--rate R] [--dividend Q]`

Add `--json` for structured output when you need to parse the result programmatically.

Notes:
- `--rate` defaults to 0.06 (6%) if omitted; `--dividend` defaults to 0. The script
  always reports which of these it defaulted in an `assumptions` list (JSON) or as
  `# assumption:` lines (text output) — relay these to the user verbatim, do not
  silently drop them.
- For dividend-paying stock options, pass `--dividend` with the underlying's
  annualized dividend yield; omitting it is a real source of error, not just a
  formality.
- `--close-hour-utc` controls the assumed expiry settlement time (default 10 = 15:30
  IST / NSE close). Override it for non-Indian exchanges.
- The script validates the premium against no-arbitrage bounds and raises an error
  (`price` not computed) if the premium is outside them, non-positive, or the expiry
  has already passed. Surface that error to the user rather than retrying with
  adjusted inputs on your own.

## Reporting results

Report implied/assumed volatility, delta, gamma, theta (per calendar day), vega (per
1 percentage-point volatility change), and rho (per 1 percentage-point rate change).
State plainly that these are model estimates, not exchange-provided figures, and
repeat the assumptions the script reported (rate, dividend, European exercise).
Black-Scholes assumes European exercise, continuous pricing, and constant
volatility/rates — flag American-style early-exercise value and discrete dividend
dates as unmodeled, and label results for illiquid or stale-quoted options as
unreliable rather than computing anyway.

This skill never places, modifies, or evaluates trades, and never gives investment
advice — it only computes model figures from the inputs it's given.
