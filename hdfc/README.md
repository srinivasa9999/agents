# hdfc-investright-mcp

MCP server for HDFC Securities' **InvestRight Open API**, built in Python with
[FastMCP](https://github.com/jlowin/fastmcp). Covers the full documented
surface: login, orders, order/trade book, positions, holdings, funds, user
profile, LTP, the security master, and the live market-data **websocket**
feed — including **options Greeks (delta/gamma/vega/theta/rho)**, which the
plain REST API and Kite do not expose.

All endpoint shapes were extracted directly from
https://developer.hdfcsec.com/ir-docs (the docs site ships as a
client-rendered SPA; the raw JSON payloads were pulled from its compiled
route bundles and cross-checked against the linked `GenericDTO3.proto`
schema — see `src/hdfc_mcp/proto/generic_dto.proto`).

## Setup

```bash
cd hdfc
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # then fill in HDFC_API_KEY / HDFC_API_SECRET
```

## Running

```bash
source .venv/bin/activate
python -m hdfc_mcp.server
```

Or register it with Claude Code as an MCP server:

```bash
claude mcp add hdfc-investright -- /home/srinivas/agents/hdfc/.venv/bin/python -m hdfc_mcp.server
```

## Authentication

InvestRight's login flow needs a human-entered OTP, so it's exposed as a
sequence of tools rather than one automatic step:

1. `hdfc_login_get_token_id` → `token_id`
2. `hdfc_login_validate_credentials(token_id, username, password)` → returns the
   2FA question(s) to ask the user
3. `hdfc_login_validate_2fa(token_id, answer)` → `answer` must come from the
   real human, never fabricate it → returns a `request_token`
4. `hdfc_login_authorize(token_id, request_token)` → a fresh `request_token`
5. `hdfc_login_fetch_access_token(request_token)` → exchanges it for an
   access token, which is cached at `~/.hdfc_investright/session.json`
   (mode `0600`) and reused automatically by every other tool afterward.

`hdfc_login_status` / `hdfc_logout` check/clear the cached session.

If you already have a valid access token (e.g. from the browser-based
`/oapi/v1/login` flow), skip all of this and just set `HDFC_ACCESS_TOKEN` in
`.env` — every tool will use it directly.

## Safety rails on order tools

`hdfc_place_order`, `hdfc_modify_order`, `hdfc_cancel_order`, and
`hdfc_cancel_all_orders` all:

- **Validate required conditional fields before sending** (e.g. `expiry_date`
  for F&O, `strike_price` + `option_type` for options, `price`/`trigger_price`
  for LIMIT/SL orders) — a malformed order is rejected locally, never sent.
- **Never fire without `confirm=true`.** Calling without it returns a
  `"status": "preview"` response showing the exact payload that would be
  sent — use this to double-check before actually placing the order.
- **Are never auto-retried.** A network blip on an order call surfaces as an
  error instead of silently retrying (which could double-place or
  double-cancel an order). Read-only GET calls do get a small bounded retry
  for transient 502/503/504s.
- **Block accidental duplicates.** An identical `place_order` payload fired
  within `HDFC_DUPLICATE_ORDER_WINDOW` seconds (default 10) of the last one is
  rejected unless `allow_duplicate=true` — this specifically protects against
  an LLM retrying an order call whose response timed out but whose order went
  through. The error tells the caller to check `hdfc_get_orders` first.
- **Respect optional hard caps.** Set `HDFC_MAX_ORDER_QTY` and/or
  `HDFC_MAX_ORDER_VALUE` (rupees) to bound any single order. For MARKET orders
  the value is estimated from live LTP where the token allows it; when it
  can't be estimated the order proceeds with an explicit warning in the
  response.
- **Auto-tag every order.** If you don't pass `external_reference_number`, a
  millisecond-timestamp one is generated so every order fired by this server
  is identifiable in the order book.
- **Honour a kill switch.** `HDFC_READ_ONLY=true` disables all four
  money-moving tools at the server level while keeping every data tool alive —
  run this mode when you only want analysis.

Also: any 401 automatically clears the cached token, so the next call fails
fast with "no session — re-run login" instead of hammering the API with a dead
token. `hdfc_login_status` reports token age and warns when it's likely expired.

**Credential caveat**: `hdfc_login_validate_credentials` necessarily passes your
username/password through the MCP conversation. If that bothers you, do the
browser login flow instead and drop the resulting token into `HDFC_ACCESS_TOKEN`.

## Option chain with Greeks

```
hdfc_get_option_chain(underlying_symbol="NIFTY")
  -> status="need_expiry", available_expiries=[...]   # if more than one expiry exists
hdfc_get_option_chain(underlying_symbol="NIFTY", expiry_date="27-AUG-2026")
  -> chain=[{strike, CE:{security_id, ltp, greeks}, PE:{...}}, ...]
```

There's no single documented option-chain endpoint — InvestRight doesn't have
one. This tool assembles it from three calls: the security master (to
enumerate every strike/CE/PE contract for the expiry), a batched
`hdfc_fetch_ltp` (price), and a short-lived websocket `GREEK` subscription per
contract (delta/gamma/vega/theta/rho). It never guesses an expiry — if more
than one exists it returns the list and asks you to pick one, since the CSV's
date format isn't documented anywhere. If `greeks_received` comes back low or
0, the market is likely closed, or increase `wait_seconds` (default 6s, capped
at 30s) and retry.

Because the actual security-master column names aren't published, the CSV
parser probes a broad set of common aliases (snake_case and NSE-style `SEM_`
naming). If it can't find your underlying at all, the response includes
`csv_columns` — the real header row — so the alias list in
`csv_cache.py::SecurityMasterCache` can be extended for whatever schema
InvestRight actually ships.

