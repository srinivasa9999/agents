"""Persistent state in Azure Table Storage.

Three tables (created automatically on first use):
  - KiteAuthState : one row holding today's Kite access_token (written by the
                    VM's daily login script, read by the function).
  - AlertState    : one row per monitored condition (a watch level, VIX,
                    a position check) tracking which "side" it's currently
                    on and when it last alerted, so we only alert on
                    transitions and respect the cooldown window.
  - SeenHeadlines : one row per news headline we've already alerted on.

Using Azure Table Storage (not blob/local file) because it survives cold
starts and scale-out (multiple function instances would share the same
file-locking problems a local file doesn't solve), needs no extra Azure
resource beyond the storage account the Function App already requires, and
gives us cheap per-entity read/write without a database server.
"""
import logging
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode

logger = logging.getLogger("market_monitor.state")


class StateStore:
    def __init__(self, connection_string: str, kite_table: str, alert_table: str, news_table: str):
        self._service = TableServiceClient.from_connection_string(connection_string)
        self.kite_table = self._get_or_create_table(kite_table)
        self.alert_table = self._get_or_create_table(alert_table)
        self.news_table = self._get_or_create_table(news_table)

    def _get_or_create_table(self, name: str):
        try:
            self._service.create_table(name)
        except ResourceExistsError:
            pass
        return self._service.get_table_client(name)

    # ---------------------------------------------------------------- Kite auth

    def get_kite_session(self) -> dict | None:
        try:
            return self.kite_table.get_entity(partition_key="kite", row_key="session")
        except ResourceNotFoundError:
            return None

    # ---------------------------------------------------------- generic alert state

    def get_alert_state(self, condition_key: str) -> dict | None:
        try:
            return self.alert_table.get_entity(partition_key="alert", row_key=condition_key)
        except ResourceNotFoundError:
            return None

    def set_alert_state(self, condition_key: str, **fields) -> None:
        entity = {"PartitionKey": "alert", "RowKey": condition_key, **fields}
        self.alert_table.upsert_entity(entity, mode=UpdateMode.MERGE)

    def cooldown_elapsed(self, condition_key: str, cooldown_minutes: int, now: datetime | None = None) -> bool:
        """True if enough time has passed since the last alert for this
        condition (or there has never been one)."""
        state = self.get_alert_state(condition_key)
        if not state or "last_alert_at" not in state:
            return True
        now = now or datetime.now(timezone.utc)
        last_alert_at = datetime.fromisoformat(state["last_alert_at"])
        return now - last_alert_at >= timedelta(minutes=cooldown_minutes)

    def record_alert_sent(self, condition_key: str, now: datetime | None = None, **extra_fields) -> None:
        now = now or datetime.now(timezone.utc)
        self.set_alert_state(condition_key, last_alert_at=now.isoformat(), **extra_fields)

    # ------------------------------------------------------------------- news dedup

    def has_seen_headline(self, headline_id: str) -> bool:
        try:
            self.news_table.get_entity(partition_key="headline", row_key=headline_id)
            return True
        except ResourceNotFoundError:
            return False

    def mark_headline_seen(self, headline_id: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.news_table.upsert_entity(
            {"PartitionKey": "headline", "RowKey": headline_id, "seen_at": now.isoformat()},
            mode=UpdateMode.MERGE,
        )

    def prune_old_headlines(self, older_than_days: int = 7) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        try:
            entities = self.news_table.query_entities("PartitionKey eq 'headline'")
            for entity in entities:
                seen_at = entity.get("seen_at")
                if seen_at and datetime.fromisoformat(seen_at) < cutoff:
                    self.news_table.delete_entity(entity["PartitionKey"], entity["RowKey"])
        except Exception:
            logger.exception("Failed to prune old headline entries (non-fatal)")
