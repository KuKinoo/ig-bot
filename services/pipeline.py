"""Orchestration (sync, run in worker threads): scrape -> generate -> render -> store; regenerate; publish."""
from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone

from config import settings
from services import ai_brain, instagram_api, media, photos, scraper, storage

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
    slots = media.plan_slots(post)  # Unsplash search + AI photo review (or plain AI images)
    files = media.build_images(draft_id, post, post["article"], slots)
    draft = {
        "id": draft_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "revision": revision,
        "articles": articles,
        "article": post.pop("article"),
        "post": post,
        "image_files": files,
        "slots": slots,
        "img_ver": 1,
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


def caption_for(draft: dict) -> str:
    """Post caption + the Unsplash credit block for the photos that are really in the images."""
    base = draft["post"]["caption"]
    credit = photos.caption_credit(media.used_photos(draft.get("slots", [])))
    if not credit:
        return base
    room = ai_brain.MAX_CAPTION - len(credit) - 2
    if len(base) > room:  # keep the source line, shorten the text before it
        head, sep, tail = base.rpartition("\n\n📰")
        if not sep:
            head, tail = base, ""
        else:
            tail = sep + tail
        base = head[: max(0, room - len(tail) - 1)].rstrip() + tail
    return f"{base}\n\n{credit}"


def describe_pictures(draft: dict) -> list[str]:
    """One human-readable line per picture, for the Telegram preview."""
    icons = {"photo": "📷", "edited": "✏️📷", "ai": "🤖", "design": "🎨"}
    lines = []
    for i, slot in enumerate(draft.get("slots", []), 1):
        kind = slot.get("rendered") or slot.get("mode")
        who = ""
        if kind in ("photo", "edited") and slot.get("used_id"):
            cand = next((c for c in slot["candidates"] if c["id"] == slot["used_id"]), None)
            who = f" {cand['author']}" if cand else ""
        label = {"photo": "photo", "edited": "photo (AI-edited)", "ai": "AI image", "design": "designed background"}
        extra = ""
        if slot.get("grade", "none") != "none" and kind == "photo":
            extra = f", {slot['grade']} grade"
        if slot.get("by") == "fallback":
            extra += ", not AI-reviewed"
        lines.append(f"{i}. {icons.get(kind, '•')} {label.get(kind, kind)}{who}{extra}")
    return lines


def _change_slide(draft_id: str, slide: int, **changes) -> dict:
    draft = storage.get_draft(draft_id)
    if not draft or draft["status"] != "pending":
        raise RuntimeError("This draft is no longer active.")
    slots = draft.get("slots") or []
    if not 0 <= slide < len(slots):
        raise RuntimeError("Unknown slide.")
    slot = slots[slide]
    backup = dict(slot)
    slot.update(changes)
    slot.update(grade=changes.get("grade", "none"), edit_prompt="", by="human")
    old_files, old_ver = list(draft["image_files"]), draft.get("img_ver", 1)
    try:
        media.rebuild_slide(draft, slide)
    except Exception:
        slot.clear()
        slot.update(backup)
        draft["image_files"], draft["img_ver"] = old_files, old_ver
        raise
    storage.save_draft(draft)
    return draft


def use_photo(draft_id: str, slide: int, candidate: int) -> dict:
    """Human picked candidate `candidate` for picture `slide` (0-based)."""
    draft = storage.get_draft(draft_id)
    slots = (draft or {}).get("slots") or []
    if not (0 <= slide < len(slots) and 0 <= candidate < len(slots[slide]["candidates"])):
        raise RuntimeError("That photo is no longer available.")
    return _change_slide(draft_id, slide, mode="photo", choice=candidate)


def use_ai_image(draft_id: str, slide: int) -> dict:
    return _change_slide(draft_id, slide, mode="ai", choice=None)


def use_design(draft_id: str, slide: int) -> dict:
    return _change_slide(draft_id, slide, mode="design", choice=None)


def publish_draft(draft_id: str) -> str | None:
    """Publish an approved draft to Instagram. Returns permalink (or None)."""
    if not storage.claim_draft(draft_id, "pending", "publishing"):
        raise RuntimeError("Draft is not pending (already published or being published).")
    draft = storage.get_draft(draft_id)
    try:
        urls = [media.public_url(draft_id, f) for f in draft["image_files"]]
        _, permalink = instagram_api.publish(draft["post"]["post_type"], urls, caption_for(draft))
    except Exception:
        storage.set_status(draft_id, "pending")  # allow retry
        raise
    for photo in media.used_photos(draft.get("slots", [])):  # Unsplash guideline: report the use of each photo
        photos.track_download(photo)
    storage.delete_draft(draft_id)
    _remove_media(draft_id)
    return permalink or "(published - link unavailable)"
