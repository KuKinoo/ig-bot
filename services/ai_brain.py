"""Gemini integration: turns scraped articles into a structured Instagram post draft."""
from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from config import settings

logger = logging.getLogger(__name__)

MAX_CAPTION = 2200  # Instagram hard limit

SYSTEM_PROMPT = """You are the content brain of an AI/tech Instagram influencer. Your voice: sharp, curious, \
friendly, and concise - never hype-y or clickbait-y.

You receive numbered candidate news articles. Do this:
1. Pick the SINGLE most newsworthy, visually-shareable article (return its number as source_index).
2. Write an Instagram post about it using ONLY facts present in the provided title/summary. Never invent \
numbers, quotes, or claims. If the summary is thin, stay general.
3. Choose post_type: "carousel" when the story has several distinct points to explain (then provide 4-7 \
slides); otherwise "single".

STRICT OUTPUT RULES
- Respond with ONE valid JSON object and nothing else - no markdown fences, no commentary.
- Keys exactly: source_index (int), post_type ("single" | "carousel"), caption (string), headline (string), \
topic (string), slides_text (array), slides_body (array), image_prompt (string), slide_image_prompts (array).
- caption: strong hook in the first line, 2-4 short paragraphs, 5-8 relevant hashtags at the end, under 1800 \
characters. It MUST end with a source credit line in this form: \
"📰 Source: <Outlet> (by <Author>) - <article URL>". Use the exact URL provided.
- topic: the main subject of the story as a short label, max 4 words, plain text, no emojis, e.g. \
"CLAUDE · ANTHROPIC", "OPENAI SORA", "NVIDIA BLACKWELL". It is printed on the post graphic.
- headline: punchy title for the post graphic, max 9 words, no emojis, no hashtags.
- CAROUSEL: slides_text, slides_body and slide_image_prompts must all have the SAME length N (4-7).
  * slides_text = short slide titles, max 8 words each.
  * slides_body = one supporting sentence per slide, max 22 words each.
  * Slide 1 is the cover: slides_text is the hook, slides_body a one-line teaser.
  * Slides 2..N-1 each make ONE key point.
  * Slide N is the call to action: title like "Follow for daily AI news", body like "Save this post and \
share it with a friend who loves tech."
  * No emojis or hashtags inside slides.
  * slide_image_prompts: one background-picture prompt per slide, each showing a DIFFERENT scene tied to \
that slide's point, all in one consistent visual style.
- SINGLE: slides_text, slides_body and slide_image_prompts must be empty arrays.
- PICTURE PROMPTS (image_prompt and slide_image_prompts): must be CONCRETE and tied to what the article \
actually says. In 25-45 words name the specific subject matter of the story (e.g. a coding assistant, a data \
center, a robot arm, a medical scan, a chip), plus setting, action and mood. Show the CONCEPT, never the \
brand: NO logos, NO product screenshots or app interfaces, NO readable text or letters, NO real people's \
faces. Example for a story about an AI coding assistant: "A glowing translucent assistant orb hovering beside \
a developer's laptop, code fragments floating in the air, dark workspace, soft cyan light". \
image_prompt is the cover/main picture; vertical 4:5.
- When editor feedback is supplied, apply it faithfully while keeping all rules above."""


class GeneratedPost(BaseModel):
    source_index: int = Field(description="1-based number of the chosen article")
    post_type: str = Field(description="'single' or 'carousel'")
    caption: str
    headline: str
    topic: str
    slides_text: list[str]
    slides_body: list[str]
    image_prompt: str
    slide_image_prompts: list[str]


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    return genai.Client(api_key=settings.gemini_api_key)


def _build_prompt(articles: list[dict], feedback: str | None, previous: dict | None) -> str:
    lines = ["Candidate articles:"]
    for i, a in enumerate(articles, 1):
        lines.append(
            f"[{i}] Title: {a['title']}\n    Outlet: {a['source']}\n    Author: {a['author']}\n"
            f"    URL: {a['url']}\n    Published: {a.get('published') or 'unknown'}\n"
            f"    Summary: {a['summary']}"
        )
    if feedback and previous:
        lines += [
            "",
            "PREVIOUS DRAFT (rejected by the human editor):",
            json.dumps(previous, ensure_ascii=False),
            "",
            "EDITOR FEEDBACK:",
            feedback,
            "",
            "Produce a revised draft that applies this feedback.",
        ]
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    return json.loads(text)


