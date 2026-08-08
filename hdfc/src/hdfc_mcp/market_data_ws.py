"""WebSocket client for InvestRight's live market data feed (incl. Greeks).

Wire format (from /ir-docs/docs/market_data_websocket + the linked
GenericDTO3.proto):

  * Client -> server: JSON text frames shaped like
    {"heart_beat": false, "subscribe": [{"scripId": "NFO_12345", "type": "GREEK"}],
     "unSubscribe": [...]}
    scripId is prefixed per exchange/segment, e.g. NSE_<token>, NFO_<token>,
    BFO_<token>, NSE_INDEX_<token>, MCX_<token> (see PREFIX table in the doc).
  * Server -> client: binary protobuf `GenericDTO` frames. `packetType`
    indicates which sub-message is populated (mbpData / indexData /
    greekData / order / trade), plus periodic JSON heartbeat text frames.

This module keeps a single long-lived connection, remembers every
subscription so it can resubscribe after a reconnect, decodes every
incoming GenericDTO, and stores the latest snapshot per scripId in memory
for the MCP tools to read — no per-call network round trip needed once
streaming.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import websockets
from google.protobuf.json_format import MessageToDict
from websockets.exceptions import ConnectionClosed

from .config import Settings
from .proto import generic_dto_pb2 as pb

logger = logging.getLogger("hdfc_mcp.ws")

_RECONNECT_BACKOFF = (1, 2, 5, 10, 20, 30)
_HEARTBEAT_INTERVAL = 25.0

# packetType -> which GenericDTO field carries the payload
_FIELD_BY_PACKET_TYPE: dict[int, str] = {
    pb.NSE_CM_ALL: "mbpData",
    pb.NSE_CD_ALL: "mbpData",
    pb.NSE_FO_ALL: "mbpData",
    pb.BSE_CM: "mbpData",
    pb.BSE_FO_ALL: "mbpData",
    pb.MCX_PKT: "mbpData",
    pb.NSE_CM_CIRC: "mbpData",
    pb.NSE_CD_CIRC: "mbpData",
    pb.NSE_CD_OI: "mbpData",
    pb.NSE_FO_CIRC: "mbpData",
    pb.NSE_FO_OI: "mbpData",
    pb.BSE_FO_OI: "mbpData",
    pb.NSE_INDEX: "indexData",
    pb.BSE_INDEX: "indexData",
    pb.NSE_FO_GREEK: "greekData",
    pb.BSE_FO_GREEK: "greekData",
    pb.ORDER: "order",
    pb.TRADE: "trade",
}


@dataclass
class ScripSnapshot:
    scrip_id: str
    packets: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at: dict[str, float] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "scripId": self.scrip_id,
            "data": self.packets,
            "updated_at": self.updated_at,
        }


class MarketDataStream:
    def __init__(self, settings: Settings, access_token_provider):
        self._settings = settings
        self._access_token_provider = access_token_provider
        self._subscriptions: dict[str, set[str]] = {}
        self._snapshots: dict[str, ScripSnapshot] = {}
        self._task: asyncio.Task | None = None
        self._ws: Any | None = None  # websockets.ClientConnection once connected
        self._connected = asyncio.Event()
        self._stop = False
        self._last_heartbeat_sent = 0.0
        self._last_heartbeat_recv: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop = False
        self._task = asyncio.create_task(self._run_forever(), name="hdfc-ws-market-data")

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _run_forever(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                await self._connect_and_listen()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected.clear()
                delay = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
                logger.warning("market data ws disconnected (%s); reconnecting in %ss", exc, delay)
                attempt += 1
                await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> None:
        token = self._access_token_provider()
        headers = {"User-Agent": self._settings.user_agent}
        if token:
            headers["Authorization"] = token
        url = f"{self._settings.ws_url}?api_key={self._settings.api_key}"

        async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
            self._ws = ws
            self._connected.set()
            logger.info("market data ws connected")
            await self._resubscribe_all()

            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            try:
                async for message in ws:
                    self._handle_message(message)
            finally:
                heartbeat_task.cancel()
                self._connected.clear()
                self._ws = None

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            if self._ws is not None:
                try:
                    await self._ws.send(json.dumps({"heart_beat": True}))
                    self._last_heartbeat_sent = time.time()
                except ConnectionClosed:
                    return

    def _handle_message(self, message: bytes | str) -> None:
        if isinstance(message, str):
            self._last_heartbeat_recv = time.time()
            return

        dto = pb.GenericDTO()
        try:
            dto.ParseFromString(message)
        except Exception:
            logger.debug("failed to parse binary ws frame (%d bytes)", len(message))
            return

        field_name = _FIELD_BY_PACKET_TYPE.get(dto.packetType)
        scrip_id = str(dto.instrumentId)

        snapshot = self._snapshots.setdefault(scrip_id, ScripSnapshot(scrip_id=scrip_id))
        now = time.time()

        if field_name is not None and dto.HasField(field_name):
            sub = getattr(dto, field_name)
            snapshot.packets[field_name] = MessageToDict(
                sub, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
            )
            snapshot.updated_at[field_name] = now
        snapshot.updated_at["_packetType"] = dto.packetType
        snapshot.updated_at["_lastSeen"] = now

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    async def subscribe(self, scrip_id: str, sub_type: str = "ALL") -> None:
        self._subscriptions.setdefault(scrip_id, set()).add(sub_type)
        if self._ws is not None:
            await self._ws.send(
                json.dumps(
                    {
                        "heart_beat": False,
                        "subscribe": [{"scripId": scrip_id, "type": sub_type}],
                        "unSubscribe": [],
                    }
                )
            )

    async def unsubscribe(self, scrip_id: str, sub_type: str | None = None) -> None:
        types = self._subscriptions.get(scrip_id)
        if types is None:
            return
        to_remove = {sub_type} if sub_type else set(types)
        types -= to_remove
        if not types:
            self._subscriptions.pop(scrip_id, None)

        if self._ws is not None:
            await self._ws.send(
                json.dumps(
                    {
                        "heart_beat": False,
                        "subscribe": [],
                        "unSubscribe": [
                            {"scripId": scrip_id, "type": t} for t in to_remove
                        ],
                    }
                )
            )

    async def _resubscribe_all(self) -> None:
        if not self._subscriptions or self._ws is None:
            return
        subs = [
            {"scripId": scrip_id, "type": t}
            for scrip_id, types in self._subscriptions.items()
            for t in types
        ]
        await self._ws.send(json.dumps({"heart_beat": False, "subscribe": subs, "unSubscribe": []}))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    # Incoming packets are keyed by bare instrumentId, but subscriptions use
    # exchange-prefixed scripIds ("NFO_43382"). Accept either form on read.
    _SCRIP_PREFIXES = (
        "NSE_INDEX_", "BSE_INDEX_", "NFO_", "BFO_", "NCD_", "CDS_",
        "NSE_", "BSE_", "MCX_",
    )

    @classmethod
    def _bare_scrip_id(cls, scrip_id: str) -> str:
        for prefix in cls._SCRIP_PREFIXES:
            if scrip_id.startswith(prefix):
                return scrip_id[len(prefix):]
        return scrip_id

    def get_snapshot(self, scrip_id: str) -> dict[str, Any] | None:
        snap = self._snapshots.get(scrip_id) or self._snapshots.get(self._bare_scrip_id(scrip_id))
        if snap is None:
            return None
        result = snap.to_public_dict()
        last_seen = snap.updated_at.get("_lastSeen")
        if last_seen is not None:
            result["age_seconds"] = round(time.time() - last_seen, 3)
        return result

    def list_subscriptions(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self._subscriptions.items()}

    def connection_status(self) -> dict[str, Any]:
        return {
            "connected": self._connected.is_set(),
            "subscribed_scrips": len(self._subscriptions),
            "last_heartbeat_received": self._last_heartbeat_recv,
            "last_heartbeat_sent": self._last_heartbeat_sent or None,
        }
