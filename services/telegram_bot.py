"""Telegram approval gate (synchronous, one-shot). Updates are forwarded here by the Cloudflare Worker."""
from __future__ import annotations

import json
import logging

import requests

from config import settings
from services import gitsync, pipeline, storage

logger = logging.getLogger(__name__)

TIMEOUT = 60


# --------------------------------------------------------------------------- Bot API helpers
def _call(method: str, data: dict | None = None, files: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    resp = requests.post(url, data=data, files=files, timeout=TIMEOUT)
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description') or resp.text[:200]}")
    return payload["result"]


def _say(text: str, reply_markup: dict | None = None) -> None:
    data = {
        "chat_id": settings.telegram_chat_id,
        "text": text[:4000],
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    _call("sendMessage", data)


def _keyboard(draft_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve & Publish", "callback_data": f"approve:{draft_id}"},
            {"text": "❌ Reject / Edit", "callback_data": f"reject:{draft_id}"},
        ]]
    }


def _remove_buttons(message_id: int) -> None:
    try:
        _call("editMessageReplyMarkup", {
            "chat_id": settings.telegram_chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })
    except RuntimeError as exc:  # e.g. message not modified / too old - not worth failing the run
        logger.warning("Could not remove buttons: %s", exc)


# --------------------------------------------------------------------------- preview
def send_preview(draft: dict) -> None:
    """Send image(s) + caption + inline keyboard to the approver."""
    folder = settings.media_dir / draft["id"]
    paths = [folder / f for f in draft["image_files"]]
    chat = {"chat_id": settings.telegram_chat_id}
    if len(paths) == 1:
        _call("sendPhoto", chat, files={"photo": (paths[0].name, paths[0].read_bytes(), "image/jpeg")})
    else:
        media = [{"type": "photo", "media": f"attach://f{i}"} for i in range(len(paths))]
        files = {f"f{i}": (p.name, p.read_bytes(), "image/jpeg") for i, p in enumerate(paths)}
        _call("sendMediaGroup", {**chat, "media": json.dumps(media)}, files=files)

    post, art = draft["post"], draft["article"]
    header = f"🆕 Draft v{draft['revision']} · {post['post_type']}" + (
        f" ({len(paths)} slides)" if len(paths) > 1 else "")
    text = f"{header}\n\n{post['caption']}\n\n— — —\nArticle: {art['title']}\n{art['url']}"
    _say(text, reply_markup=_keyboard(draft["id"]))


def deliver_new_draft() -> None:
    """Daily job / /generate: build today's draft, make its images public, then ask for approval."""
    try:
        draft = pipeline.create_new_draft()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Draft generation failed")
        _say(f"⚠️ Draft generation failed: {exc}")
        return
    if draft is None:
        _say("📭 No fresh AI/tech articles found right now.")
        return
    # Images must be on the public branch BEFORE the approver can tap "Approve".
    gitsync.push(f"draft {draft['id']}")
    send_preview(draft)


# --------------------------------------------------------------------------- handlers
def _cmd_start() -> None:
    _say("👋 AI Influencer bot is online.\n\n"
         "/generate – create a draft now\n/cancel – cancel pending feedback\n/status – show pending state")


def _cmd_generate() -> None:
    _say("🔎 Scraping news and generating a draft…")
    deliver_new_draft()


def _cmd_cancel() -> None:
    storage.set_awaiting_feedback(None)
    _say("Feedback cancelled. The rejected draft stays unpublished.")


def _cmd_status() -> None:
    pending = [d for d in storage.all_drafts() if d["status"] == "pending"]
    waiting = storage.get_awaiting_feedback()
    _say(f"Pending drafts: {len(pending)}\nWaiting for your feedback: {'yes' if waiting else 'no'}")


def _on_text(feedback: str) -> None:
    draft_id = storage.get_awaiting_feedback()
    if not draft_id:
        _say("Nothing to review right now. Use /generate to create a draft.")
        return
    storage.set_awaiting_feedback(None)  # prevents double-processing
    _say("🔄 Regenerating with your feedback…")
    try:
        new_draft = pipeline.regenerate_draft(draft_id, feedback)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Regeneration failed")
        storage.set_awaiting_feedback(draft_id)
        _say(f"⚠️ Regeneration failed: {exc}\nSend your feedback again or /cancel.")
        return
    gitsync.push(f"draft {new_draft['id']}")
    send_preview(new_draft)


def _on_button(query: dict) -> None:
    message = query.get("message") or {}
    if str(message.get("chat", {}).get("id")) != str(settings.telegram_chat_id):
        return  # not the approver
    action, _, draft_id = (query.get("data") or "").partition(":")
    if message.get("message_id"):
        _remove_buttons(message["message_id"])  # one tap only; failed publishes re-send the keyboard

    draft = storage.get_draft(draft_id)
    if not draft or draft["status"] != "pending":
        _say("This draft is no longer active.")
        return

    if action == "approve":
        _say("⏳ Publishing to Instagram…")
        try:
            permalink = pipeline.publish_draft(draft_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Publish failed")
            _say(f"❌ Publishing failed: {exc}\n\nThe draft is still pending – tap Approve to retry.",
                 reply_markup=_keyboard(draft_id))
            return
        gitsync.push(f"published {draft_id}")  # save "published" at once so it can never be posted twice
        _say(f"✅ Published!\n{permalink}")
    elif action == "reject":
        storage.set_awaiting_feedback(draft_id)
        _say("✍️ What should change? Reply with your feedback (tone, angle, length, hashtags, "
             "slide wording, image style…). Send /cancel to drop it.")


def _on_message(message: dict) -> None:
    if str(message.get("chat", {}).get("id")) != str(settings.telegram_chat_id):
        return  # only the approver may talk to the bot
    text = (message.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/"):
        command = text.split()[0].split("@")[0].lower()
        handlers = {"/start": _cmd_start, "/generate": _cmd_generate,
                    "/cancel": _cmd_cancel, "/status": _cmd_status}
        handler = handlers.get(command)
        if handler:
            handler()
        else:
            _cmd_start()
        return
    _on_text(text)


def process_update(update: dict) -> None:
    """Handle one Telegram update (a message or an inline-button tap)."""
    if "callback_query" in update:
        _on_button(update["callback_query"])
    elif "message" in update:
        _on_message(update["message"])
