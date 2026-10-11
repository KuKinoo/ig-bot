"""Picture sourcing and final post rendering. Outputs Instagram-ready JPEGs.

Every picture ("slot") of a post is filled in this order of preference:
  1. a real Unsplash photo (picked by Gemini vision, optionally colour-graded or lightly AI-edited, or swapped by you)
  2. an AI-generated picture (Cloudflare FLUX / Gemini) when no photo fits
  3. a designed gradient when AI generation is off or fails

Slot layout (stored per draft in state.json):
  {"query", "ai_prompt", "candidates": [photo...], "mode": "photo"|"ai"|"design", "choice": int|None,
   "grade": "none|warm|cool|dark|mono|vivid", "edit_prompt": str, "by": "auto"|"human"|"fallback",
   "rendered": "photo"|"edited"|"ai"|"design", "used_id": str}
"""
from __future__ import annotations

import base64
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from config import settings
from services import photos, template

logger = logging.getLogger(__name__)

CF_EDIT_MODEL = "@cf/runwayml/stable-diffusion-v1-5-img2img"  # beta, 512px-class model
GRADES = ("none", "warm", "cool", "dark", "mono", "vivid")


# --------------------------------------------------------------------------- AI providers
def _cloudflare_run(model: str, payload: dict, timeout: int = 120) -> Image.Image:
    url = f"https://api.cloudflare.com/client/v4/accounts/{settings.cloudflare_account_id}/ai/run/{model}"
    resp = requests.post(
        url, headers={"Authorization": f"Bearer {settings.cloudflare_api_token}"}, json=payload, timeout=timeout
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Cloudflare {resp.status_code}: {resp.text[:300]}")
    if resp.headers.get("content-type", "").startswith("image/"):
        raw = resp.content
    else:
        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"Cloudflare error: {data.get('errors')}")
        raw = base64.b64decode(data["result"]["image"])
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _cloudflare_image(prompt: str) -> Image.Image:
    """Cloudflare Workers AI (FLUX). Free allocation: 10,000 neurons/day."""
    return _cloudflare_run(settings.cloudflare_image_model, {"prompt": prompt[:2000], "steps": 4})


def _first_image(resp) -> Image.Image:
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if part.inline_data and part.inline_data.data:
                return Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
    raise RuntimeError("Gemini returned no image part")


def _gemini_image(prompt: str, source: Image.Image | None = None) -> Image.Image:
    from google.genai import types  # lazy: Cloudflare-only setups never need it

    from services.ai_brain import get_client

    contents = [prompt, source] if source is not None else prompt
    resp = get_client().models.generate_content(
        model=settings.gemini_image_model,
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
    )
    return _first_image(resp)


def _compose_prompt(prompt: str) -> str:
    style = settings.realistic_style if photos.enabled() else settings.image_style
    return (
        f"{prompt}. Style: {style}. Vertical 4:5 composition, "
        "no text, letters, numbers, logos or watermarks, no real people's faces."
    )


def try_generate(prompt: str, attempts: int = 2) -> Image.Image | None:
    """One AI picture, or None when the provider is off / keeps failing (caller uses a designed background)."""
    provider = settings.image_provider
    if provider not in ("cloudflare", "gemini"):
        return None
    for attempt in range(attempts):
        try:
            if provider == "cloudflare":
                return _cloudflare_image(_compose_prompt(prompt))
            return _gemini_image(_compose_prompt(prompt))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Image generation failed (%s, attempt %d/%d): %s", provider, attempt + 1, attempts, exc)
            if attempt + 1 < attempts:
                time.sleep(3)
    return None


# --------------------------------------------------------------------------- editing a real photo
def edit_available() -> bool:
    return settings.image_provider in ("cloudflare", "gemini")


def _grain(img: Image.Image, amount: float = 0.05) -> Image.Image:
    noise = Image.effect_noise(img.size, 28).convert("RGB")
    return Image.blend(img, noise, amount)


