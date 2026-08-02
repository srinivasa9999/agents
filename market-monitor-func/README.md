# Nifty / Bank Nifty Market Monitor (Azure Function)

A timer-triggered, **read-only** Azure Function that watches Nifty 50, Bank
Nifty, India VIX, your open Kite positions, and a fixed news-keyword list
during NSE market hours, and pings you on Telegram when something you
defined in `config/config.yaml` trips. It never places, modifies, or
cancels an order — see [Safety](#safety-this-is-alerting-only) below.

## Architecture

```
                    ┌─────────────────────────────────────────┐
Azure VM (yours)    │  scripts/daily_kite_login.py (cron,      │
  holds:             │  ~08:50 IST Mon-Fri)                     │
  - Kite password    │  logs into Kite (TOTP-automated or        │
  - TOTP secret       │  manual paste), writes today's            │
  - api_secret        │  access_token to Table Storage             │
                    └───────────────────┬───────────────────────┘
                                        │ writes
                                        ▼
                          Azure Table Storage (KiteAuthState,
                          AlertState, SeenHeadlines) - same
                          storage account the Function App uses
                                        ▲
                                        │ reads
                    ┌───────────────────┴───────────────────────┐
Azure Function      │  function_app.py (timer, every 5-10 min)   │
  holds only:        │  - is_market_open_now() gate                │
  - api_key          │  - reads config.yaml                          │
  - Telegram token    │  - reads access_token (never generates one)   │
                    │  - Kite quotes/positions -> alert_engine.py     │
                    │  - RSS keyword check -> news_check.py            │
                    │  - sends Telegram messages -> telegram_notify.py  │
                    └─────────────────────────────────────────────────┘
```

The Function App never sees your Kite password, TOTP secret, or
`api_secret` — those live only on your VM. This is the key design decision
that works around Kite's daily-login requirement; see
[Kite auth](#2-kite-auth---read-this-the-daily-login-problem) for why.

## Repo layout

```
market-monitor-func/
├── function_app.py            Timer trigger + orchestration
├── host.json, requirements.txt
├── config/config.yaml         YOUR watch levels/thresholds/keywords - edit this, not the code
├── data/nse_holidays.json     NSE holiday list (INCOMPLETE - see warning in the file)
├── shared/
│   ├── config_loader.py       Loads config.yaml / nse_holidays.json
│   ├── market_calendar.py     Is-it-market-hours-right-now logic
│   ├── kite_auth.py           Reads (never generates) a Kite session
│   ├── market_data.py         Read-only Kite quote/positions wrapper
│   ├── alert_engine.py        Level-cross / VIX / position / PCR / OI threshold logic + cooldown
│   ├── pivot_points.py        Classic pivot point formula (pure math, no I/O)
│   ├── option_chain.py        PCR / top-OI-strike math (pure, no I/O)
│   ├── news_check.py          RSS keyword scan with new-headline dedup
│   ├── telegram_notify.py     Telegram Bot API sender
│   └── state_store.py         Azure Table Storage persistence
├── scripts/daily_kite_login.py       Raw-HTTP login (VM, not the Function) -
│                                      also computes/stores daily pivot levels
│                                      and resolves the daily option chain
├── scripts/playwright_kite_login.py  What kite_login_cron_wrapper.sh (the
│                                      actual daily cron) runs - same login +
│                                      pivot/option-chain computation, via a
│                                      real browser
└── tests/                           pytest unit tests for the logic above
```

---

## 1. Telegram bot setup

1. Message **@BotFather** on Telegram, `/newbot`, follow the prompts. You'll get a bot token like `123456:ABC-DEF...`.
2. Message your new bot anything (e.g. "hi") so it has a chat with you.
3. Get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id": <number>, ...}` in the response.
4. Keep the token and chat ID handy for the app settings step below.

## 2. Kite auth - read this, the daily-login problem

Kite Connect access tokens **expire every day around 06:00 IST** and can
only be minted via an interactive login (or TOTP-automated equivalent)
that requires your `api_secret`. There is no supported way for a
serverless timer function to log itself in. This is a hard platform
limitation, not something this code works around cleverly - so the design
splits responsibilities:

- **Your VM** runs `scripts/daily_kite_login.py` once each morning via
  cron, *before* market open. It performs the login (see two modes below)
  and writes the resulting `access_token` to Azure Table Storage.
- **The Function App** only ever *reads* that token. If it's missing or
  stale (not from today), the function skips Kite-dependent checks, sends
  you one Telegram warning (rate-limited to hourly, not spammed), and
  keeps running the Kite-independent news check.

Two login modes, pick one:

- **`--manual`** (safer, some daily effort): prints the Kite login URL,
  you complete it in a browser, paste back the `request_token`. No
  password/TOTP stored anywhere.
- **Full TOTP automation** (default, no daily effort): the script drives
  Zerodha's *unofficial* login endpoints using your user ID, password, and
  TOTP secret via `pyotp`. This is a well-known pattern in the retail
  algo-trading community, but it is **not part of the documented Kite
  Connect API**, can break if Zerodha changes their login page, and
  requires storing your Kite password + TOTP secret on the VM (Key Vault,
  restrictive file permissions - never in git). Read the full docstring in
  `scripts/daily_kite_login.py` before enabling it.

Either way, `api_secret` and your Kite password/TOTP secret **never touch
the Function App** - only `api_key` does.

## 3. Auto-computed pivot levels (optional, extra cost)

Both login scripts - `daily_kite_login.py` and `playwright_kite_login.py`
(whichever your cron actually runs; see [§11](#11-daily-kite-login-script-on-your-vm))
- compute classic floor-trader pivot
points (P, R1-R3, S1-S3) for NIFTY and BANKNIFTY from the previous trading
day's high/low/close, right after minting today's access token, and write
them to the `PIVOT_LEVELS_TABLE` table (default `PivotLevels`). The Function
App merges these in alongside your manual `watch_levels` at alert time (see
`pivot_levels.enabled` in `config/config.yaml`) - no redeploy needed for a
new day's levels to take effect.

**This needs Zerodha's Historical API add-on**, a separate paid subscription
on top of the base Kite Connect API plan (`kite.historical_data()` fails
without it). If you don't have it enabled, the pivot step logs a warning and
is skipped - it does not fail the login itself, and the function falls back
to just your manual `watch_levels`. Decide whether the add-on is worth it
before relying on this; otherwise keep hand-setting support/resistance in
`config.yaml` as before.

## 4. Auto-computed option chain: PCR + top-OI-strike (optional, extra permission)

Both login scripts also resolve, once a day right after logging in, an
ATM-centered window of NIFTY/BANKNIFTY option strikes for the nearest
unexpired expiry - NIFTY: ATM +/-4 strikes, BANKNIFTY: ATM +/-6 strikes (see
`OPTION_CHAIN_SYMBOLS` in `scripts/daily_kite_login.py` if you want to widen
or narrow this) - and store that curated strike/tradingsymbol list in the
`OPTION_CHAIN_TABLE` table (default `OptionChainInstruments`).

The Function App then quotes just that list every cycle (18 instruments for
NIFTY, 26 for BANKNIFTY in the default ranges) to compute:
- **PCR** (put-call ratio: total put OI / total call OI across the tracked
  strikes) - alerts once when it moves outside the configured band
  (`option_chain.pcr_alert.low_threshold`/`high_threshold` in
  `config/config.yaml`, default 0.7/1.3) and re-arms when it returns inside.
- **Top-OI-strike shifts** - alerts when the strike holding the highest call
  OI (resistance) or highest put OI (support) changes from the last cycle.

**Cost**: resolving the chain (`kite.instruments("NFO")`, a multi-MB dump of
every F&O contract) only happens once a day, pre-market, in the login
script - never inside the Function's 5-10 minute timer. The Function's
per-cycle cost is just two extra `quote()` calls (measured ~0.08s each for
the default ranges) - the same endpoint it already calls for
NIFTY/BANKNIFTY/VIX, at no extra Kite subscription cost. This needs your
Kite Connect app to have F&O/derivatives instrument access; if
`kite.instruments("NFO")` or the option quotes fail, the resolution step
logs a warning and is skipped for that symbol without failing the login or
the alert run.

## 5. Configure your watch levels/thresholds

Edit `config/config.yaml`:
- `symbols.NIFTY.watch_levels` / `symbols.BANKNIFTY.watch_levels` — your support/resistance/strike levels.
- `vix.intraday_move_pct_threshold` — default 5%.
- `positions.*` — P&L (absolute ₹ and/or %) and LTP-move thresholds for open positions.
- `news.keywords` / `news.feeds` — keyword list and RSS sources.
- `alerting.cooldown_minutes` — minimum gap between repeat alerts on the same condition.

This file ships with placeholder example levels — **replace them with your
real numbers before relying on this.**

## 6. NSE holidays - incomplete by design, verify before relying on it

`data/nse_holidays.json` includes only the fixed-date national holidays we
could confirm (Republic Day, Good Friday, Maharashtra Din, Muharram 2026,
Gandhi Jayanti, Christmas). Lunar/Hindu/Islamic-calendar holidays (Holi,
Eid, Ganesh Chaturthi, Dussehra, Diwali, Guru Nanak Jayanti, etc.) shift
every year and are **not included** — the file lists them under
`unconfirmed_variable_holidays_todo` as a reminder. Cross-check and
complete the list against NSE's official circular (published every
December for the next year) at
`nseindia.com/resources/exchange-communication-holidays` before the
function runs unattended for an extended period. Missing a holiday means
the function evaluates stale/closed-market prices during that window, not
a crash — but it can produce a misleading alert.

## 7. Local testing

Install [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local) and [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) (local Table Storage emulator):

```bash
cd market-monitor-func
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp local.settings.json.sample local.settings.json
# edit local.settings.json: set AzureWebJobsStorage to "UseDevelopmentStorage=true"
# (requires Azurite running), fill in KITE_API_KEY / TELEGRAM_* for a real test

python -m pytest                 # unit tests, no Azure/Kite/Telegram needed
azurite --silent &                # local Table Storage emulator
func start                        # runs the timer function locally
```

To force an immediate run instead of waiting for the schedule, call the
admin endpoint Core Tools prints at startup, or temporarily set
`"run_on_startup": true` in the `@app.timer_trigger` decorator for a local
test (revert before deploying).

## 8. Provision Azure resources

```bash
RG=market-monitor-rg
LOCATION=centralindia
STORAGE=marketmonitorst$RANDOM      # must be globally unique, lowercase, no dashes
FUNCAPP=market-monitor-func-$RANDOM # must be globally unique

az group create -n $RG -l $LOCATION

az storage account create -n $STORAGE -g $RG -l $LOCATION --sku Standard_LRS

az functionapp create \
  -g $RG -n $FUNCAPP \
  --storage-account $STORAGE \
  --consumption-plan-location $LOCATION \
  --runtime python --runtime-version 3.11 \
  --functions-version 4 \
  --os-type Linux
```

(Consumption plan is fine — this function runs briefly, a few hundred
times a day at most, well within the free grant. If you already have a
Function App / plan on your Azure VM's App Service environment, use that
instead of creating a new Consumption plan.)

## 9. App settings

```bash
STORAGE_CONN=$(az storage account show-connection-string -n $STORAGE -g $RG -o tsv)

az functionapp config appsettings set -g $RG -n $FUNCAPP --settings \
  "AzureWebJobsStorage=$STORAGE_CONN" \
  "TimerSchedule=0 */5 * * * *" \
  "KITE_API_KEY=<your kite api key>" \
  "KITE_STATE_TABLE=KiteAuthState" \
  "ALERT_STATE_TABLE=AlertState" \
  "NEWS_SEEN_TABLE=SeenHeadlines" \
  "PIVOT_LEVELS_TABLE=PivotLevels" \
  "OPTION_CHAIN_TABLE=OptionChainInstruments" \
  "TELEGRAM_BOT_TOKEN=<your telegram bot token>" \
  "TELEGRAM_CHAT_ID=<your telegram chat id>"
```

`TimerSchedule` is an NCRONTAB expression (`{second} {minute} {hour} {day} {month} {day-of-week}`).
`0 */5 * * * *` fires every 5 minutes, all day, every day — the function's
own `is_market_open_now()` check (weekday + configured hours + holiday
list, evaluated in IST) is what actually restricts activity to market
hours, so this is deliberately simple and immune to cron-timezone
footguns. Use `0 */10 * * * *` for a 10-minute cadence instead.

## 10. Deploy the function code

```bash
func azure functionapp publish $FUNCAPP
```

This packages `function_app.py`, `shared/`, `config/config.yaml`, and
`data/nse_holidays.json` together and deploys them. Because `config.yaml`
ships *inside* the deployment package, **editing it later requires
re-running `func azure functionapp publish`** (no code changes, just this
one command) — it is not hot-reloaded from disk. If you want to edit
levels without redeploying at all, a natural follow-up is to move
`config.yaml` into Blob Storage and read it from there instead of the
local file; not implemented here to keep the initial version simple.

## 11. Daily Kite login script on your VM

On the Azure VM:

```bash
python3.11 -m venv ~/kite-login-venv
source ~/kite-login-venv/bin/activate
pip install kiteconnect pyotp requests azure-data-tables

# store secrets somewhere only this account can read, e.g.:
sudo install -o $(whoami) -m 600 /dev/null /etc/kite-login.env
cat <<'EOF' | sudo tee /etc/kite-login.env > /dev/null
KITE_API_KEY=<same as Function App KITE_API_KEY>
KITE_API_SECRET=<your kite api_secret - NEVER put this in the Function App>
KITE_USER_ID=<your Zerodha client ID>
KITE_PASSWORD=<your Zerodha password>              # full-auto mode only
KITE_TOTP_SECRET=<your base32 TOTP secret>          # full-auto mode only
AZURE_STORAGE_CONNECTION_STRING=<same connection string as AzureWebJobsStorage>
KITE_STATE_TABLE=KiteAuthState
PIVOT_LEVELS_TABLE=PivotLevels
OPTION_CHAIN_TABLE=OptionChainInstruments
EOF
sudo chmod 600 /etc/kite-login.env
```

Cron entry (`crontab -e`), 08:50 IST Mon-Fri, well before the 09:15 open:

```
50 8 * * 1-5 set -a && . /etc/kite-login.env && set +a && /home/youruser/kite-login-venv/bin/python /path/to/market-monitor-func/scripts/daily_kite_login.py >> /var/log/kite_login.log 2>&1
```

Prefer the safer manual mode? Drop `KITE_PASSWORD`/`KITE_TOTP_SECRET` from
the env file and run the script with `--manual` from an interactive
terminal each morning instead of via cron.

For production, move these secrets into **Azure Key Vault** and have the
VM pull them at run time (via managed identity) instead of a plaintext
env file — the env file above is the minimum-viable version.

## 12. Verify end-to-end

```bash
func azure functionapp log-stream -g $RG -n $FUNCAPP
```

Run `daily_kite_login.py` once manually, confirm a Telegram message never
arrives falsely, then temporarily lower a watch level in `config.yaml` to
something you know the current price has already crossed, redeploy, and
confirm you get exactly one Telegram alert (not one every 5 minutes).
Revert the level afterward.

## 13. Ad-hoc stock analysis against docs/TRADING_PLAN.md (scripts/analyze_stock.py)

Separate from the timer-triggered Function, this runs on demand (same VM as
`daily_kite_login.py`, using the same stored session) to check any NSE stock
against the trading plan's Setup A / Setup B rules right now:

```bash
python3 scripts/analyze_stock.py RELIANCE
python3 scripts/analyze_stock.py NSE:RELIANCE --capital 500000
```

It fetches today's candles plus yesterday's OHLC (read-only, same
`kite.quote()` / `kite.historical_data()` calls used elsewhere in this repo),
computes VWAP/EMA9/EMA20/RSI14 (`shared/indicators.py`), and prints a
rule-by-rule checklist for both setups (`shared/setup_rules.py`) - which
conditions pass, which fail, and the entry/stop/target/size it would
suggest only if every condition for that setup currently passes. It never
places, modifies, or suggests placing an order via any Kite API call - see
"Safety" below. Needs the same Historical API add-on as the pivot-point
step in `daily_kite_login.py`.

The "pullback high/low" it uses for the stop is a documented proxy (lowest
low / highest high over the last 5 candles), not real swing-point
detection - sanity-check its output against the chart, the same way you'd
sanity-check any indicator, before acting on it.

## Editing config after deploy

- `config/config.yaml`, `data/nse_holidays.json`: edit locally, `func azure functionapp publish` again.
- App settings (thresholds are *not* here, but tokens/schedule are): `az functionapp config appsettings set ...` or the Portal, no redeploy needed, takes effect on next invocation.

## Safety: this is alerting-only

- `shared/market_data.py`'s `ReadOnlyKite` class exposes exactly two Kite
  calls — `quote()` and `positions()`. It does not wrap, import, or
  reference `place_order`, `modify_order`, `cancel_order`, GTTs, or any
  other mutating Kite Connect method, so there's no code path that could
  place a trade even by mistake.
- `daily_kite_login.py` (the VM script) only calls `generate_session` —
  it establishes a session, nothing else.
- If you ever extend this project, keep all Kite interaction going through
  `ReadOnlyKite` rather than importing `kiteconnect.KiteConnect` directly
  elsewhere.

## Known limitations

- NSE holiday list is incomplete for variable-date festivals — see [§6](#6-nse-holidays---incomplete-by-design-verify-before-relying-on-it).
- The TOTP auto-login path depends on Zerodha's undocumented login
  endpoints and may break without notice; the manual fallback is more
  durable but needs daily attention.
- Position-check state (`AlertState` table) isn't pruned when a position
  is closed — a stale `tripped` flag for a long-closed position just sits
  unused; harmless, but if you're auditing the table, that's why old
  entries linger.
- `config.yaml` changes require a redeploy, not a hot-reload (see §10).
- Auto pivot levels require Zerodha's paid Historical API add-on — see
  [§3](#3-auto-computed-pivot-levels-optional-extra-cost). Without it,
  `PivotLevels` stays empty and only your manual `watch_levels` apply.
- Pivot levels are computed once per day (pre-market) and never refreshed
  intraday, matching the classic floor-trader definition — they will not
  reflect same-day volatility swings (e.g. a big VIX move) until the next
  morning's run.
- The option chain's strike/tradingsymbol window (§4) is likewise resolved
  once per day, pre-market. If the nearest expiry rolls over mid-day (it
  won't for weekly index options resolved before 09:15) or ATM drifts far
  enough intraday, PCR/OI will be computed on a window that's no longer
  centered on the current spot price until the next morning's run re-picks it.
- Top-OI-strike shift alerts only track the single highest-OI call and put
  strike each - a full per-strike long/short-buildup breakdown (the richer
  but noisier option originally considered) isn't implemented.