def _normalise(raw: GeneratedPost, articles: list[dict]) -> dict:
    idx = min(max(raw.source_index, 1), len(articles)) - 1
    article = articles[idx]

    post_type = raw.post_type.strip().lower()
    titles = [s.strip() for s in raw.slides_text]
    n = len(titles)
    bodies = (list(raw.slides_body) + [""] * n)[:n]
    prompts = (list(raw.slide_image_prompts) + [raw.image_prompt] * n)[:n]
    keep = [i for i, t in enumerate(titles) if t][:10]

    slides = [titles[i] for i in keep]
    slides_body = [bodies[i].strip() for i in keep]
    slide_prompts = [prompts[i].strip() or raw.image_prompt for i in keep]
    if post_type == "carousel" and len(slides) < 2:
        post_type = "single"
    if post_type != "carousel":
        post_type, slides, slides_body, slide_prompts = "single", [], [], []

    caption = raw.caption.strip()
    credit = f"\n\n📰 Source: {article['source']} (by {article['author']}) - {article['url']}"
    if article["url"] not in caption or len(caption) > MAX_CAPTION:
        body = caption
        if article["url"] in body:  # too long but has credit: cut before it
            body = body.split("📰")[0].rstrip()
        body = body[: MAX_CAPTION - len(credit) - 1].rstrip()
        caption = body + credit

    headline = raw.headline.strip() or article["title"]
    if len(headline) > 110:
        headline = headline[:109].rstrip() + "…"

    topic = " ".join(raw.topic.split()).upper()
    if len(topic) > 32:
        topic = topic[:31].rstrip() + "…"

    return {
        "post_type": post_type,
        "caption": caption,
        "headline": headline,
        "topic": topic,
        "slides_text": slides,
        "slides_body": slides_body,
        "image_prompt": raw.image_prompt.strip(),
        "slide_image_prompts": slide_prompts,
        "source_index": idx + 1,
        "article": article,
    }


RETRY_DELAYS = (5, 15, 30, 60)  # seconds to wait between attempts when Google is overloaded
_TEMPORARY_MARKERS = ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "OVERLOADED", "DEADLINE_EXCEEDED")


def _is_temporary(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    return any(m in str(exc).upper() for m in _TEMPORARY_MARKERS)


def _is_permanent(exc: Exception) -> bool:
    """4xx errors (bad model name, bad key...) will not fix themselves by retrying."""
    code = getattr(exc, "code", None)
    return isinstance(code, int) and 400 <= code < 500 and code != 429


def generate_post(articles: list[dict], feedback: str | None = None, previous: dict | None = None) -> dict:
    """Generate a validated post dict.

    Retries with growing pauses on temporary Google errors (503/429) and, if configured,
    falls back to GEMINI_FALLBACK_MODEL. Raises RuntimeError when everything fails.
    """
    if not articles:
        raise ValueError("No articles supplied")
    prompt = _build_prompt(articles, feedback, previous)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=GeneratedPost,
        temperature=0.8,
    )

    models = [settings.gemini_text_model]
    if settings.gemini_fallback_model and settings.gemini_fallback_model not in models:
        models.append(settings.gemini_fallback_model)

    last_exc: Exception | None = None
    for model in models:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                resp = get_client().models.generate_content(model=model, contents=prompt, config=config)
                raw = GeneratedPost.model_validate(_parse_json(resp.text))
                return _normalise(raw, articles)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("Gemini %s attempt %d failed: %s", model, attempt + 1, exc)
                if _is_permanent(exc) or attempt == len(RETRY_DELAYS):
                    break
                time.sleep(RETRY_DELAYS[attempt] if _is_temporary(exc) else 2)
    raise RuntimeError(f"Gemini failed: {last_exc}")
