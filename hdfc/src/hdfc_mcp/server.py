"""FastMCP server exposing HDFC Securities InvestRight Open API as tools.

Run with:  python -m hdfc_mcp.server
Requires HDFC_API_KEY and HDFC_API_SECRET in the environment (see .env.example).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import ConfigError, Settings
from .csv_cache import SecurityMasterCache
from .errors import InvestRightError
from .market_data_ws import MarketDataStream
from .rest_client import InvestRightClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hdfc_mcp.server")

mcp = FastMCP(
    name="hdfc-investright",
    instructions=(
        "Tools for HDFC Securities InvestRight Open API: login/session, live LTP, "
        "the order book/trade book/positions/holdings/funds, order placement, and "
        "a live market-data websocket feed that includes options Greeks "
        "(delta/gamma/vega/theta/rho) — something the plain REST API does not expose. "
        "Order-placing tools require confirm=true; call them once without confirm to "
        "preview, then again with confirm=true to actually fire."
    ),
)

_settings: Settings | None = None
_client: InvestRightClient | None = None
_stream: MarketDataStream | None = None
_security_master: SecurityMasterCache | None = None
_init_lock = asyncio.Lock()


async def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


async def _get_client() -> InvestRightClient:
    global _client
    async with _init_lock:
        if _client is None:
            settings = await _get_settings()
            _client = InvestRightClient(settings)
    return _client


async def _get_stream() -> MarketDataStream:
    global _stream
    if _stream is not None:
        return _stream
    # _get_client() takes _init_lock itself, so it must be called before we
    # take it here — asyncio.Lock isn't reentrant, and nesting them deadlocks.
    settings = await _get_settings()
    client = await _get_client()
    async with _init_lock:
        if _stream is None:
            _stream = MarketDataStream(settings, client.get_access_token_or_none)
    return _stream


async def _get_security_master() -> tuple[SecurityMasterCache, InvestRightClient]:
    global _security_master
    client = await _get_client()
    if _security_master is None:
        settings = await _get_settings()
        _security_master = SecurityMasterCache(settings.session_path.parent / "security_master.csv")
    return _security_master, client


def _envelope(status: str, **kwargs: Any) -> dict[str, Any]:
    return {"status": status, **kwargs}


def _ok(body: dict[str, Any]) -> dict[str, Any]:
    """Wrap an API response body, preserving its own status under api_status.

    The InvestRight envelope carries "status": "success" itself, which would
    otherwise collide with our envelope's status key.
    """
    out: dict[str, Any] = {"status": "ok"}
    for key, value in body.items():
        out["api_status" if key == "status" else key] = value
    return out


def _from_error(exc: InvestRightError) -> dict[str, Any]:
    return _envelope(
        "error",
        error_type=type(exc).__name__,
        message=str(exc),
        http_status=exc.status_code,
        details=exc.payload,
    )


# ======================================================================
# Login / session tools
# ======================================================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="Login Status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_login_status() -> dict[str, Any]:
    """Check whether a usable InvestRight access token is cached, its age, and server mode."""
    client = await _get_client()
    settings = await _get_settings()
    return _envelope("ok", **client.session_info(), read_only=settings.read_only)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Log Out",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_logout() -> dict[str, Any]:
    """Discard the cached access token, forcing a fresh login next time."""
    client = await _get_client()
    client.clear_session()
    return _envelope("ok", message="Session cleared.")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Start Login (Token ID)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_login_get_token_id() -> dict[str, Any]:
    """Step 1 of login: obtain a token_id to anchor the rest of the login flow."""
    client = await _get_client()
    try:
        token_id = await client.login_get_token_id()
        return _envelope("ok", token_id=token_id)
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Submit Login Credentials",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_login_validate_credentials(
    token_id: str, username: str, password: str
) -> dict[str, Any]:
    """Step 2: submit username/password for the given token_id.

    Returns the 2FA question(s) to present to the user — do not guess or
    fabricate an answer, ask the human for the real OTP/answer.
    """
    client = await _get_client()
    try:
        body = await client.login_validate_credentials(token_id, username, password)
        return _ok(body)
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Resend 2FA Code",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_login_resend_2fa(token_id: str) -> dict[str, Any]:
    """Step 2b (optional): ask InvestRight to resend the 2FA/OTP code."""
    client = await _get_client()
    try:
        body = await client.login_resend_2fa(token_id)
        return _ok(body)
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Submit 2FA Code",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_login_validate_2fa(token_id: str, answer: str) -> dict[str, Any]:
    """Step 3: submit the 2FA/OTP answer. `answer` must come from the human user."""
    client = await _get_client()
    try:
        body = await client.login_validate_2fa(token_id, answer)
        return _ok(body)
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Authorize Login",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_login_authorize(
    token_id: str, request_token: str, consent: str = "Y"
) -> dict[str, Any]:
    """Step 4: authorize the app to receive a fresh request token."""
    client = await _get_client()
    try:
        body = await client.login_authorize(token_id, request_token, consent)
        return _ok(body)
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Fetch Access Token",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_login_fetch_access_token(request_token: str) -> dict[str, Any]:
    """Step 5 (final): exchange the request token for an access token.

    On success the access token is cached on disk (user-only permissions)
    and every subsequent tool call will use it automatically.
    """
    client = await _get_client()
    try:
        await client.login_fetch_access_token(request_token)
        return _envelope("ok", message="Access token acquired and cached.")
    except InvestRightError as exc:
        return _from_error(exc)


# ======================================================================
# Market data (REST)
# ======================================================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="Fetch LTP",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_fetch_ltp(instruments: list[dict[str, str]]) -> dict[str, Any]:
    """Fetch last traded price for one or more instruments.

    instruments: list of {"exchange": "NSE"|"BSE", "token": "<security_id>"}.
    Look up tokens first with hdfc_search_security_master if you only know the symbol.
    """
    client = await _get_client()
    try:
        return _ok(await client.fetch_ltp(instruments))
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Security Master",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_search_security_master(
    query: str, exchange: str | None = None, limit: int = 20, refresh: bool = False
) -> dict[str, Any]:
    """Search the full instrument/security master by symbol or company name substring.

    Downloads and caches the CSV (refreshed every 6h, or immediately if refresh=true).
    Use this to find the exact `security_id` / token required by hdfc_place_order
    and hdfc_fetch_ltp — never guess a security_id.
    """
    cache, client = await _get_security_master()
    try:
        count = await cache.ensure_loaded(client.http_client, client.security_master_csv_url(), force=refresh)
        matches = cache.search(query, exchange=exchange, limit=limit)
        return _envelope("ok", total_instruments=count, matches=matches)
    except Exception as exc:  # noqa: BLE001
        return _envelope("error", message=f"Failed to load/search security master: {exc}")


_WS_PREFIX_BY_EXCHANGE = {"NSE": "NFO_", "BSE": "BFO_"}


def _sort_key_strike(strike: str) -> float:
    try:
        return float(strike)
    except (TypeError, ValueError):
        return float("inf")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Option Chain (with Greeks)",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_option_chain(
    underlying_symbol: str,
    exchange: Literal["NSE", "BSE"] = "NSE",
    expiry_date: str | None = None,
    wait_seconds: float = 6.0,
    refresh_security_master: bool = False,
) -> dict[str, Any]:
    """Build an option chain for underlying_symbol with live LTP and Greeks per strike.

    This isn't a single documented endpoint — InvestRight has no REST option-chain
    API. It's assembled from three pieces: the security master (to enumerate every
    strike/CE/PE contract for the expiry), a batched hdfc_fetch_ltp call (for
    price), and a short-lived websocket GREEK subscription per contract (for
    delta/gamma/vega/theta/rho — the data Kite doesn't expose at all).

    If expiry_date is omitted and more than one expiry exists for this
    underlying, returns status="need_expiry" with the list of available
    expiries instead of guessing one — call again with expiry_date set to
    one of those exact strings (the security master's own date format,
    whatever that turns out to be, since it isn't documented anywhere).

    wait_seconds controls how long to listen for GREEK packets to arrive
    after subscribing (market must be open for them to arrive at all).
    Increase it if greeks_received comes back 0 or low.
    """
    cache, client = await _get_security_master()
    try:
        await cache.ensure_loaded(
            client.http_client, client.security_master_csv_url(), force=refresh_security_master
        )
    except Exception as exc:  # noqa: BLE001
        return _envelope("error", message=f"Failed to load security master: {exc}")

    candidates_all = cache.option_chain_candidates(underlying_symbol, exchange=exchange)
    if not candidates_all:
        return _envelope(
            "empty",
            message=(
                f"No option contracts found for underlying_symbol='{underlying_symbol}' on {exchange}. "
                "The security master's column names may not match what this tool looks for "
                "(see csv_columns below) — try hdfc_search_security_master to inspect raw rows."
            ),
            csv_columns=cache.columns(),
        )

    if expiry_date:
        needle = expiry_date.strip()
        candidates = [c for c in candidates_all if needle in c["expiry_date"]]
        if not candidates:
            return _envelope(
                "error",
                message=f"No contracts matched expiry_date '{expiry_date}' for {underlying_symbol}.",
                available_expiries=cache.distinct_expiries(candidates_all),
            )
    else:
        expiries = cache.distinct_expiries(candidates_all)
        if len(expiries) > 1:
            return _envelope(
                "need_expiry",
                message="Multiple expiries available; call again with expiry_date set to one of these.",
                available_expiries=expiries,
            )
        candidates = candidates_all
        expiry_date = expiries[0] if expiries else None

    seen_ids: set[str] = set()
    unique: list[dict[str, str]] = []
    for c in candidates:
        sid = c["security_id"]
        if sid and sid not in seen_ids:
            seen_ids.add(sid)
            unique.append(c)
    candidates = unique
    if not candidates:
        return _envelope(
            "error",
            message="Matched contracts but none had a recognizable security_id column.",
            csv_columns=cache.columns(),
        )

    ltp_items = await client.fetch_ltp_bulk(
        [{"exchange": c["exchange"] or exchange, "token": c["security_id"]} for c in candidates]
    )
    ltp_map: dict[str, float] = {}
    for item in ltp_items:
        if item.get("ltp") is not None:
            ltp_map[str(item.get("token"))] = float(item["ltp"])

    stream = await _get_stream()
    if not stream.is_running:
        await stream.start()
    prefix = _WS_PREFIX_BY_EXCHANGE.get(exchange, "NFO_")
    for c in candidates:
        await stream.subscribe(f"{prefix}{c['security_id']}", "GREEK")

    await asyncio.sleep(max(0.0, min(wait_seconds, 30.0)))

    chain: dict[str, dict[str, Any]] = {}
    greeks_received = 0
    for c in candidates:
        strike = c["strike_price"] or "unknown"
        leg = chain.setdefault(strike, {"strike": strike, "CE": None, "PE": None})
        snapshot = stream.get_snapshot(c["security_id"])
        greeks = None
        if snapshot and "greekData" in snapshot.get("data", {}):
            greeks = snapshot["data"]["greekData"]
            greeks_received += 1
        entry = {
            "security_id": c["security_id"],
            "symbol": c["symbol"],
            "ltp": ltp_map.get(c["security_id"]),
            "greeks": greeks,
        }
        if c["option_type"] in ("CE", "CALL"):
            leg["CE"] = entry
        elif c["option_type"] in ("PE", "PUT"):
            leg["PE"] = entry

    rows = sorted(chain.values(), key=lambda r: _sort_key_strike(r["strike"]))

    warnings: list[str] = []
    if greeks_received == 0:
        warnings.append(
            "No Greek packets arrived within wait_seconds. Market may be closed, or "
            "increase wait_seconds and retry — subscriptions from this call remain active."
        )

    return _envelope(
        "ok",
        underlying_symbol=underlying_symbol,
        exchange=exchange,
        expiry_date=expiry_date,
        contracts=len(candidates),
        greeks_received=greeks_received,
        chain=rows,
        warnings=warnings,
    )


# ======================================================================
# Orders (read)
# ======================================================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Orders",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_orders() -> dict[str, Any]:
    """List every order placed today."""
    client = await _get_client()
    try:
        return _ok(await client.get_orders())
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Order",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_order(order_id: str) -> dict[str, Any]:
    """Get the status/detail of a single order by order_id."""
    client = await _get_client()
    try:
        return _ok(await client.get_order(order_id))
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Trades",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_trades() -> dict[str, Any]:
    """List every trade (fill) executed today."""
    client = await _get_client()
    try:
        return _ok(await client.get_trades())
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Order Trades",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_order_trades(order_id: str) -> dict[str, Any]:
    """List the trade(s)/fill(s) for a specific order_id."""
    client = await _get_client()
    try:
        return _ok(await client.get_order_trades(order_id))
    except InvestRightError as exc:
        return _from_error(exc)


# ======================================================================
# Orders (write) — always require explicit confirm=true
# ======================================================================


def _require_confirm(confirm: bool, action_summary: dict[str, Any]) -> dict[str, Any] | None:
    if confirm:
        return None
    return _envelope(
        "preview",
        message=(
            "This would place/modify/cancel a real order but confirm=false. "
            "Review the details below, then call again with confirm=true to execute."
        ),
        would_send=action_summary,
    )


async def _write_guard() -> dict[str, Any] | None:
    """Hard kill switch: HDFC_READ_ONLY=true disables every money-moving tool."""
    settings = await _get_settings()
    if settings.read_only:
        return _envelope(
            "error",
            message="Server is running in read-only mode (HDFC_READ_ONLY=true); order tools are disabled.",
        )
    return None


# Recently fired order fingerprints, to stop an accidental double-submit
# (e.g. an LLM retrying a place_order call that actually succeeded).
_recent_order_fingerprints: dict[str, float] = {}


def _duplicate_check(order: dict[str, Any], window_seconds: float) -> float | None:
    """Return seconds since an identical order was fired inside the window, else None."""
    fingerprint_src = {k: v for k, v in order.items() if k != "external_reference_number"}
    fingerprint = json.dumps(fingerprint_src, sort_keys=True)
    now = time.time()
    for key in [k for k, ts in _recent_order_fingerprints.items() if now - ts > window_seconds]:
        _recent_order_fingerprints.pop(key, None)
    fired_at = _recent_order_fingerprints.get(fingerprint)
    if fired_at is not None:
        return now - fired_at
    return None


def _record_order_fired(order: dict[str, Any]) -> None:
    fingerprint_src = {k: v for k, v in order.items() if k != "external_reference_number"}
    _recent_order_fingerprints[json.dumps(fingerprint_src, sort_keys=True)] = time.time()


@mcp.tool(
    annotations=ToolAnnotations(
        title="Place Order (real money)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_place_order(
    exchange: Literal["NSE", "BSE"],
    security_id: str,
    instrument_segment: str,
    transaction_type: Literal["Buy", "Sell"],
    product: Literal["DELIVERY", "OVERNIGHT", "INTRADAY", "MTF", "COLL-SELL", "ENCASH"],
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"],
    quantity: int,
    price: float = 0,
    trigger_price: float = 0,
    disclosed_quantity: int = 0,
    validity: Literal["DAY", "IOC", "GTD"] = "DAY",
    amo: bool = False,
    external_reference_number: int | None = None,
    underlying_symbol: str | None = None,
    expiry_date: str | None = None,
    strike_price: float | None = None,
    option_type: str | None = None,
    confirm: bool = False,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """Place a real order via POST /oapi/v1/orders/regular. THIS MOVES REAL MONEY.

    security_id must be the exact exchange token from hdfc_search_security_master
    (or Security Master CSV) — not the display symbol.

    For futures (instrument_segment FUTIDX/FUTSTK/FUTCUR): expiry_date is required
    (format YYYYMMDD, per the docs' worked examples).
    For options (OPTIDX/OPTSTK/OPTCUR): expiry_date, strike_price and option_type
    (e.g. "CE"/"PE") are all required, and underlying_symbol is expected too.

    Call once with confirm=false (default) to preview the exact payload, then
    call again with confirm=true to actually submit it. Never set confirm=true
    without the human explicitly approving this specific order.

    Safety rails: an identical order fired within the last few seconds is
    rejected unless allow_duplicate=true; optional HDFC_MAX_ORDER_VALUE /
    HDFC_MAX_ORDER_QTY caps are enforced before anything is sent; if an
    external_reference_number isn't given one is auto-generated so the order
    is traceable in the order book.
    """
    guard = await _write_guard()
    if guard is not None:
        return guard

    segment = instrument_segment.upper()
    is_future = segment in {"FUTIDX", "FUTSTK", "FUTCUR", "FUTIVX", "FUTCOM"}
    is_option = segment in {"OPTIDX", "OPTSTK", "OPTCUR", "OPTFUT", "OPTCOM"}

    validation_errors = []
    if (is_future or is_option) and not expiry_date:
        validation_errors.append(f"expiry_date is required for instrument_segment={segment}")
    if is_option and strike_price is None:
        validation_errors.append(f"strike_price is required for instrument_segment={segment}")
    if is_option and not option_type:
        validation_errors.append(f"option_type is required for instrument_segment={segment}")
    if order_type in ("LIMIT", "SL") and price <= 0:
        validation_errors.append(f"price must be > 0 for order_type={order_type}")
    if order_type in ("SL", "SL-M") and trigger_price <= 0:
        validation_errors.append(f"trigger_price must be > 0 for order_type={order_type}")
    if quantity <= 0:
        validation_errors.append("quantity must be > 0")

    settings = await _get_settings()
    if settings.max_order_qty and quantity > settings.max_order_qty:
        validation_errors.append(
            f"quantity {quantity} exceeds HDFC_MAX_ORDER_QTY={settings.max_order_qty}"
        )

    if validation_errors:
        return _envelope("error", message="Order rejected before sending (safety check).", validation_errors=validation_errors)

    warnings: list[str] = []
    if settings.max_order_value:
        est_price = price if price > 0 else (trigger_price if trigger_price > 0 else None)
        if est_price is None:
            client = await _get_client()
            est_price = await client.try_fetch_single_ltp(exchange, security_id)
        if est_price is None:
            warnings.append(
                "Could not estimate order value for this MARKET order (LTP lookup failed); "
                f"HDFC_MAX_ORDER_VALUE={settings.max_order_value:g} was NOT enforced."
            )
        elif est_price * quantity > settings.max_order_value:
            return _envelope(
                "error",
                message="Order rejected before sending (safety check).",
                validation_errors=[
                    f"estimated order value {est_price * quantity:,.2f} exceeds "
                    f"HDFC_MAX_ORDER_VALUE={settings.max_order_value:g}"
                ],
            )

    order: dict[str, Any] = {
        "exchange": exchange,
        "security_id": security_id,
        "instrument_segment": segment,
        "transaction_type": transaction_type,
        "product": product,
        "order_type": order_type,
        "price": price,
        "trigger_price": trigger_price,
        "quantity": quantity,
        "disclosed_quantity": disclosed_quantity,
        "validity": validity,
        "amo": amo,
    }
    # Auto-generate a reference number (numeric, fits the documented max length
    # of 20) so every order fired by this server is traceable in the order book.
    order["external_reference_number"] = (
        external_reference_number if external_reference_number is not None else int(time.time() * 1000)
    )
    if underlying_symbol is not None:
        order["underlying_symbol"] = underlying_symbol
    if expiry_date is not None:
        order["expiry_date"] = expiry_date
    if strike_price is not None:
        order["strike_price"] = strike_price
    if option_type is not None:
        order["option_type"] = option_type

    preview = _require_confirm(confirm, order)
    if preview is not None:
        if warnings:
            preview["warnings"] = warnings
        return preview

    if not allow_duplicate:
        seconds_ago = _duplicate_check(order, settings.duplicate_order_window_seconds)
        if seconds_ago is not None:
            return _envelope(
                "error",
                message=(
                    f"An identical order was already submitted {seconds_ago:.1f}s ago. "
                    "If this really is a second intentional order, call again with "
                    "allow_duplicate=true. If you were retrying after a timeout, check "
                    "hdfc_get_orders first — the original may have gone through."
                ),
            )

    client = await _get_client()
    try:
        result = await client.place_order(order)
        _record_order_fired(order)
        response = _ok(result)
        if warnings:
            response["warnings"] = warnings
        return response
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Modify Order (real money)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def hdfc_modify_order(
    order_id: str,
    quantity: int | None = None,
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] | None = None,
    price: float | None = None,
    trigger_price: float | None = None,
    disclosed_quantity: int | None = None,
    product: str | None = None,
    validity: Literal["DAY", "IOC", "GTD"] | None = None,
    amo: bool | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modify a pending order via PUT /oapi/v1/orders/regular/:order_id. THIS MOVES REAL MONEY.

    Only pending orders can be modified. Only fields you pass are changed.
    Call once with confirm=false to preview, then confirm=true to execute.
    """
    changes: dict[str, Any] = {}
    for key, value in (
        ("quantity", quantity),
        ("order_type", order_type),
        ("price", price),
        ("trigger_price", trigger_price),
        ("disclosed_quantity", disclosed_quantity),
        ("product", product),
        ("validity", validity),
        ("amo", amo),
    ):
        if value is not None:
            changes[key] = value

    guard = await _write_guard()
    if guard is not None:
        return guard

    if not changes:
        return _envelope("error", message="No fields provided to modify.")

    preview = _require_confirm(confirm, {"order_id": order_id, **changes})
    if preview is not None:
        return preview

    client = await _get_client()
    try:
        return _ok(await client.modify_order(order_id, changes))
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Cancel Order (real money)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_cancel_order(order_id: str, confirm: bool = False) -> dict[str, Any]:
    """Cancel a pending order via DELETE /oapi/v1/orders/regular/:order_id.

    Call once with confirm=false to preview, then confirm=true to execute.
    """
    guard = await _write_guard()
    if guard is not None:
        return guard

    preview = _require_confirm(confirm, {"order_id": order_id})
    if preview is not None:
        return preview

    client = await _get_client()
    try:
        return _ok(await client.cancel_order(order_id))
    except InvestRightError as exc:
        return _from_error(exc)


_TERMINAL_ORDER_STATUSES = {
    "traded", "cancelled", "canceled", "rejected", "complete", "completed", "expired",
}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Cancel All Orders (real money)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_cancel_all_orders(confirm: bool = False) -> dict[str, Any]:
    """Panic button: cancel every order not already in a terminal state.

    Fetches today's order book, filters out orders whose status is already
    Traded/Cancelled/Rejected/Complete/Expired, and cancels the rest one by one.
    Call with confirm=false first to see exactly which orders would be cancelled.
    Note this only cancels pending ORDERS — it does not square off open positions.
    """
    guard = await _write_guard()
    if guard is not None:
        return guard

    client = await _get_client()
    try:
        book = await client.get_orders()
    except InvestRightError as exc:
        return _from_error(exc)

    orders = book.get("data") or []
    candidates = [
        o for o in orders
        if str(o.get("status", "")).strip().lower() not in _TERMINAL_ORDER_STATUSES
    ]
    summary = [
        {
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "company_name": o.get("company_name"),
            "transaction_type": o.get("transaction_type"),
            "quantity": o.get("quantity"),
            "pending_quantity": o.get("pending_quantity"),
        }
        for o in candidates
    ]
    if not candidates:
        return _envelope("ok", message="No cancellable orders found.", orders_checked=len(orders))

    preview = _require_confirm(confirm, {"orders_to_cancel": summary})
    if preview is not None:
        return preview

    results = []
    for o in candidates:
        order_id = str(o.get("order_id"))
        try:
            await client.cancel_order(order_id)
            results.append({"order_id": order_id, "result": "cancelled"})
        except InvestRightError as exc:
            results.append({"order_id": order_id, "result": "error", "message": str(exc)})
    failed = sum(1 for r in results if r["result"] == "error")
    return _envelope(
        "ok" if failed == 0 else "partial",
        cancelled=len(results) - failed,
        failed=failed,
        results=results,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Wait For Order Fill",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_wait_for_order(
    order_id: str, timeout_seconds: float = 30, poll_interval_seconds: float = 2
) -> dict[str, Any]:
    """Poll a single order until it reaches a terminal state (Traded/Rejected/Cancelled) or timeout.

    Use right after placing an order to confirm the fill instead of assuming it.
    Returns the last-seen order detail either way, with waited_seconds and final=true/false.
    """
    timeout_seconds = min(max(timeout_seconds, 1.0), 300.0)
    poll_interval_seconds = min(max(poll_interval_seconds, 0.5), 10.0)
    client = await _get_client()
    started = time.monotonic()
    last_detail: dict[str, Any] | None = None

    while True:
        try:
            body = await client.get_order(order_id)
            data = body.get("data") or []
            last_detail = data[0] if isinstance(data, list) and data else body
            status = str((last_detail or {}).get("status", "")).strip().lower()
            if status in _TERMINAL_ORDER_STATUSES:
                return _envelope(
                    "ok",
                    final=True,
                    waited_seconds=round(time.monotonic() - started, 1),
                    order=last_detail,
                )
        except InvestRightError as exc:
            return _from_error(exc)

        if time.monotonic() - started >= timeout_seconds:
            return _envelope(
                "timeout",
                final=False,
                waited_seconds=round(time.monotonic() - started, 1),
                message=f"Order did not reach a terminal state within {timeout_seconds:g}s.",
                order=last_detail,
            )
        await asyncio.sleep(poll_interval_seconds)


# ======================================================================
# Portfolio
# ======================================================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Positions",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_positions() -> dict[str, Any]:
    """Get overall (day + carry-forward) F&O/equity positions."""
    client = await _get_client()
    try:
        return _ok(await client.get_positions())
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Positions With P&L",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_positions_with_pnl() -> dict[str, Any]:
    """Positions joined with live LTP: adds ltp, unrealized_pnl (MTM estimate) per position and totals.

    unrealized_pnl = (ltp - avg_buy) * net_qty for longs, (avg_sell - ltp) * |net_qty| for shorts.
    LTP lookup only works where security_id is a numeric exchange token (all F&O,
    most non-equity); positions we can't price are returned with ltp=null.
    """
    client = await _get_client()
    try:
        body = await client.get_positions()
    except InvestRightError as exc:
        return _from_error(exc)

    data = body.get("data") or {}
    positions = data.get("net") if isinstance(data, dict) else data
    if not isinstance(positions, list):
        positions = []

    priceable = [
        p for p in positions
        if str(p.get("security_id", "")).isdigit() and p.get("exchange")
    ]
    ltp_map: dict[tuple[str, str], float] = {}
    if priceable:
        try:
            ltp_body = await client.fetch_ltp(
                [{"exchange": p["exchange"], "token": str(p["security_id"])} for p in priceable]
            )
            for item in ltp_body.get("data") or []:
                if item.get("ltp") is not None:
                    ltp_map[(str(item.get("exchange")), str(item.get("token")))] = float(item["ltp"])
        except InvestRightError:
            pass  # positions still returned, just without MTM

    enriched = []
    total_unrealized = 0.0
    total_realized = 0.0
    unpriced = 0
    for p in positions:
        entry = dict(p)
        ltp = ltp_map.get((str(p.get("exchange")), str(p.get("security_id"))))
        entry["ltp"] = ltp
        net_qty = float(p.get("net_qty") or 0)
        if ltp is not None and net_qty != 0:
            if net_qty > 0:
                unrealized = (ltp - float(p.get("average_buy_price") or 0)) * net_qty
            else:
                unrealized = (float(p.get("average_sell_price") or 0) - ltp) * abs(net_qty)
            entry["unrealized_pnl"] = round(unrealized, 2)
            total_unrealized += unrealized
        else:
            entry["unrealized_pnl"] = None
            if net_qty != 0:
                unpriced += 1
        realized = p.get("realised_pl_overall_position")
        if realized is not None:
            total_realized += float(realized)
        enriched.append(entry)

    return _envelope(
        "ok",
        positions=enriched,
        totals={
            "unrealized_pnl": round(total_unrealized, 2),
            "realized_pnl": round(total_realized, 2),
            "open_positions_without_ltp": unpriced,
        },
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Holdings",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_holdings() -> dict[str, Any]:
    """Get long-term demat equity holdings/portfolio."""
    client = await _get_client()
    try:
        return _ok(await client.get_holdings())
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Margins",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_margins() -> dict[str, Any]:
    """Get available funds and margin utilisation."""
    client = await _get_client()
    try:
        return _ok(await client.get_margins())
    except InvestRightError as exc:
        return _from_error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Profile",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_get_profile() -> dict[str, Any]:
    """Get the logged-in user's account profile (exchanges/products/order types enabled)."""
    client = await _get_client()
    try:
        return _ok(await client.get_profile())
    except InvestRightError as exc:
        return _from_error(exc)


# ======================================================================
# Live market-data websocket (quotes + Greeks)
# ======================================================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="Start Market Data Stream",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_start() -> dict[str, Any]:
    """Start the background market-data websocket connection (auto-reconnects).

    Required before hdfc_stream_subscribe / hdfc_stream_get_snapshot return live data.
    """
    stream = await _get_stream()
    await stream.start()
    return _envelope("ok", message="Market data stream starting.")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Stop Market Data Stream",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_stop() -> dict[str, Any]:
    """Stop the background market-data websocket connection."""
    stream = await _get_stream()
    await stream.stop()
    return _envelope("ok", message="Market data stream stopped.")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Stream Status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_status() -> dict[str, Any]:
    """Get connection status and heartbeat timing for the market-data stream."""
    stream = await _get_stream()
    return _envelope("ok", **stream.connection_status())


@mcp.tool(
    annotations=ToolAnnotations(
        title="Subscribe To Instrument",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_subscribe(
    scrip_id: str, sub_type: Literal["ALL", "LTP", "GREEK"] = "ALL"
) -> dict[str, Any]:
    """Subscribe to live updates for one instrument.

    scrip_id must include the exchange/segment prefix, e.g. "NSE_2885" (cash),
    "NFO_43382" (NSE F&O), "BFO_3004" (BSE F&O), "NSE_INDEX_26000", "MCX_<token>".
    Use sub_type="GREEK" on an NFO/BFO options scrip_id to receive delta/gamma/
    vega/theta/rho — this is the data Kite does NOT provide.
    Call hdfc_stream_start first if the stream isn't already running.
    """
    stream = await _get_stream()
    if not stream.is_running:
        await stream.start()
    await stream.subscribe(scrip_id, sub_type)
    return _envelope("ok", message=f"Subscribed to {scrip_id} ({sub_type}).")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Unsubscribe From Instrument",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_unsubscribe(scrip_id: str, sub_type: str | None = None) -> dict[str, Any]:
    """Unsubscribe from one instrument (all types, or a specific sub_type)."""
    stream = await _get_stream()
    await stream.unsubscribe(scrip_id, sub_type)
    return _envelope("ok", message=f"Unsubscribed from {scrip_id}.")


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Stream Subscriptions",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_list_subscriptions() -> dict[str, Any]:
    """List every scrip_id currently subscribed on the market-data stream."""
    stream = await _get_stream()
    return _envelope("ok", subscriptions=stream.list_subscriptions())


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Live Snapshot",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_get_snapshot(scrip_id: str) -> dict[str, Any]:
    """Get the latest cached packet(s) for a scrip_id: quote data, index data, and/or Greeks.

    Returns whatever packet types have arrived since subscribing — check the
    "data" key for "mbpData" (quote/depth), "indexData", or "greekData"
    (delta/gamma/vega/theta/rho).
    """
    stream = await _get_stream()
    snapshot = stream.get_snapshot(scrip_id)
    if snapshot is None:
        return _envelope(
            "empty",
            message=f"No data yet for {scrip_id}. Subscribe first and allow a moment for packets to arrive.",
        )
    return _envelope("ok", **snapshot)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Live Greeks",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def hdfc_stream_get_greeks(scrip_id: str) -> dict[str, Any]:
    """Convenience wrapper: return only the Greeks (delta/gamma/vega/theta/rho) for a scrip_id.

    scrip_id must be an NFO/BFO options instrument subscribed with sub_type="GREEK" or "ALL".
    """
    stream = await _get_stream()
    snapshot = stream.get_snapshot(scrip_id)
    if snapshot is None or "greekData" not in snapshot.get("data", {}):
        return _envelope(
            "empty",
            message=(
                f"No Greek data yet for {scrip_id}. Subscribe with sub_type='GREEK' "
                "(or 'ALL') on an options instrument and allow a moment for packets to arrive."
            ),
        )
    return _envelope(
        "ok",
        scripId=scrip_id,
        greeks=snapshot["data"]["greekData"],
        updated_at=snapshot["updated_at"].get("greekData"),
    )


def main() -> None:
    try:
        Settings.load()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    try:  # uvloop meaningfully speeds up the websocket decode loop on Linux
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    mcp.run()


if __name__ == "__main__":
    main()
