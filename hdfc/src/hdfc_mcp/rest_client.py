"""Async REST client for HDFC Securities InvestRight Open API.

Endpoint paths, params, and payload shapes below were extracted directly
from the published docs at https://developer.hdfcsec.com/ir-docs (see
scratchpad research). Two documented endpoints have a mismatch between
their prose "EndPoint" line and their example curl command:

  * Overall position: EndPoint says `/oapi/v1/portfolio/cumulative-positions`,
    the curl example uses `/oapi/v1/portfolio/overall_positions`. We use the
    curl-verified path and expose the other as a fallback on 404.
  * Funds & margins: EndPoint says `/oapi/v1/user/margins`, the curl example
    drops the `/oapi/v1` prefix. We use the documented `/oapi/v1` path and
    fall back to the bare path on 404.

Every write endpoint (place/modify/cancel order) is never retried
automatically — a network blip during an order call must surface as an
error to the caller rather than risk a duplicate/duplicate-cancel action.
Read (GET) calls get a small bounded retry for transient network/5xx
failures.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

import httpx

from .config import Settings
from .errors import (
    AuthenticationError,
    InvestRightError,
    NotFoundError,
    SessionExpiredError,
    error_for_status,
)
from .session import SessionStore

logger = logging.getLogger("hdfc_mcp.rest")

JsonDict = dict[str, Any]

_RETRYABLE_STATUS = {502, 503, 504}
_GET_MAX_ATTEMPTS = 3
_GET_RETRY_BACKOFF = (0.4, 1.2)


class InvestRightClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._store = SessionStore(settings.session_path)
        self._access_token: str | None = settings.static_access_token
        self._login_id: str | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        try:  # HTTP/2 multiplexes concurrent calls over one connection when the server supports it
            import h2  # noqa: F401

            http2 = True
        except ImportError:
            http2 = False
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            http2=http2,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

        if self._access_token is None:
            stored = self._store.load()
            if stored is not None:
                self._access_token = stored.access_token
                self._login_id = stored.login_id

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def http_client(self) -> httpx.AsyncClient:
        return self._http

    # ------------------------------------------------------------------
    # Session / token management
    # ------------------------------------------------------------------

    @property
    def has_access_token(self) -> bool:
        return self._access_token is not None

    def get_access_token_or_none(self) -> str | None:
        return self._access_token

    def session_info(self) -> JsonDict:
        """Metadata about the current session for status reporting (never the token itself)."""
        stored = self._store.load()
        info: JsonDict = {
            "has_access_token": self.has_access_token,
            "source": "env" if self._settings.static_access_token else "login-flow",
        }
        if stored is not None and not self._settings.static_access_token:
            import time

            age_hours = (time.time() - stored.issued_at) / 3600
            info["token_age_hours"] = round(age_hours, 2)
            info["login_id"] = stored.login_id
            # InvestRight sessions are typically valid for the trading day only.
            if age_hours > 20:
                info["warning"] = "Token is over 20h old and has likely expired; re-login recommended."
        return info

    def set_access_token(self, token: str, *, login_id: str | None = None) -> None:
        self._access_token = token
        self._login_id = login_id or self._login_id
        self._store.save(token, login_id=self._login_id)

    def clear_session(self) -> None:
        self._access_token = None
        self._login_id = None
        self._store.clear()

    def _auth_header(self) -> dict[str, str]:
        if not self._access_token:
            raise SessionExpiredError(
                "No access token available. Run the login tools "
                "(hdfc_login_get_token_id -> ... -> hdfc_login_fetch_access_token) "
                "or set HDFC_ACCESS_TOKEN."
            )
        return {"Authorization": self._access_token}

    # ------------------------------------------------------------------
    # Low-level request plumbing
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        *,
        params: JsonDict | None = None,
        json_body: JsonDict | None = None,
        authed: bool = True,
        allow_retry: bool | None = None,
    ) -> JsonDict:
        params = dict(params or {})
        params.setdefault("api_key", self._settings.api_key)

        headers: dict[str, str] = {}
        if authed:
            headers.update(self._auth_header())
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        retry = allow_retry if allow_retry is not None else (method == "GET")
        attempts = _GET_MAX_ATTEMPTS if retry else 1

        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                async with self._semaphore:
                    response = await self._http.request(
                        method, path, params=params, json=json_body, headers=headers
                    )
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(_GET_RETRY_BACKOFF[min(attempt, len(_GET_RETRY_BACKOFF) - 1)])
                    continue
                raise error_for_status(0, f"Network error calling {path}: {exc}") from exc

            if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                await asyncio.sleep(_GET_RETRY_BACKOFF[min(attempt, len(_GET_RETRY_BACKOFF) - 1)])
                continue

            try:
                return self._parse_response(response, path)
            except AuthenticationError as exc:
                # A 401 on an authed call means the cached token is dead. Clear it
                # so the very next tool call reports "no session" instead of
                # failing the same way forever.
                if authed and not isinstance(exc, SessionExpiredError):
                    self.clear_session()
                    raise SessionExpiredError(
                        f"{exc} — cached session cleared; re-run the login flow "
                        "(hdfc_login_get_token_id → … → hdfc_login_fetch_access_token).",
                        status_code=exc.status_code,
                        payload=exc.payload,
                    ) from exc
                raise

        # Unreachable, but keeps type-checkers happy.
        raise error_for_status(0, f"Exhausted retries calling {path}: {last_exc}")

    @staticmethod
    def _parse_response(response: httpx.Response, path: str) -> JsonDict:
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}

        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else None
            message = message or f"{path} failed with HTTP {response.status_code}"
            raise error_for_status(response.status_code, message, payload=body)

        if isinstance(body, dict) and body.get("status") == "error":
            raise error_for_status(
                response.status_code, body.get("message", f"{path} returned an error"), payload=body
            )

        return body if isinstance(body, dict) else {"data": body}

    # ------------------------------------------------------------------
    # Login flow  (docs: fetch_access_token_via_api/*)
    # ------------------------------------------------------------------

    async def login_get_token_id(self) -> str:
        """GET /oapi/v1/login -> {"tokenId": "..."}"""
        body = await self._request("GET", "/oapi/v1/login", authed=False)
        return body["tokenId"]

    async def login_validate_credentials(
        self, token_id: str, username: str, password: str
    ) -> JsonDict:
        """POST /oapi/v1/login/validate -> {loginId, twofa: {questions:[...]}, twoFAEnabled}"""
        body = await self._request(
            "POST",
            "/oapi/v1/login/validate",
            params={"token_id": token_id},
            json_body={"username": username, "password": password},
            authed=False,
        )
        if "loginId" in body:
            self._login_id = body["loginId"]
        return body

    async def login_resend_2fa(self, token_id: str) -> JsonDict:
        """GET /oapi/v1/twofa/resend"""
        return await self._request(
            "GET", "/oapi/v1/twofa/resend", params={"token_id": token_id}, authed=False
        )

    async def login_validate_2fa(self, token_id: str, answer: str) -> JsonDict:
        """POST /oapi/v1/twofa/validate -> {requestToken, termsAndConditions, authorised}"""
        return await self._request(
            "POST",
            "/oapi/v1/twofa/validate",
            params={"token_id": token_id},
            json_body={"answer": answer},
            authed=False,
        )

    async def login_authorize(
        self, token_id: str, request_token: str, consent: str = "Y"
    ) -> JsonDict:
        """GET /oapi/v1/authorise -> {callbackUrl, requestToken}"""
        return await self._request(
            "GET",
            "/oapi/v1/authorise",
            params={"token_id": token_id, "consent": consent, "request_token": request_token},
            authed=False,
        )

    async def login_fetch_access_token(self, request_token: str) -> str:
        """POST /oapi/v1/access-token -> {accessToken}. Persists the token on success."""
        body = await self._request(
            "POST",
            "/oapi/v1/access-token",
            params={"request_token": request_token},
            json_body={"apiSecret": self._settings.api_secret},
            authed=False,
        )
        token = body["accessToken"]
        self.set_access_token(token, login_id=self._login_id)
        return token

    # ------------------------------------------------------------------
    # Market data (REST)
    # ------------------------------------------------------------------

    async def fetch_ltp(self, instruments: list[dict[str, str]]) -> JsonDict:
        """PUT /oapi/v1/fetch-ltp. instruments: [{"exchange": "NSE", "token": "21840"}, ...]"""
        return await self._request(
            "PUT", "/oapi/v1/fetch-ltp", json_body={"data": instruments}, allow_retry=False
        )

    async def fetch_ltp_bulk(
        self, instruments: list[dict[str, str]], *, chunk_size: int = 200
    ) -> list[JsonDict]:
        """fetch_ltp in batches (undocumented per-call limit; chunked defensively).

        Returns the flattened "data" list from every chunk; a chunk that fails
        is skipped rather than aborting the whole batch (best-effort — used for
        option chains where one bad token shouldn't blank out the rest).
        """
        merged: list[JsonDict] = []
        for i in range(0, len(instruments), chunk_size):
            chunk = instruments[i : i + chunk_size]
            try:
                body = await self.fetch_ltp(chunk)
                merged.extend(body.get("data") or [])
            except InvestRightError:
                continue
        return merged

    async def try_fetch_single_ltp(self, exchange: str, token: str) -> float | None:
        """Best-effort LTP for one instrument; None if the lookup fails.

        Used for pre-flight order-value estimation on MARKET orders. Note the
        equity `security_id` used by place_order (e.g. "WIPLTDEQNR") is not
        always the numeric token fetch-ltp expects, so failure here is normal
        and must not block the caller.
        """
        try:
            body = await self.fetch_ltp([{"exchange": exchange, "token": str(token)}])
            data = body.get("data") or []
            if data and data[0].get("ltp") is not None:
                return float(data[0]["ltp"])
        except Exception:  # noqa: BLE001 — estimation only, never fatal
            pass
        return None

    def security_master_csv_url(self) -> str:
        """Direct-download CSV of the full instrument/security master."""
        return f"{self._settings.base_url}/oapi/v1/security-master?api_key={self._settings.api_key}"

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def place_order(self, order: JsonDict) -> JsonDict:
        """POST /oapi/v1/orders/regular -> {order_id}. Never auto-retried."""
        return await self._request(
            "POST", "/oapi/v1/orders/regular", json_body=order, allow_retry=False
        )

    async def modify_order(self, order_id: str, changes: JsonDict) -> JsonDict:
        """PUT /oapi/v1/orders/regular/:order_id -> {order_id}. Never auto-retried."""
        return await self._request(
            "PUT",
            f"/oapi/v1/orders/regular/{order_id}",
            json_body=changes,
            allow_retry=False,
        )

    async def cancel_order(self, order_id: str) -> JsonDict:
        """DELETE /oapi/v1/orders/regular/:order_id -> {order_id}. Never auto-retried."""
        return await self._request(
            "DELETE", f"/oapi/v1/orders/regular/{order_id}", allow_retry=False
        )

    async def get_orders(self) -> JsonDict:
        """GET /oapi/v1/orders -> all of today's orders."""
        return await self._request("GET", "/oapi/v1/orders")

    async def get_order(self, order_id: str) -> JsonDict:
        """GET /oapi/v1/orders/:order_id -> single order status."""
        return await self._request("GET", f"/oapi/v1/orders/{order_id}")

    async def get_trades(self) -> JsonDict:
        """GET /oapi/v1/trades -> all of today's trades."""
        return await self._request("GET", "/oapi/v1/trades")

    async def get_order_trades(self, order_id: str) -> JsonDict:
        """GET /oapi/v1/orders/:order_id/trades -> trades for one order."""
        return await self._request("GET", f"/oapi/v1/orders/{order_id}/trades")

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    async def get_positions(self) -> JsonDict:
        """GET /oapi/v1/portfolio/overall_positions -> day + carry-forward positions."""
        try:
            return await self._request("GET", "/oapi/v1/portfolio/overall_positions")
        except NotFoundError:
            return await self._request("GET", "/oapi/v1/portfolio/cumulative-positions")

    async def get_holdings(self) -> JsonDict:
        """GET /oapi/v1/portfolio/holdings -> long-term demat holdings."""
        return await self._request("GET", "/oapi/v1/portfolio/holdings")

    async def get_margins(self) -> JsonDict:
        """GET /oapi/v1/user/margins -> available funds and margin utilisation."""
        try:
            return await self._request("GET", "/oapi/v1/user/margins")
        except NotFoundError:
            return await self._request("GET", "/user/margins")

    async def get_profile(self) -> JsonDict:
        """POST /oapi/v3/user/profile -> account/user profile."""
        return await self._request("POST", "/oapi/v3/user/profile")