## Live market data + Greeks

```
hdfc_stream_start
hdfc_stream_subscribe(scrip_id="NFO_43382", sub_type="GREEK")
hdfc_stream_get_greeks(scrip_id="43382")
```

- `scrip_id` on **subscribe** needs the exchange/segment prefix from the docs'
  prefix table: `NSE_<token>`, `BFO_<token>`, `NFO_<token>`,
  `NSE_INDEX_<token>`, `MCX_<token>`, etc.
- `scrip_id` on read tools accepts either form — prefixed or bare — and every
  snapshot includes `age_seconds` so you can tell live data from stale.
- The connection auto-reconnects with backoff and resubscribes everything
  automatically; `hdfc_stream_status` reports connection/heartbeat health.
- Look up a token first with `hdfc_search_security_master("banknifty",
  exchange="NSE")` rather than guessing it.

## MCP protocol features in use

All 33 tools carry `ToolAnnotations` (`readOnlyHint`, `destructiveHint`,
`idempotentHint`, `openWorldHint`) — a second, protocol-level layer of the
same safety story as the `confirm=true` gates. Clients that understand
annotations can visually flag or gate the five `destructiveHint=true` tools
(`hdfc_place_order`, `hdfc_modify_order`, `hdfc_cancel_order`,
`hdfc_cancel_all_orders`) differently from the 20+ `readOnlyHint=true` ones,
enforced by the client itself rather than only by our own code.

Running `fastmcp` 3.4.6 (current) and the `mcp` SDK 1.29.0 — the latest
version FastMCP itself supports (it pins `mcp<2.0`) — negotiating MCP
protocol version `2025-11-25` (current spec revision, per
`mcp.types.LATEST_PROTOCOL_VERSION`).

## Performance

- **uvloop** is used automatically on Linux/macOS (big win for the websocket
  decode loop), **HTTP/2** is negotiated when available (multiplexes concurrent
  REST calls over one connection), and the connection pool keeps sockets warm.
- Live quotes/Greeks are served from the in-memory websocket snapshot cache —
  reading them costs zero network round trips.
- The security master (potentially 100k+ rows) is parsed off the event loop
  with per-row search haystacks precomputed at load; a full-scan search over
  200k rows measures ~20ms. Exact symbol matches rank above substring hits.

## Known doc inconsistencies (handled defensively)

- **Overall position**: the prose "EndPoint" line says
  `/oapi/v1/portfolio/cumulative-positions`, but the worked curl example uses
  `/oapi/v1/portfolio/overall_positions`. The client tries the curl-verified
  path first and falls back to the other on a 404.
- **Funds & margins**: the "EndPoint" line says `/oapi/v1/user/margins`, the
  curl example drops the `/oapi/v1` prefix. The client tries the documented
  path first, falls back to the bare path on 404.

## Project layout

```
src/hdfc_mcp/
  config.py        env-var settings (HDFC_API_KEY, HDFC_API_SECRET, ...)
  errors.py         typed exceptions for 400/401/403/404/422/5xx
  session.py         on-disk access-token cache
  rest_client.py      async httpx client for every REST endpoint
  csv_cache.py        security-master CSV download + search cache
  market_data_ws.py   websocket client, protobuf decode, live snapshot cache
  proto/               GenericDTO3.proto + compiled generic_dto_pb2.py
  server.py            FastMCP tool definitions (entry point)
```

## Tool reference

| Tool | Purpose |
|---|---|
| `hdfc_login_status` / `hdfc_logout` | Check/clear the cached session |
| `hdfc_login_get_token_id` … `hdfc_login_fetch_access_token` | 5-step login flow |
| `hdfc_fetch_ltp` | Last traded price for one or more instruments |
| `hdfc_search_security_master` | Look up `security_id` by symbol/company name |
| `hdfc_get_option_chain` | Full option chain for an underlying+expiry with live LTP and Greeks per strike |
| `hdfc_get_orders` / `hdfc_get_order` | Order book (all / single) |
| `hdfc_get_trades` / `hdfc_get_order_trades` | Trade book (all / single order) |
| `hdfc_place_order` / `hdfc_modify_order` / `hdfc_cancel_order` | Order placement (confirm=true required) |
| `hdfc_cancel_all_orders` | Panic button: cancel every non-terminal order (confirm=true required) |
| `hdfc_wait_for_order` | Poll an order until Traded/Rejected/Cancelled or timeout |
| `hdfc_get_positions` | Day + carry-forward positions |
| `hdfc_get_positions_with_pnl` | Positions joined with live LTP → per-position and total MTM P&L |
| `hdfc_get_holdings` | Long-term demat holdings |
| `hdfc_get_margins` | Available funds / margin utilisation |
| `hdfc_get_profile` | Account profile |
| `hdfc_stream_start` / `stop` / `status` | Manage the market-data websocket |
| `hdfc_stream_subscribe` / `unsubscribe` / `list_subscriptions` | Manage per-instrument subscriptions |
| `hdfc_stream_get_snapshot` | Latest quote/index/Greeks packet for an instrument |
| `hdfc_stream_get_greeks` | Just the delta/gamma/vega/theta/rho for an option |

## Testing without real credentials

The REST client, protobuf decoding, CSV cache, and session store were all
verified with `httpx.MockTransport` and synthetic protobuf frames (no live
InvestRight account needed) — see the test snippets used during development
in the session history. There is no bundled pytest suite yet; add one under
`tests/` if this grows further.
