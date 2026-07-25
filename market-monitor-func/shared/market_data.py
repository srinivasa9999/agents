"""Read-only access to Kite market data and positions.

`ReadOnlyKite` intentionally exposes only the handful of GET-style calls
this app needs (quote, positions, margins). It does not wrap, proxy, or
expose `place_order`, `modify_order`, `cancel_order`, GTTs, or any other
mutating Kite Connect method - not even as an unused/disabled option. This
app is alerting-only and must stay that way structurally, not just by
convention.
"""
import logging

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

logger = logging.getLogger("market_monitor.market_data")


class MarketDataError(Exception):
    pass


class ReadOnlyKite:
    """Thin read-only facade over kiteconnect.KiteConnect."""

    def __init__(self, kite: KiteConnect):
        self._kite = kite

    def quote(self, symbols: list[str]) -> dict:
        return self._kite.quote(symbols)

    def positions(self) -> list[dict]:
        return self._kite.positions().get("net", [])


def get_index_quotes(kite: ReadOnlyKite, kite_symbols: list[str]) -> dict:
    """Returns {kite_symbol: quote_dict} for the requested index/instrument symbols."""
    try:
        return kite.quote(kite_symbols)
    except KiteException as exc:
        raise MarketDataError(f"Kite quote() failed for {kite_symbols}: {exc}") from exc
    except Exception as exc:
        raise MarketDataError(f"Unexpected error fetching quotes for {kite_symbols}: {exc}") from exc


def get_open_positions(kite: ReadOnlyKite) -> list[dict]:
    """Returns open (non-zero quantity) net positions."""
    try:
        net_positions = kite.positions()
    except KiteException as exc:
        raise MarketDataError(f"Kite positions() failed: {exc}") from exc
    except Exception as exc:
        raise MarketDataError(f"Unexpected error fetching positions: {exc}") from exc

    return [p for p in net_positions if p.get("quantity", 0) != 0]
