"""Picture sourcing (Cloudflare / Gemini) and final post rendering. Outputs Instagram-ready JPEGs."""
from __future__ import annotations

import base64
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

from config import settings
from services import template

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- providers
def _cloudflare_image(prompt: str) -> Image.Image:
    """Cloudflare Workers AI (FLUX). Free allocation: 10,000 neurons/day."""
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.cloudflare_account_id}/ai/run/{settings.cloudflare_image_model}"
    )
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {settings.cloudflare_api_token}"},
        json={"prompt": prompt[:2000], "steps": 4},
        timeout=120,
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


def _gemini_image(prompt: str) -> Image.Image:
    from google.genai import types  # lazy: Cloudflare-only setups never need it

    from services.ai_brain import get_client

    resp = get_client().models.generate_content(
        model=settings.gemini_image_model,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
    )
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if part.inline_data and part.inline_data.data:
                return Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
    raise RuntimeError("Gemini returned no image part")


def _compose_prompt(prompt: str) -> str:
    return (
        f"{prompt}. Style: {settings.image_style}. Vertical 4:5 composition, "
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


def _backgrounds_for_slides(prompts: list[str]) -> list[Image.Image]:
    """One distinct background per slide, generated in parallel; failures get a designed gradient."""
    limit = settings.max_slide_images

    def job(arg):
        i, prompt = arg
        if i >= limit:
            return template.designed_background(i), False
        img = try_generate(prompt)
        return (img, True) if img is not None else (template.designed_background(i), False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(job, enumerate(prompts)))
    logger.info("Carousel pictures: %d of %d slides have an AI image", sum(ok for _, ok in results), len(results))
    return [img for img, _ in results]


# --------------------------------------------------------------------------- rendering
def build_images(draft_id: str, post: dict) -> list[str]:
    """Create JPEG files for a draft. Returns file names inside MEDIA_DIR/<draft_id>/."""
    out_dir = settings.media_dir / draft_id
    out_dir.mkdir(parents=True, exist_ok=True)
    article = post.get("article") or {}

    if post["post_type"] == "carousel":
        n = len(post["slides_text"])
        prompts = list(post.get("slide_image_prompts") or [])
        prompts = (prompts + [post["image_prompt"]] * n)[:n]
        images = template.render_carousel(_backgrounds_for_slides(prompts), post, article)
    else:
        bg = try_generate(post["image_prompt"])
        headline = post.get("headline") or article.get("title") or "AI & Tech news"
        if bg is None or settings.single_post_overlay:
            images = [template.render_single(bg or template.designed_background(0), headline,
                                             article.get("source", ""), post.get("topic", ""))]
        else:
            images = [template.fit_cover(bg)]

    names = []
    for i, img in enumerate(images, 1):
        name = f"slide_{i}.jpg"  # Instagram only accepts JPEG
        img.convert("RGB").save(out_dir / name, "JPEG", quality=92, optimize=True)
        names.append(name)
    return names


def public_url(draft_id: str, filename: str) -> str:
    """URL Instagram's servers will fetch (raw file on the public repo's `data` branch)."""
    return f"{settings.media_base_url}/{draft_id}/{filename}"
