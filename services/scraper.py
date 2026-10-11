"""RSS scraping: fetches fresh AI/tech articles with author + URL for attribution."""
from __future__ import annotations

import calendar
import html
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests

from config import settings

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; AIInfluencerBot/1.0; +https://github.com/)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class Article:
    title: str
    summary: str
    author: str
    url: str
    source: str
    published: str | None  # ISO 8601, UTC

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(text: str, limit: int = 1200) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    text = _WS_RE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _source_name(feed_url: str, parsed_feed) -> str:
    title = (getattr(parsed_feed, "feed", {}) or {}).get("title")
    if title:
        return _clean(title, 80)
    return urlparse(feed_url).netloc.replace("www.", "")


def _parse_date(entry) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)


def _fetch_feed(feed_url: str) -> list[Article]:
    try:
        resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Feed fetch failed for %s: %s", feed_url, exc)
        return []

    parsed = feedparser.parse(resp.content)
    source = _source_name(feed_url, parsed)
    articles: list[Article] = []
    for entry in parsed.entries[:15]:
        url = entry.get("link")
        title = _clean(entry.get("title", ""), 300)
        if not url or not title:
            continue
        body = entry.get("summary") or ""
        if not body and entry.get("content"):
            body = entry["content"][0].get("value", "")
        author = _clean(entry.get("author", ""), 100) or source
        published = _parse_date(entry)
        articles.append(
            Article(
                title=title,
                summary=_clean(body),
                author=author,
                url=url,
                source=source,
                published=published.isoformat() if published else None,
            )
        )
    return articles


def get_fresh_articles(exclude_urls: set[str] | None = None, limit: int | None = None) -> list[Article]:
    """Return the newest unseen articles across all configured feeds."""
    exclude_urls = exclude_urls or set()
    limit = limit or settings.candidates_per_run
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.max_article_age_hours)

    collected: dict[str, Article] = {}
    for feed in settings.rss_feeds:
        for art in _fetch_feed(feed):
            if art.url in exclude_urls or art.url in collected:
                continue
            if art.published and datetime.fromisoformat(art.published) < cutoff:
                continue
            collected[art.url] = art

    ordered = sorted(collected.values(), key=lambda a: a.published or "", reverse=True)
    # Spread across sources so one outlet doesn't dominate the candidate list.
    picked: list[Article] = []
    per_source: dict[str, int] = {}
    for art in ordered:
        if per_source.get(art.source, 0) >= 2:
            continue
        per_source[art.source] = per_source.get(art.source, 0) + 1
        picked.append(art)
        if len(picked) >= limit:
            break
    logger.info("Scraper collected %d candidate articles", len(picked))
    return picked
