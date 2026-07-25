from datetime import datetime, timedelta, timezone

import pytest


class FakeStateStore:
    """In-memory stand-in for shared.state_store.StateStore, same public
    interface, no Azure dependency - lets alert_engine/news_check tests run
    fully offline."""

    def __init__(self):
        self._alert_state: dict[str, dict] = {}
        self._seen_headlines: set[str] = set()

    def get_alert_state(self, condition_key: str):
        return self._alert_state.get(condition_key)

    def set_alert_state(self, condition_key: str, **fields) -> None:
        self._alert_state.setdefault(condition_key, {}).update(fields)

    def cooldown_elapsed(self, condition_key: str, cooldown_minutes: int, now=None) -> bool:
        state = self._alert_state.get(condition_key)
        if not state or "last_alert_at" not in state:
            return True
        now = now or datetime.now(timezone.utc)
        last_alert_at = datetime.fromisoformat(state["last_alert_at"])
        return now - last_alert_at >= timedelta(minutes=cooldown_minutes)

    def record_alert_sent(self, condition_key: str, now=None, **extra_fields) -> None:
        now = now or datetime.now(timezone.utc)
        self.set_alert_state(condition_key, last_alert_at=now.isoformat(), **extra_fields)

    def has_seen_headline(self, headline_id: str) -> bool:
        return headline_id in self._seen_headlines

    def mark_headline_seen(self, headline_id: str, now=None) -> None:
        self._seen_headlines.add(headline_id)

    def prune_old_headlines(self, older_than_days: int = 7) -> None:
        pass


@pytest.fixture
def state_store():
    return FakeStateStore()
