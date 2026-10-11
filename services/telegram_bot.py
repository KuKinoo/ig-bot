"""Telegram approval gate (synchronous, one-shot). Updates are forwarded here by the Cloudflare Worker."""
from __future__ import annotations

import json
import logging

import requests

from config import settings
from services import gitsync, photos, pipeline, storage

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


def _say(text: str, reply_markup: dict | None = None) -> int | None:
    """Send a text message; returns its message id."""
    data = {
        "chat_id": settings.telegram_chat_id,
        "text": text[:4000],
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return _call("sendMessage", data).get("message_id")


def _keyboard(draft: dict) -> dict:
    draft_id = draft["id"]
    rows = [[
        {"text": "✅ Approve & Publish", "callback_data": f"approve:{draft_id}"},
        {"text": "❌ Reject / Edit", "callback_data": f"reject:{draft_id}"},
    ]]
    slots = draft.get("slots") or []
    if photos.enabled() and slots:  # one button per picture opens the photo picker for it
        label = "🖼 Change photo" if len(slots) == 1 else "🖼 {}"
        rows.append([{"text": label.format(i + 1), "callback_data": f"swap:{draft_id}:{i}:0"}
                     for i in range(len(slots))])
    return {"inline_keyboard": rows}


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
    if draft.get("kb_msg"):  # older preview of this draft: its buttons must not stay active
        _remove_buttons(draft["kb_msg"])
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
    pictures = pipeline.describe_pictures(draft)
    pics = ("\n\nPictures:\n" + "\n".join(pictures) + "\nTap 🖼 to swap a photo.") if pictures and photos.enabled() else ""
    text = f"{header}{pics}\n\n{pipeline.caption_for(draft)}\n\n— — —\nArticle: {art['title']}\n{art['url']}"
    draft["kb_msg"] = _say(text, reply_markup=_keyboard(draft))
    storage.save_draft(draft)


PAGE = 4


def _send_candidates(chat: dict, shown: list[dict]) -> None:
    """Show candidate thumbnails. Telegram fetches the hotlinked Unsplash URLs itself; upload as a fallback."""
    captions = [f"{n}. {c['author']}" for n, c in enumerate(shown, 1)]
    if len(shown) == 1:
        try:
            _call("sendPhoto", {**chat, "photo": shown[0]["thumb"], "caption": captions[0]})
        except RuntimeError:
            _call("sendPhoto", {**chat, "caption": captions[0]},
                  files={"photo": ("c1.jpg", photos.fetch_thumb(shown[0]), "image/jpeg")})
        return
    media = [{"type": "photo", "media": c["thumb"], "caption": cap} for c, cap in zip(shown, captions)]
    try:
        _call("sendMediaGroup", {**chat, "media": json.dumps(media)})
    except RuntimeError as exc:
        logger.warning("Telegram could not fetch the thumbnails by URL (%s); uploading them", exc)
        files = {f"f{i}": (f"c{i}.jpg", photos.fetch_thumb(c), "image/jpeg") for i, c in enumerate(shown)}
        media = [{"type": "photo", "media": f"attach://f{i}", "caption": cap} for i, cap in enumerate(captions)]
        _call("sendMediaGroup", {**chat, "media": json.dumps(media)}, files=files)


def _send_picker(draft: dict, slide: int, page: int) -> None:
    slot = draft["slots"][slide]
    order = [k for k in range(len(slot["candidates"])) if not (slot["mode"] == "photo" and k == slot["choice"])]
    chunk = order[page * PAGE:(page + 1) * PAGE]
    did = draft["id"]
    rows: list[list[dict]] = []
    if chunk:
        _send_candidates({"chat_id": settings.telegram_chat_id}, [slot["candidates"][k] for k in chunk])
        rows.append([{"text": f"Use {n}", "callback_data": f"use:{did}:{slide}:{k}"}
                     for n, k in enumerate(chunk, 1)])
        if len(order) > (page + 1) * PAGE:
            rows.append([{"text": "More photos ▶", "callback_data": f"swap:{did}:{slide}:{page + 1}"}])
        text = f"Picture {slide + 1}: tap the photo you want ('{slot['query']}')."
    else:
        text = f"Picture {slide + 1}: no more photos for '{slot['query']}'."
    rows.append([{"text": "🤖 AI image", "callback_data": f"ai:{did}:{slide}"},
                 {"text": "🎨 Plain design", "callback_data": f"design:{did}:{slide}"}])
    rows.append([{"text": "✖ Close", "callback_data": f"close:{did}"}])
    _say(text, reply_markup={"inline_keyboard": rows})


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
    parts = (query.get("data") or "").split(":")
    action, draft_id = parts[0], (parts[1] if len(parts) > 1 else "")
    args = parts[2:]
    # The main preview keeps its buttons while you browse photos; every other tap is one-shot.
    first_picker_open = action == "swap" and args[-1:] == ["0"]
    if message.get("message_id") and not first_picker_open:
        _remove_buttons(message["message_id"])  # one tap only; failed publishes re-send the keyboard
    if action == "close":
        return

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
                 reply_markup=_keyboard(draft))
            return
        gitsync.push(f"published {draft_id}")  # save "published" at once so it can never be posted twice
        _say(f"✅ Published!\n{permalink}")
    elif action == "reject":
        storage.set_awaiting_feedback(draft_id)
        _say("✍️ What should change? Reply with your feedback (tone, angle, length, hashtags, "
             "slide wording, image style…). Send /cancel to drop it.")
    elif action in ("swap", "use", "ai", "design"):
        _on_picture_action(draft, action, args)


def _on_picture_action(draft: dict, action: str, args: list[str]) -> None:
    try:
        slide = int(args[0])
        if action == "swap":
            _send_picker(draft, slide, int(args[1]) if len(args) > 1 else 0)
            return
        _say("🔄 Re-rendering that picture…")
        if action == "use":
            new = pipeline.use_photo(draft["id"], slide, int(args[1]))
        elif action == "ai":
            new = pipeline.use_ai_image(draft["id"], slide)
        else:
            new = pipeline.use_design(draft["id"], slide)
    except (ValueError, IndexError, KeyError):
        _say("That button is out of date.")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Picture change failed")
        _say(f"⚠️ Could not change the picture: {exc}")
        return
    gitsync.push(f"photo change {new['id']}")  # new image must be public before the approver can tap Approve
    send_preview(new)


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
