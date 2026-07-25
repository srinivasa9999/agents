import time
from types import SimpleNamespace

from shared import news_check

from .conftest import deliver

NEWS_CONFIG = {
    "enabled": True,
    "keywords": ["Iran", "RBI"],
    "feeds": ["https://example.com/feed.xml"],
    "max_headline_age_hours": 12,
}


def _fake_entry(title, link, hours_old=0, entry_id=None):
    published_parsed = time.gmtime(time.time() - hours_old * 3600)
    return {
        "id": entry_id or link,
        "title": title,
        "summary": "",
        "link": link,
        "published_parsed": published_parsed,
    }


def _patch_feed(monkeypatch, entries):
    class FakeResponse:
        content = b""

        def raise_for_status(self):
            pass

    monkeypatch.setattr(news_check.requests, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(news_check.feedparser, "parse", lambda *a, **k: SimpleNamespace(entries=entries))


def test_matching_new_headline_alerts(state_store, monkeypatch):
    entries = [_fake_entry("Iran tensions rattle crude markets", "https://example.com/1")]
    _patch_feed(monkeypatch, entries)

    alerts = news_check.check_news(NEWS_CONFIG, state_store)
    assert len(alerts) == 1
    assert alerts[0].category == "news"


def test_non_matching_headline_no_alert(state_store, monkeypatch):
    entries = [_fake_entry("Local cricket team wins match", "https://example.com/2")]
    _patch_feed(monkeypatch, entries)

    alerts = news_check.check_news(NEWS_CONFIG, state_store)
    assert alerts == []


def test_already_seen_headline_not_repeated(state_store, monkeypatch):
    entries = [_fake_entry("RBI hikes repo rate", "https://example.com/3")]
    _patch_feed(monkeypatch, entries)

    first = deliver(news_check.check_news(NEWS_CONFIG, state_store))
    second = news_check.check_news(NEWS_CONFIG, state_store)
    assert len(first) == 1
    assert second == []


def test_stale_headline_ignored(state_store, monkeypatch):
    entries = [_fake_entry("Iran RBI old news", "https://example.com/4", hours_old=48)]
    _patch_feed(monkeypatch, entries)

    alerts = news_check.check_news(NEWS_CONFIG, state_store)
    assert alerts == []
