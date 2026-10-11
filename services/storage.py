"""Tiny JSON-file state store (drafts, seen URLs, pending-feedback marker). Thread-safe."""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from config import settings

_lock = threading.RLock()
_state: dict[str, Any] | None = None
MAX_SEEN = 500


def _load() -> dict[str, Any]:
    global _state
    if _state is None:
        try:
            _state = json.loads(settings.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            _state = {}
        _state.setdefault("seen_urls", [])
        _state.setdefault("drafts", {})
        _state.setdefault("awaiting_feedback", None)
    return _state


def _save() -> None:
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, settings.state_file)


# --- seen URLs ---------------------------------------------------------------
def seen_urls() -> set[str]:
    with _lock:
        return set(_load()["seen_urls"])


def mark_seen(url: str) -> None:
    with _lock:
        st = _load()
        if url not in st["seen_urls"]:
            st["seen_urls"] = (st["seen_urls"] + [url])[-MAX_SEEN:]
            _save()


# --- drafts ------------------------------------------------------------------
def save_draft(draft: dict) -> None:
    with _lock:
        _load()["drafts"][draft["id"]] = draft
        _save()


def get_draft(draft_id: str) -> dict | None:
    with _lock:
        return _load()["drafts"].get(draft_id)


def all_drafts() -> list[dict]:
    with _lock:
        return list(_load()["drafts"].values())


def delete_draft(draft_id: str) -> None:
    with _lock:
        st = _load()
        st["drafts"].pop(draft_id, None)
        if st["awaiting_feedback"] == draft_id:
            st["awaiting_feedback"] = None
        _save()


def claim_draft(draft_id: str, from_status: str, to_status: str) -> bool:
    """Atomically move a draft between statuses (guards against double-clicks)."""
    with _lock:
        draft = _load()["drafts"].get(draft_id)
        if not draft or draft["status"] != from_status:
            return False
        draft["status"] = to_status
        _save()
        return True


def set_status(draft_id: str, status: str) -> None:
    with _lock:
        draft = _load()["drafts"].get(draft_id)
        if draft:
            draft["status"] = status
            _save()


# --- feedback loop marker ------------------------------------------------------
def set_awaiting_feedback(draft_id: str | None) -> None:
    with _lock:
        _load()["awaiting_feedback"] = draft_id
        _save()


def get_awaiting_feedback() -> str | None:
    with _lock:
        return _load()["awaiting_feedback"]
