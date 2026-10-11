"""Central configuration, loaded from environment variables (GitHub Actions secrets/vars, or .env locally)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    # GitHub Actions passes unset variables as empty strings, so an empty value means "use the default".
    value = os.getenv(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


DEFAULT_FEEDS = (
    "https://techcrunch.com/category/artificial-intelligence/feed/,"
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml,"
    "https://openai.com/news/rss.xml"
)


@dataclass(frozen=True)
class Settings:
    # --- Required secrets -------------------------------------------------
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    instagram_access_token: str = field(default_factory=lambda: _env("INSTAGRAM_ACCESS_TOKEN"))
    instagram_account_id: str = field(default_factory=lambda: _env("INSTAGRAM_ACCOUNT_ID"))
    # "owner/repo" - set automatically by GitHub Actions. Used to build the public image URLs.
    github_repository: str = field(default_factory=lambda: _env("GITHUB_REPOSITORY"))
    # Optional override of the public folder URL that serves the draft images.
    media_base_url_override: str = field(default_factory=lambda: _env("MEDIA_BASE_URL"))

    # --- Optional tuning --------------------------------------------------
    gemini_text_model: str = field(default_factory=lambda: _env("GEMINI_TEXT_MODEL", "gemini-3.8-flash"))
    gemini_fallback_model: str = field(default_factory=lambda: _env("GEMINI_FALLBACK_MODEL"))
    gemini_image_model: str = field(default_factory=lambda: _env("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image"))
    # Image provider: "cloudflare" (free tier), "gemini" (paid) or "none" (gradient + text only).
    # Leave IMAGE_PROVIDER empty to auto-pick: cloudflare if its credentials are set, otherwise gemini.
    image_provider_setting: str = field(default_factory=lambda: _env("IMAGE_PROVIDER").lower())
    cloudflare_account_id: str = field(default_factory=lambda: _env("CLOUDFLARE_ACCOUNT_ID"))
    cloudflare_api_token: str = field(default_factory=lambda: _env("CLOUDFLARE_API_TOKEN"))
    cloudflare_image_model: str = field(
        default_factory=lambda: _env("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
    )
    # --- Real photos (Unsplash) -------------------------------------------------
    # Free "Access Key" from https://unsplash.com/oauth/applications. Empty = photo mode off (AI images only).
    unsplash_access_key: str = field(default_factory=lambda: _env("UNSPLASH_ACCESS_KEY"))
    # Name of YOUR app as registered at Unsplash. Used in the required ?utm_source=<name>&utm_medium=referral links.
    # Unsplash forbids "Unsplash" inside an app name.
    unsplash_app_name: str = field(default_factory=lambda: _env("UNSPLASH_APP_NAME", "ai-influencer-bot"))
    # Candidates the AI looks at per slide, and how many are kept so you can swap without new API calls.
    photo_candidates: int = field(default_factory=lambda: _int("PHOTO_CANDIDATES", 4))
    photo_pool: int = field(default_factory=lambda: _int("PHOTO_POOL", 8))
    # Generative edits of a real photo (img2img) are slow and low-res on free models -> keep them rare.
    max_photo_edits: int = field(default_factory=lambda: _int("MAX_PHOTO_EDITS", 2))
    # Style for AI pictures that fill in when no photo fits (only used while Unsplash is enabled).
    realistic_style: str = field(
        default_factory=lambda: _env(
            "REALISTIC_STYLE",
            "realistic documentary photograph, natural light, shallow depth of field, muted colours, "
            "35mm photo, high detail",
        )
    )

    # --- Visual template / branding ----------------------------------------
    brand_handle: str = field(default_factory=lambda: _env("BRAND_HANDLE"))            # e.g. @yourname
    brand_kicker: str = field(default_factory=lambda: _env("BRAND_KICKER", "AI & TECH DAILY"))
    brand_accent: str = field(default_factory=lambda: _env("BRAND_ACCENT", "#8B5CF6"))  # hex colour
    image_style: str = field(
        default_factory=lambda: _env(
            "IMAGE_STYLE",
            "modern editorial tech illustration, deep indigo and cyan neon glow, minimal, "
            "cinematic lighting, high detail",
        )
    )
    # Max number of AI-generated backgrounds per carousel (one per slide). Rest get designed gradients.
    max_slide_images: int = field(default_factory=lambda: _int("MAX_SLIDE_IMAGES", 7))
    single_post_overlay: bool = field(
        default_factory=lambda: _env("SINGLE_POST_OVERLAY", "true").lower() not in ("0", "false", "no", "off")
    )
    instagram_graph_version: str = field(default_factory=lambda: _env("INSTAGRAM_GRAPH_VERSION", "v22.0"))

    rss_feeds: tuple[str, ...] = field(
        default_factory=lambda: tuple(u.strip() for u in _env("RSS_FEEDS", DEFAULT_FEEDS).split(",") if u.strip())
    )
    max_article_age_hours: int = field(default_factory=lambda: _int("MAX_ARTICLE_AGE_HOURS", 48))
    candidates_per_run: int = field(default_factory=lambda: _int("CANDIDATES_PER_RUN", 5))

    # State + draft images live in this folder (in GitHub Actions: a checkout of the `data` branch).
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "./data"))

    # --- Derived ----------------------------------------------------------
    @property
    def media_base_url(self) -> str:
        """Public https folder from which Instagram downloads the images."""
        if self.media_base_url_override:
            return self.media_base_url_override.rstrip("/")
        return f"https://raw.githubusercontent.com/{self.github_repository}/data/media"

    @property
    def image_provider(self) -> str:
        if self.image_provider_setting in ("cloudflare", "gemini", "none"):
            return self.image_provider_setting
        if self.cloudflare_account_id and self.cloudflare_api_token:
            return "cloudflare"
        return "gemini"

    @property
    def photos_enabled(self) -> bool:
        return bool(self.unsplash_access_key)

    @property
    def media_dir(self) -> Path:
        return Path(self.data_dir) / "media"

    @property
    def state_file(self) -> Path:
        return Path(self.data_dir) / "state.json"

    def validate(self) -> None:
        required = {
            "GEMINI_API_KEY": self.gemini_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "INSTAGRAM_ACCESS_TOKEN": self.instagram_access_token,
            "INSTAGRAM_ACCOUNT_ID": self.instagram_account_id,
        }
        missing = [k for k, v in required.items() if not v]
        if not (self.github_repository or self.media_base_url_override):
            missing.append("GITHUB_REPOSITORY (or MEDIA_BASE_URL)")
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        if not self.media_base_url.startswith("https://"):
            raise RuntimeError("MEDIA_BASE_URL must be a public https:// URL (Instagram downloads the images from it).")
        try:
            int(self.telegram_chat_id)
        except ValueError as exc:
            raise RuntimeError("TELEGRAM_CHAT_ID must be a numeric chat id.") from exc


settings = Settings()
settings.media_dir.mkdir(parents=True, exist_ok=True)
