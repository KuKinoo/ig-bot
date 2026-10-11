"""Unsplash client + attribution helpers.

Written to follow the Unsplash API Guidelines (https://help.unsplash.com/en/articles/2511245-unsplash-api-guidelines):
  * Hotlinking   - every image is fetched from the URLs the API returned (photo.urls.raw, only extended with
                   imgix size parameters; the `ixid` parameter is kept so Unsplash can count the view).
  * Downloads    - track_download() calls photo.links.download_location when a photo is really used (on approval).
  * Attribution  - photographer + Unsplash, with links that carry ?utm_source=<app>&utm_medium=referral
                   (caption text and a small credit printed on the slide).
  * Rate limits  - demo keys get 50 API requests/hour; the client reads X-Ratelimit-Remaining and stops early.
  * Secrets      - the Access Key only lives in an environment variable / GitHub secret.
"""
from __future__ import annotations

import io
import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from PIL import Image

from config import settings

logger = logging.getLogger(__name__)

API = "https://api.unsplash.com"
TIMEOUT = 20
SEARCH_RESERVE = 8  # API calls left untouched for download tracking after you approve a post

_remaining: int | None = None  # from the X-Ratelimit-Remaining header of the last API response


class UnsplashError(RuntimeError):
    pass


def enabled() -> bool:
    return settings.photos_enabled


def _headers() -> dict:
    return {"Authorization": f"Client-ID {settings.unsplash_access_key}", "Accept-Version": "v1"}


def _remember_limit(resp: requests.Response) -> None:
    global _remaining
    value = resp.headers.get("X-Ratelimit-Remaining", "")
    if value.isdigit():
        _remaining = int(value)


def _get(path_or_url: str, params: dict | None = None, reserve: int = 0) -> requests.Response:
    if _remaining is not None and _remaining <= reserve:
        raise UnsplashError(f"Unsplash hourly request budget used up ({_remaining} left)")
    url = path_or_url if path_or_url.startswith("http") else f"{API}{path_or_url}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise UnsplashError(f"Network error calling Unsplash: {exc}") from exc
    _remember_limit(resp)
    if resp.status_code >= 400:
        raise UnsplashError(f"Unsplash {resp.status_code}: {resp.text[:200]}")
    return resp


# --------------------------------------------------------------------------- URLs
def with_params(url: str, **params) -> str:
    """Add/override query parameters while keeping everything else (notably `ixid`)."""
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in params.items()})
    return urlunparse(parts._replace(query=urlencode(query)))


def utm(url: str) -> str:
    return with_params(url, utm_source=settings.unsplash_app_name, utm_medium="referral")


# --------------------------------------------------------------------------- search
def _trim(p: dict) -> dict | None:
    """Keep only what the bot needs (this dict is stored in state.json)."""
    try:
        user = p.get("user") or {}
        urls = p["urls"]
        return {
            "id": p["id"],
            "alt": (p.get("alt_description") or p.get("description") or "")[:200],
            "color": p.get("color") or "",
            "w": int(p.get("width") or 0),
            "h": int(p.get("height") or 0),
            "thumb": urls["small"],
            "raw": urls["raw"],
            "author": user.get("name") or user.get("username") or "Unknown",
            "author_url": (user.get("links") or {}).get("html")
            or f"https://unsplash.com/@{user.get('username', '')}",
            "page_url": (p.get("links") or {}).get("html") or "",
            "download_location": (p.get("links") or {})["download_location"],
        }
    except (KeyError, TypeError):
        return None


def search(query: str, limit: int, exclude: set[str] | None = None) -> list[dict]:
    """Portrait photos for `query`, best match first. One API request."""
    exclude = exclude or set()
    resp = _get(
        "/search/photos",
        {
            "query": query,
            "orientation": "portrait",
            "content_filter": "high",  # stricter safe-search
            "per_page": 12,
            "order_by": "relevant",
        },
        reserve=SEARCH_RESERVE,
    )
    out: list[dict] = []
    for raw in resp.json().get("results", []):
        photo = _trim(raw)
        if not photo or photo["id"] in exclude or photo["w"] < 1080:
            continue
        out.append(photo)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- images
def image_url(photo: dict, width: int, height: int) -> str:
    """Hotlinked, cropped rendition of the photo (imgix params officially supported by Unsplash)."""
    return with_params(photo["raw"], w=width, h=height, fit="crop", crop="entropy", q=85, fm="jpg")


def fetch_image(photo: dict, width: int, height: int) -> Image.Image:
    resp = requests.get(image_url(photo, width, height), timeout=60)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def fetch_thumb(photo: dict) -> bytes:
    resp = requests.get(photo["thumb"], timeout=30)
    resp.raise_for_status()
    return resp.content


def track_download(photo: dict) -> None:
    """Required by the guidelines whenever a photo is actually used. Failures never block publishing."""
    try:
        _get(photo["download_location"])
    except UnsplashError as exc:
        logger.warning("Download tracking failed for %s: %s", photo.get("id"), exc)


# --------------------------------------------------------------------------- attribution
def credit_short(photo: dict) -> str:
    """Tiny line printed on the slide itself."""
    return f"Photo: {photo['author']} / Unsplash"


def caption_credit(used: list[dict]) -> str:
    """Caption block: photographer(s) + Unsplash, each linked with the required UTM parameters."""
    seen: dict[str, str] = {}
    for photo in used:
        seen.setdefault(photo["author_url"], photo["author"])
    if not seen:
        return ""
    names = ", ".join(f"{name} ({utm(url)})" for url, name in seen.items())
    noun = "Photo" if len(seen) == 1 else "Photos"
    return f"📷 {noun} by {names} on Unsplash ({utm('https://unsplash.com')})"
