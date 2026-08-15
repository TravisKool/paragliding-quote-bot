"""Environment loading and shared constants.

Every other module reads configuration from here rather than touching os.environ
directly, so the required-variable checks live in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # no-op in CI, where the values come from Actions secrets

# --- Repo layout -------------------------------------------------------
# Paths are resolved from this file so commands work from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = REPO_ROOT / "images"
LIBRARY_DIR = IMAGES_DIR / "library"
GENERATED_DIR = IMAGES_DIR / "generated"
MANIFEST_PATH = LIBRARY_DIR / "manifest.json"
BOOK_DIR = REPO_ROOT / "book"

# Extensions the Graph API will accept for a feed image.
SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})

# Optional fonts committed to the repo. Anything dropped here is preferred over
# system fonts, which is the only way to get a consistent look between a local
# Windows run and the Ubuntu runner the daily Action uses.
FONTS_DIR = REPO_ROOT / "assets" / "fonts"

# --- Quote cards -------------------------------------------------------
# Used when there is no photo library: the quote is typeset onto a generated
# background instead. Square, because it is the safest feed crop.
CARD_SIZE = 1080
CARD_MARGIN = 110  # keeps text clear of the feed's rounded corners

# --- Instagram limits (Graph API) --------------------------------------
CAPTION_MAX_CHARS = 2200
HASHTAG_MAX_COUNT = 30
HASHTAG_TARGET_COUNT = 8  # 5-10 well-chosen tags outperform stuffing
ALT_TEXT_MAX_CHARS = 1000

# --- Graph API ---------------------------------------------------------
# This bot uses the *Instagram API with Instagram Login*, so calls go to
# graph.instagram.com rather than graph.facebook.com. That path needs no linked
# Facebook Page and no business portfolio — the account authorises the app
# directly. Endpoint shapes are otherwise identical to the Facebook-Login flow.
GRAPH_API_VERSION = "v26.0"
GRAPH_API_HOST = "https://graph.instagram.com"
GRAPH_API_BASE = f"{GRAPH_API_HOST}/{GRAPH_API_VERSION}"

# --- Launch ------------------------------------------------------------
LAUNCH_POST_COUNT = 8
LAUNCH_DELAY_SECONDS = 300  # spacing between seed posts, so it doesn't look like a burst


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Required environment variable {name} is not set. "
            "See .env.example and the README setup section."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


IMAGE_MODES = frozenset({"auto", "card", "photo"})


def _image_mode() -> str:
    """How the daily post gets its image.

    auto  — use the photo library, and fall back to a generated quote card when
            it has nothing usable. This is what an empty library needs.
    card  — always generate a card, even if photos exist.
    photo — only use the library, and fail the run rather than posting a card.

    An unrecognised value falls back to auto with a warning rather than failing:
    a typo here should not take the daily post down.
    """
    raw = _optional("IMAGE_MODE", "auto").lower()
    if raw and raw not in IMAGE_MODES:
        import logging

        logging.getLogger(__name__).warning(
            "IMAGE_MODE=%r is not one of %s — using 'auto'", raw, sorted(IMAGE_MODES)
        )
        return "auto"
    return raw or "auto"


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    anthropic_model: str
    turso_database_url: str
    turso_auth_token: str
    local_db_path: str
    ig_user_id: str
    ig_access_token: str
    meta_app_id: str
    meta_app_secret: str
    image_base_url: str
    image_mode: str  # "auto" | "card" | "photo"
    dry_run: bool
    log_level: str

    @property
    def use_turso(self) -> bool:
        """True when a remote Turso database is configured.

        Falsy locally and in tests, where db.py falls back to a SQLite file so
        the suite runs without live credentials.
        """
        return bool(self.turso_database_url)

    def require_anthropic(self) -> None:
        if not self.anthropic_api_key:
            raise ConfigError("ANTHROPIC_API_KEY is required for this command.")

    def require_instagram(self) -> None:
        for name, value in (
            ("IG_USER_ID", self.ig_user_id),
            ("IG_ACCESS_TOKEN", self.ig_access_token),
            ("IMAGE_BASE_URL", self.image_base_url),
        ):
            if not value:
                raise ConfigError(f"{name} is required to publish to Instagram.")


def load_config() -> Config:
    """Read configuration from the environment.

    Nothing is validated as required here — individual commands call the
    `require_*` helpers for the subset they actually need, so `pytest` and
    `seed_quotes` don't demand Instagram credentials.
    """
    return Config(
        anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
        anthropic_model=_optional("ANTHROPIC_MODEL", "claude-opus-5"),
        turso_database_url=_optional("TURSO_DATABASE_URL"),
        turso_auth_token=_optional("TURSO_AUTH_TOKEN"),
        local_db_path=_optional("LOCAL_DB_PATH", "local.db"),
        ig_user_id=_optional("IG_USER_ID"),
        ig_access_token=_optional("IG_ACCESS_TOKEN"),
        meta_app_id=_optional("META_APP_ID"),
        meta_app_secret=_optional("META_APP_SECRET"),
        image_base_url=_optional("IMAGE_BASE_URL").rstrip("/"),
        image_mode=_image_mode(),
        dry_run=_flag("DRY_RUN"),
        log_level=_optional("LOG_LEVEL", "INFO").upper(),
    )


__all__ = [
    "ALT_TEXT_MAX_CHARS",
    "BOOK_DIR",
    "CAPTION_MAX_CHARS",
    "CARD_MARGIN",
    "CARD_SIZE",
    "FONTS_DIR",
    "GENERATED_DIR",
    "IMAGE_MODES",
    "GRAPH_API_BASE",
    "GRAPH_API_HOST",
    "GRAPH_API_VERSION",
    "HASHTAG_MAX_COUNT",
    "HASHTAG_TARGET_COUNT",
    "IMAGES_DIR",
    "LAUNCH_DELAY_SECONDS",
    "LAUNCH_POST_COUNT",
    "LIBRARY_DIR",
    "MANIFEST_PATH",
    "REPO_ROOT",
    "SUPPORTED_IMAGE_SUFFIXES",
    "Config",
    "ConfigError",
    "load_config",
]