def try_edit(img: Image.Image, instruction: str, attempts: int = 2) -> Image.Image | None:
    """Light generative edit of a real photo (img2img). None when unavailable/failing -> caller keeps the original.

    Cloudflare's free img2img model works at ~512px and is upscaled, so it is kept for small changes only
    (low `strength`); Gemini (paid) edits at full quality.
    """
    provider = settings.image_provider
    if provider not in ("cloudflare", "gemini") or not instruction.strip():
        return None
    for attempt in range(attempts):
        try:
            if provider == "cloudflare":
                small = ImageOps.fit(img, (512, 640), Image.LANCZOS)
                buf = io.BytesIO()
                small.save(buf, "JPEG", quality=92)
                out = _cloudflare_run(
                    CF_EDIT_MODEL,
                    {
                        "prompt": f"{instruction[:900]}, realistic photograph, natural lighting, sharp focus",
                        "negative_prompt": "text, watermark, logo, cartoon, illustration, painting, deformed",
                        "image_b64": base64.b64encode(buf.getvalue()).decode(),
                        "width": 512,
                        "height": 640,
                        "strength": 0.45,
                        "num_steps": 20,
                        "guidance": 7.5,
                    },
                    timeout=180,
                )
                out = template.fit_cover(out).filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
                return _grain(out)
            prompt = (f"Edit this photo: {instruction}. Keep it a realistic photograph with the same composition. "
                      "No text, logos or watermarks.")
            return template.fit_cover(_gemini_image(prompt, source=img))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Photo edit failed (%s, attempt %d/%d): %s", provider, attempt + 1, attempts, exc)
            if attempt + 1 < attempts:
                time.sleep(3)
    return None


def _tint(img: Image.Image, rgb: tuple[int, int, int], amount: float) -> Image.Image:
    return Image.blend(img, Image.new("RGB", img.size, rgb), amount)


def grade(img: Image.Image, name: str) -> Image.Image:
    """Non-generative colour grading so mixed photos feel like one set (and text stays readable)."""
    if name == "warm":
        return ImageEnhance.Contrast(_tint(img, (255, 176, 96), 0.14)).enhance(1.04)
    if name == "cool":
        return ImageEnhance.Contrast(_tint(img, (86, 140, 255), 0.16)).enhance(1.04)
    if name == "dark":
        return ImageEnhance.Contrast(ImageEnhance.Brightness(img).enhance(0.78)).enhance(1.08)
    if name == "mono":
        mono = ImageOps.grayscale(img).convert("RGB")
        return _tint(ImageEnhance.Contrast(mono).enhance(1.1), template.accent(), 0.12)
    if name == "vivid":
        return ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.25)).enhance(1.05)
    return img


# --------------------------------------------------------------------------- planning (search + AI choice)
def slot_count(post: dict) -> int:
    return len(post["slides_text"]) if post["post_type"] == "carousel" else 1


def _new_slot(query: str, ai_prompt: str) -> dict:
    return {"query": query, "ai_prompt": ai_prompt, "candidates": [], "mode": "ai", "choice": None,
            "grade": "none", "edit_prompt": "", "by": "auto", "rendered": "", "used_id": ""}


def plan_slots(post: dict) -> list[dict]:
    """Decide what fills every picture of the post. Without an Unsplash key all slots are plain AI slots."""
    n = slot_count(post)
    if post["post_type"] == "carousel":
        prompts = (list(post.get("slide_image_prompts") or []) + [post["image_prompt"]] * n)[:n]
    else:
        prompts = [post["image_prompt"]]
    fallback_q = post.get("topic", "").lower() or "technology"
    queries = (list(post.get("photo_queries") or []) + [fallback_q] * n)[:n]
    slots = [_new_slot(q, p) for q, p in zip(queries, prompts)]
    if photos.enabled():
        _attach_candidates(slots, post)
        _decide(slots, post)
    return slots


