"""Choose a photo to pair with a quote.

Resolution order:
  1. an entry in images/library/manifest.json tagged with the quote's theme
  2. any entry in the library
  3. a file in images/generated/ (logs a warning — this path means the library
     is empty or entirely broken, which shouldn't happen once photos are added)

Manifest entries whose file is missing on disk are skipped everywhere. A URL
that 404s makes the Graph API reject the whole post, and the failure surfaces
as an opaque Meta error rather than a missing-file one, so it's much cheaper to
catch it here.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (
    GENERATED_DIR,
    LIBRARY_DIR,
    MANIFEST_PATH,
    REPO_ROOT,
    SUPPORTED_IMAGE_SUFFIXES,
)

log = logging.getLogger(__name__)


class NoImagesAvailable(RuntimeError):
    """Neither the library nor the generated directory yielded a usable image."""


@dataclass(frozen=True)
class ImageChoice:
    """A picked image and how it was picked."""

    path: Path  # absolute path on disk
    relative_path: str  # repo-relative, forward slashes — used to build the URL
    source: str  # "theme-match" | "library-random" | "generated-fallback"
    alt_text: str | None = None
    entry: dict[str, Any] = field(default_factory=dict)


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX path.

    Forward slashes matter: this string goes straight into a URL, and the job
    may run on Windows locally. An image outside the repo can't be served from
    it at all, so that's an error rather than a silently wrong URL.
    """
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise NoImagesAvailable(
            f"Image {path} is outside the repo root {repo_root}, so it cannot be "
            "served from the repo's public URL."
        ) from exc


def load_manifest(manifest_path: Path | None = None) -> list[dict[str, Any]]:
    """Read manifest.json. A missing or malformed manifest is not fatal —
    selection degrades to the library-random path instead of failing the run."""
    manifest_path = manifest_path or MANIFEST_PATH
    if not manifest_path.exists():
        log.warning("No manifest at %s — falling back to untagged selection", manifest_path)
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("Manifest at %s is not valid JSON — ignoring it", manifest_path, exc_info=True)
        return []
    entries = data.get("images", []) if isinstance(data, dict) else data
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("filename")]


def _entry_tags(entry: dict[str, Any]) -> set[str]:
    """All lowercased tags on an entry that a theme could match against."""
    tags: set[str] = set()
    raw_themes = entry.get("themes") or []
    if isinstance(raw_themes, str):
        raw_themes = [raw_themes]
    tags.update(str(theme).strip().lower() for theme in raw_themes)
    for key in ("mood", "site"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            tags.add(value.strip().lower())
    return {tag for tag in tags if tag}


def _existing_library_entries(
    entries: list[dict[str, Any]], library_dir: Path
) -> list[tuple[dict[str, Any], Path]]:
    """Pair each manifest entry with its file, dropping the ones that are missing."""
    usable = []
    for entry in entries:
        path = library_dir / str(entry["filename"])
        if not path.exists():
            log.warning("Manifest lists %s but the file is missing — skipping", path.name)
            continue
        usable.append((entry, path))
    return usable


def _scan_directory(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
    )


def pick_image(
    theme: str | None,
    *,
    library_dir: Path | None = None,
    generated_dir: Path | None = None,
    manifest_path: Path | None = None,
    repo_root: Path | None = None,
    rng: random.Random | None = None,
) -> ImageChoice:
    """Pick an image for `theme`, falling back as described in the module docstring.

    The directory, repo_root, and rng arguments exist so tests can drive this
    against a temporary library with deterministic choices.
    """
    library_dir = library_dir or LIBRARY_DIR
    generated_dir = generated_dir or GENERATED_DIR
    repo_root = repo_root or REPO_ROOT
    rng = rng or random.Random()

    entries = load_manifest(manifest_path or (library_dir / "manifest.json"))
    usable = _existing_library_entries(entries, library_dir)

    # 1. Theme match.
    normalized = (theme or "").strip().lower()
    if normalized:
        matches = [(entry, path) for entry, path in usable if normalized in _entry_tags(entry)]
        if matches:
            entry, path = rng.choice(matches)
            log.info("Matched theme %r to %s", theme, path.name)
            return ImageChoice(
                path=path,
                relative_path=_relative_to_repo(path, repo_root),
                source="theme-match",
                alt_text=entry.get("alt_text"),
                entry=entry,
            )
        log.info("No image tagged %r — picking from the library at random", theme)

    # 2. Any library image. Prefer manifest entries so alt text comes along,
    #    but fall back to whatever is on disk if the manifest is empty.
    if usable:
        entry, path = rng.choice(usable)
        return ImageChoice(
            path=path,
            relative_path=_relative_to_repo(path, repo_root),
            source="library-random",
            alt_text=entry.get("alt_text"),
            entry=entry,
        )

    untagged = _scan_directory(library_dir)
    if untagged:
        log.warning("Library has %d image(s) but no usable manifest entries", len(untagged))
        path = rng.choice(untagged)
        return ImageChoice(
            path=path,
            relative_path=_relative_to_repo(path, repo_root),
            source="library-random",
        )

    # 3. Generated fallback.
    generated = _scan_directory(generated_dir)
    if generated:
        log.warning(
            "Image library is empty — falling back to %s. Add tagged photos to %s.",
            generated_dir.name,
            library_dir,
        )
        path = rng.choice(generated)
        return ImageChoice(
            path=path,
            relative_path=_relative_to_repo(path, repo_root),
            source="generated-fallback",
        )

    raise NoImagesAvailable(
        f"No usable images in {library_dir} or {generated_dir}. "
        "Add photos to images/library/ and list them in manifest.json."
    )
