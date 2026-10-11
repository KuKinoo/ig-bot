"""Instagram Graph API: single-image and carousel publishing."""
from __future__ import annotations

import logging
import time

import requests

from config import settings

logger = logging.getLogger(__name__)

TIMEOUT = 60


class InstagramError(RuntimeError):
    pass


def _base() -> str:
    return f"https://graph.facebook.com/{settings.instagram_graph_version}"


def _request(method: str, path: str, **params) -> dict:
    params["access_token"] = settings.instagram_access_token
    url = f"{_base()}/{path}"
    try:
        if method == "GET":
            resp = requests.get(url, params=params, timeout=TIMEOUT)
        else:
            resp = requests.post(url, data=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise InstagramError(f"Network error calling Instagram: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text}
    if resp.status_code >= 400 or "error" in payload:
        err = payload.get("error", payload)
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise InstagramError(f"Instagram API error ({resp.status_code}): {msg}")
    return payload


def _wait_until_ready(container_id: str, timeout: int = 180) -> None:
    """Poll the container until Instagram has finished fetching/processing the media."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _request("GET", container_id, fields="status_code,status")
        status = data.get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(f"Container {container_id} failed: {data.get('status', status)}")
        time.sleep(3)
    raise InstagramError(f"Timed out waiting for container {container_id}")


def _publish_container(container_id: str) -> str:
    _wait_until_ready(container_id)
    result = _request("POST", f"{settings.instagram_account_id}/media_publish", creation_id=container_id)
    return result["id"]


def publish_single(image_url: str, caption: str) -> str:
    container = _request("POST", f"{settings.instagram_account_id}/media", image_url=image_url, caption=caption)
    return _publish_container(container["id"])


def publish_carousel(image_urls: list[str], caption: str) -> str:
    if not 2 <= len(image_urls) <= 10:
        raise InstagramError("A carousel needs between 2 and 10 images.")
    child_ids = []
    for url in image_urls:
        child = _request(
            "POST", f"{settings.instagram_account_id}/media", image_url=url, is_carousel_item="true"
        )
        child_ids.append(child["id"])
    for cid in child_ids:
        _wait_until_ready(cid)
    parent = _request(
        "POST",
        f"{settings.instagram_account_id}/media",
        media_type="CAROUSEL",
        children=",".join(child_ids),
        caption=caption,
    )
    return _publish_container(parent["id"])


def get_permalink(media_id: str) -> str | None:
    try:
        return _request("GET", media_id, fields="permalink").get("permalink")
    except InstagramError:
        return None


def publish(post_type: str, image_urls: list[str], caption: str) -> tuple[str, str | None]:
    """Publish a draft. Returns (media_id, permalink)."""
    logger.info("Publishing %s post with %d image(s)", post_type, len(image_urls))
    if post_type == "carousel":
        media_id = publish_carousel(image_urls, caption)
    else:
        media_id = publish_single(image_urls[0], caption)
    return media_id, get_permalink(media_id)
