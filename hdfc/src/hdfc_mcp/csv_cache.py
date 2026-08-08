"""In-memory + on-disk cache for the Security Master CSV.

The security master is a large, slow-changing CSV of every tradable
instrument. Placing an order requires the exact `security_id` (exchange
token), so a fast local search over this file is the difference between
"guess the token" and "look it up by symbol" — this is the #1 way an
order can go wrong if skipped.

Performance notes: the CSV can run to hundreds of thousands of rows, so we
parse it off the event loop, precompute one lowercase haystack string per
row at load time (instead of joining every row's values on every query),
and rank exact-field matches above substring matches.
"""
from __future__ import annotations

import asyncio
import csv
import io
import time
from pathlib import Path
from typing import Any

import httpx

_CACHE_TTL_SECONDS = 6 * 60 * 60  # security master changes rarely intraday
_DOWNLOAD_TIMEOUT = httpx.Timeout(90.0)  # the CSV is large; don't inherit the 10s API timeout


class SecurityMasterCache:
    def __init__(self, cache_path: Path):
        self._cache_path = cache_path
        self._rows: list[dict[str, str]] | None = None
        self._rows_lc: list[dict[str, str]] | None = None  # same rows, lowercased keys
        self._haystacks: list[str] | None = None
        self._columns: list[str] = []
        self._loaded_at: float = 0.0
        self._load_lock = asyncio.Lock()

    def columns(self) -> list[str]:
        """Actual CSV column headers, for diagnosing an unrecognized schema."""
        return list(self._columns)

    def _is_fresh(self) -> bool:
        return self._rows is not None and (time.time() - self._loaded_at) < _CACHE_TTL_SECONDS

    async def ensure_loaded(self, http: httpx.AsyncClient, url: str, *, force: bool = False) -> int:
        async with self._load_lock:
            if not force and self._is_fresh():
                return len(self._rows or [])

            if not force and self._cache_path.exists():
                age = time.time() - self._cache_path.stat().st_mtime
                if age < _CACHE_TTL_SECONDS:
                    text = await asyncio.to_thread(self._cache_path.read_text, "utf-8", "replace")
                    self._rows, self._rows_lc, self._haystacks, self._columns = (
                        await asyncio.to_thread(self._parse, text)
                    )
                    self._loaded_at = time.time()
                    return len(self._rows)

            response = await http.get(url, timeout=_DOWNLOAD_TIMEOUT)
            response.raise_for_status()
            text = response.text
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._cache_path.write_text, text)
            self._rows, self._rows_lc, self._haystacks, self._columns = await asyncio.to_thread(
                self._parse, text
            )
            self._loaded_at = time.time()
            return len(self._rows)

    @staticmethod
    def _parse(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str], list[str]]:
        reader = csv.DictReader(io.StringIO(text))
        rows: list[dict[str, str]] = []
        rows_lc: list[dict[str, str]] = []
        haystacks: list[str] = []
        for row in reader:
            rows.append(row)
            rows_lc.append({k.strip().lower(): v for k, v in row.items() if k})
            haystacks.append(" ".join(str(v) for v in row.values() if v).lower())
        columns = list(reader.fieldnames or [])
        return rows, rows_lc, haystacks, columns

    def search(
        self, query: str, *, exchange: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if not self._rows or not self._haystacks:
            return []
        q = query.strip().lower()
        exch = exchange.strip().upper() if exchange else None

        exact: list[dict[str, str]] = []
        partial: list[dict[str, str]] = []
        for row, haystack in zip(self._rows, self._haystacks):
            if q not in haystack:
                continue
            if exch:
                row_exch = (row.get("exchange") or row.get("EXCHANGE") or "").upper()
                if row_exch and row_exch != exch:
                    continue
            # Exact match on any individual field ranks first (e.g. query "TCS"
            # should surface symbol=TCS before every company name containing "tcs").
            if any(str(v).lower() == q for v in row.values() if v):
                exact.append(row)
            elif len(exact) + len(partial) < limit:
                partial.append(row)
            if len(exact) >= limit:
                break
        return (exact + partial)[:limit]

    # ------------------------------------------------------------------
    # Option chain support
    # ------------------------------------------------------------------

    # HDFC's documented field names (see 3_place_order.md) are snake_case, but
    # the actual security-master CSV schema isn't published anywhere, so we
    # probe a broad set of common aliases (including the NSE-style SEM_ naming
    # some broker security masters reuse) rather than hardcoding one guess.
    _SEGMENT_KEYS = ("instrument_segment", "instrumenttype", "instrument_type", "segment", "series", "sem_series")
    _UNDERLYING_KEYS = ("underlying_symbol", "underlyingsymbol", "underlying", "sem_underlying_symbol")
    _SYMBOL_KEYS = ("symbol", "tradingsymbol", "trading_symbol", "sem_trading_symbol", "sem_custom_symbol")
    _OPTION_TYPE_KEYS = ("option_type", "optiontype", "sem_option_type")
    _STRIKE_KEYS = ("strike_price", "strikeprice", "strike", "sem_strike_price")
    _EXPIRY_KEYS = ("expiry_date", "expirydate", "expiry", "sem_expiry_date")
    _SECURITY_ID_KEYS = ("security_id", "securityid", "token", "instrument_token", "sem_smst_security_id")
    _EXCHANGE_KEYS = ("exchange", "sem_exm_exch_id")

    @staticmethod
    def _first(row_lc: dict[str, str], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = row_lc.get(key)
            if value:
                return str(value).strip()
        return ""

    def option_chain_candidates(
        self, underlying_symbol: str, *, exchange: str | None = None
    ) -> list[dict[str, str]]:
        """Every option-contract row matching underlying_symbol (+ optional exchange), all expiries.

        Each returned dict has normalized keys: security_id, symbol, exchange,
        option_type, strike_price, expiry_date — pulled from whichever actual
        CSV columns matched, plus the original row under "_raw" for anything
        else a caller might need. Filter by expiry_date on the caller side
        (its on-disk format/column can't be assumed without a live sample).
        """
        if not self._rows_lc:
            return []
        target = underlying_symbol.strip().upper()
        exch = exchange.strip().upper() if exchange else None

        results: list[dict[str, str]] = []
        for row_lc, row in zip(self._rows_lc, self._rows):
            option_type = self._first(row_lc, self._OPTION_TYPE_KEYS).upper()
            segment = self._first(row_lc, self._SEGMENT_KEYS).upper()
            is_option = option_type in ("CE", "PE", "CALL", "PUT") or "OPT" in segment
            if not is_option:
                continue

            underlying = self._first(row_lc, self._UNDERLYING_KEYS).upper()
            symbol = self._first(row_lc, self._SYMBOL_KEYS).upper()
            if target != underlying and target not in symbol:
                continue

            if exch:
                row_exch = self._first(row_lc, self._EXCHANGE_KEYS).upper()
                if row_exch and row_exch != exch:
                    continue

            results.append(
                {
                    "security_id": self._first(row_lc, self._SECURITY_ID_KEYS),
                    "symbol": symbol or self._first(row_lc, self._SYMBOL_KEYS),
                    "exchange": self._first(row_lc, self._EXCHANGE_KEYS).upper(),
                    "option_type": option_type,
                    "strike_price": self._first(row_lc, self._STRIKE_KEYS),
                    "expiry_date": self._first(row_lc, self._EXPIRY_KEYS),
                    "_raw": row,
                }
            )
        return results

    def distinct_expiries(self, candidates: list[dict[str, str]]) -> list[str]:
        seen = {c["expiry_date"] for c in candidates if c.get("expiry_date")}
        return sorted(seen)