def _attach_candidates(slots: list[dict], post: dict) -> None:
    taken: set[str] = set()
    broad = " ".join(post.get("topic", "").lower().split()[:3]) or "technology"
    for i, slot in enumerate(slots, 1):
        found: list[dict] = []
        try:
            found = photos.search(slot["query"], settings.photo_pool, taken)
            if len(found) < 2 and broad != slot["query"]:
                ids = {p["id"] for p in found}
                found += photos.search(broad, settings.photo_pool - len(found), taken | ids)
        except photos.UnsplashError as exc:
            logger.warning("Unsplash search failed for slide %d (%r): %s", i, slot["query"], exc)
        slot["candidates"] = found
        taken.update(p["id"] for p in found[: settings.photo_candidates])
    logger.info("Photo candidates per slide: %s", [len(s["candidates"]) for s in slots])


def _slide_text(post: dict, i: int) -> tuple[str, str]:
    if post["post_type"] == "carousel":
        bodies = post.get("slides_body") or []
        return post["slides_text"][i], (bodies[i] if i < len(bodies) else "")
    return post.get("headline", ""), post.get("topic", "")


def _decide(slots: list[dict], post: dict) -> None:
    from services import ai_brain

    shown_maps: list[list[int]] = []  # AI candidate number (1-based) -> index in slot["candidates"]
    inputs = []
    for i, slot in enumerate(slots):
        mapping, loaded = [], []
        for k, cand in enumerate(slot["candidates"][: settings.photo_candidates]):
            try:
                loaded.append({"alt": cand["alt"], "thumb_bytes": photos.fetch_thumb(cand)})
                mapping.append(k)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Thumbnail download failed (%s): %s", cand["id"], exc)
        shown_maps.append(mapping)
        title, body = _slide_text(post, i)
        inputs.append({"title": title, "body": body, "query": slot["query"], "candidates": loaded})

    decisions = None
    if any(inp["candidates"] for inp in inputs):
        try:
            decisions = {d.slide: d for d in ai_brain.choose_photos(post, inputs)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI photo review failed, using the top search result per slide: %s", exc)

    edits = 0
    for i, slot in enumerate(slots, 1):
        mapping = shown_maps[i - 1]
        if decisions is None:  # AI unavailable: best Unsplash match, flagged so the preview can say so
            if mapping:
                slot.update(mode="photo", choice=mapping[0], by="fallback")
            continue
        d = decisions.get(i)
        action = (d.action if d else "ai").strip().lower()
        if d and action in ("use", "grade", "edit") and 1 <= d.candidate <= len(mapping):
            slot.update(mode="photo", choice=mapping[d.candidate - 1])
            if action == "grade" and d.grade in GRADES:
                slot["grade"] = d.grade
            elif action == "edit" and d.edit_prompt.strip():
                if edit_available() and edits < settings.max_photo_edits:
                    slot["edit_prompt"] = d.edit_prompt.strip()[:400]
                    edits += 1
                else:
                    slot["grade"] = "dark"  # cannot edit -> at least make it blend in
        else:
            slot.update(mode="ai", choice=None)


# --------------------------------------------------------------------------- rendering
def _background(arg: tuple[int, dict]) -> Image.Image:
    """Fill one slot. Records what was really used in slot['rendered'] / slot['used_id']."""
    i, slot = arg
    slot["rendered"], slot["used_id"] = "", ""
    if slot["mode"] == "photo" and slot["choice"] is not None and slot["choice"] < len(slot["candidates"]):
        photo = slot["candidates"][slot["choice"]]
        try:
            img = photos.fetch_image(photo, template.W, template.H)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Photo download failed (%s): %s", photo["id"], exc)
        else:
            slot["rendered"], slot["used_id"] = "photo", photo["id"]
            if slot.get("edit_prompt"):
                edited = try_edit(img, slot["edit_prompt"])
                if edited is not None:
                    img, slot["rendered"] = edited, "edited"
                else:
                    slot["edit_prompt"] = ""
            return grade(img, slot.get("grade", "none"))
    if slot["mode"] in ("photo", "ai"):
        img = try_generate(slot["ai_prompt"])
        if img is not None:
            slot["rendered"] = "ai"
            return img
    slot["rendered"] = "design"
    return template.designed_background(i)


def used_photos(slots: list[dict]) -> list[dict]:
    """Unsplash photos that really ended up in the rendered post (for credits and download tracking)."""
    out = []
    for slot in slots:
        if slot.get("rendered") in ("photo", "edited") and slot.get("used_id"):
            out += [c for c in slot["candidates"] if c["id"] == slot["used_id"]][:1]
    return out


def _credit_for(slot: dict) -> str:
    return next((photos.credit_short(p) for p in used_photos([slot])), "")


def _file_name(index: int, version: int) -> str:
    # Version in the name: raw.githubusercontent.com caches files for minutes, so a swapped photo needs a new URL.
    return f"slide_{index}.v{version}.jpg"


def build_images(draft_id: str, post: dict, article: dict, slots: list[dict], version: int = 1) -> list[str]:
    """Create JPEG files for a draft. Returns file names inside MEDIA_DIR/<draft_id>/."""
    out_dir = settings.media_dir / draft_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # AI pictures are capped per post (free-tier budget); the rest become designed backgrounds.
    ai_seen = 0
    for slot in slots:
        if slot["mode"] == "ai":
            ai_seen += 1
            if ai_seen > settings.max_slide_images:
                slot["mode"] = "design"

    with ThreadPoolExecutor(max_workers=2) as pool:
        backgrounds = list(pool.map(_background, enumerate(slots)))
    logger.info("Pictures: %s", [s["rendered"] for s in slots])
    credits = [_credit_for(s) for s in slots]

    if post["post_type"] == "carousel":
        images = template.render_carousel(backgrounds, post, article, credits)
    else:
        headline = post.get("headline") or article.get("title") or "AI & Tech news"
        if settings.single_post_overlay or slots[0]["rendered"] == "design":
            images = [template.render_single(backgrounds[0], headline, article.get("source", ""),
                                             post.get("topic", ""), credits[0])]
        else:
            images = [template.fit_cover(backgrounds[0])]

    names = []
    for i, img in enumerate(images, 1):
        name = _file_name(i, version)  # Instagram only accepts JPEG
        img.convert("RGB").save(out_dir / name, "JPEG", quality=92, optimize=True)
        names.append(name)
    return names


def rebuild_slide(draft: dict, index: int) -> None:
    """Re-render ONE picture of an existing draft after the human changed its photo (mutates the draft)."""
    post, article, slots = draft["post"], draft["article"], draft["slots"]
    slot = slots[index]
    bg = _background((index, slot))
    credit = _credit_for(slot)
    if post["post_type"] == "carousel":
        img = template.render_slide(index + 1, len(slots), bg, post, article, credit)
    elif settings.single_post_overlay or slot["rendered"] == "design":
        img = template.render_single(bg, post.get("headline") or article.get("title") or "AI & Tech news",
                                     article.get("source", ""), post.get("topic", ""), credit)
    else:
        img = template.fit_cover(bg)

    version = draft.get("img_ver", 1) + 1
    folder = settings.media_dir / draft["id"]
    old = draft["image_files"][index]
    new = _file_name(index + 1, version)
    img.convert("RGB").save(folder / new, "JPEG", quality=92, optimize=True)
    (folder / old).unlink(missing_ok=True)
    draft["image_files"][index] = new
    draft["img_ver"] = version


def public_url(draft_id: str, filename: str) -> str:
    """URL Instagram's servers will fetch (raw file on the public repo's `data` branch)."""
    return f"{settings.media_base_url}/{draft_id}/{filename}"
