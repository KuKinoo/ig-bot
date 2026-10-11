"""Orchestration (sync, run in worker threads): scrape -> generate -> render -> store; regenerate; publish."""
from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone

from config import settings
from services import ai_brain, instagram_api, media, scraper, storage

logger = logging.getLogger(__name__)

DRAFT_TTL_DAYS = 7


def _remove_media(draft_id: str) -> None:
    shutil.rmtree(settings.media_dir / draft_id, ignore_errors=True)


def _purge_stale() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DRAFT_TTL_DAYS)
    for d in storage.all_drafts():
        if datetime.fromisoformat(d["created_at"]) < cutoff and d["status"] != "publishing":
            storage.delete_draft(d["id"])
            _remove_media(d["id"])


def _make_draft(articles: list[dict], feedback: str | None = None, previous: dict | None = None,
                revision: int = 1) -> dict:
    post = ai_brain.generate_post(articles, feedback=feedback, previous=previous)
    draft_id = uuid.uuid4().hex[:12]  # also the unguessable media folder name
    files = media.build_images(draft_id, post)
    draft = {
        "id": draft_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "revision": revision,
        "articles": articles,
        "article": post.pop("article"),
        "post": post,
        "image_files": files,
    }
    storage.save_draft(draft)
    storage.mark_seen(draft["article"]["url"])
    return draft


def create_new_draft() -> dict | None:
    """Daily job body. Returns None when there is no fresh news."""
    _purge_stale()
    articles = scraper.get_fresh_articles(exclude_urls=storage.seen_urls())
    if not articles:
        logger.info("No fresh articles found")
        return None
    return _make_draft([a.to_dict() for a in articles])


def regenerate_draft(draft_id: str, feedback: str) -> dict:
    """Apply human feedback: build a new draft from the same candidates, retire the old one."""
    old = storage.get_draft(draft_id)
    if not old:
        raise RuntimeError("The draft you rejected no longer exists.")
    previous = {**old["post"], "source_index": old["post"].get("source_index", 1)}
    new = _make_draft(old["articles"], feedback=feedback, previous=previous, revision=old["revision"] + 1)
    storage.delete_draft(draft_id)
    _remove_media(draft_id)
    return new


def publish_draft(draft_id: str) -> str | None:
    """Publish an approved draft to Instagram. Returns permalink (or None)."""
    if not storage.claim_draft(draft_id, "pending", "publishing"):
        raise RuntimeError("Draft is not pending (already published or being published).")
    draft = storage.get_draft(draft_id)
    try:
        urls = [media.public_url(draft_id, f) for f in draft["image_files"]]
        _, permalink = instagram_api.publish(draft["post"]["post_type"], urls, draft["post"]["caption"])
    except Exception:
        storage.set_status(draft_id, "pending")  # allow retry
        raise
    storage.delete_draft(draft_id)
    _remove_media(draft_id)
    return permalink or "(published - link unavailable)"
