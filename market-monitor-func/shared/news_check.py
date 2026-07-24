"""Lightweight keyword-based headline check over free public RSS feeds.
No paid search API required. Alerts only on headlines not seen before
(tracked in the SeenHeadlines table), so re-running the timer every few
minutes doesn't re-alert on the same story.
"""
import hashlib
import logging
from calendar import timegm
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests

from .models import Alert
from .state_store import StateStore

logger = logging.getLogger("market_monitor.news")
IST = ZoneInfo("Asia/Kolkata")

REQUEST_TIMEOUT_SECONDS = 10


def _headline_id(entry) -> str:
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry_age_hours(entry, now: datetime) -> float | None:
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return None
    published_dt = datetime.fromtimestamp(timegm(published), tz=timezone.utc)
    return (now - published_dt).total_seconds() / 3600


def _matched_keywords(entry, keywords: list[str]) -> list[str]:
    haystack = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    return [kw for kw in keywords if kw.lower() in haystack]


def check_news(news_config: dict, state_store: StateStore) -> list[Alert]:
    if not news_config.get("enabled", True):
        return []

    keywords = news_config.get("keywords", [])
    feeds = news_config.get("feeds", [])
    max_age_hours = news_config.get("max_headline_age_hours", 12)
    now = datetime.now(tz=timezone.utc)

    alerts = []
    for feed_url in feeds:
        try:
            resp = requests.get(feed_url, timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": "market-monitor-func/1.0"})
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception:
            logger.exception("Failed to fetch/parse RSS feed %s (skipping this feed for this run)", feed_url)
            continue

        for entry in parsed.entries:
            age_hours = _entry_age_hours(entry, now)
            if age_hours is not None and age_hours > max_age_hours:
                continue

            matches = _matched_keywords(entry, keywords)
            if not matches:
                continue

            headline_id = _headline_id(entry)
            if state_store.has_seen_headline(headline_id):
                continue

            title = entry.get("title", "(no title)")
            link = entry.get("link", "")
            alerts.append(Alert(
                condition_key=f"news:{headline_id}",
                category="news",
                title=f"News keyword match: {', '.join(matches)}",
                current_value=title,
                threshold=", ".join(keywords),
                detail=f"{title}\n{link}" if link else title,
                timestamp=datetime.now(tz=IST),
            ))
            state_store.mark_headline_seen(headline_id)

    try:
        state_store.prune_old_headlines(older_than_days=7)
    except Exception:
        logger.exception("Headline pruning failed (non-fatal)")

    return alerts
