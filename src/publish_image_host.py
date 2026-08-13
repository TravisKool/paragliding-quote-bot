"""Make a local image publicly reachable and return its URL.

The Graph API fetches the image itself, so the URL has to resolve from the
public internet before the post is created. Library photos are already committed
and need nothing but a URL. An image produced mid-run (the generated fallback)
has to be committed and pushed first.

After a push, raw.githubusercontent.com can lag by a few seconds, so the URL is
polled until it resolves rather than handed straight to Meta — a 404 there comes
back as an opaque Meta error that says nothing about the real cause.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from .config import Config, ConfigError, load_config

log = logging.getLogger(__name__)

# Identity used when the Action has to commit an image mid-run.
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

REACHABILITY_ATTEMPTS = 6
REACHABILITY_DELAY_SECONDS = 5


class ImageHostError(RuntimeError):
    """The image could not be made publicly reachable."""


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def public_url(relative_path: str, config: Config | None = None) -> str:
    """Build the public URL for a repo-relative image path.

    IMAGE_BASE_URL points at the repo's `images/` directory, so the leading
    `images/` segment of the path is dropped before joining. Each remaining
    segment is percent-encoded, since filenames with spaces are likely in a
    hand-curated photo library.
    """
    config = config or load_config()
    if not config.image_base_url:
        raise ConfigError(
            "IMAGE_BASE_URL is not set — it must point at the images/ directory "
            "of this repo (see .env.example)."
        )

    path = relative_path.replace("\\", "/").lstrip("/")
    prefix = "images/"
    if path.startswith(prefix):
        path = path[len(prefix) :]

    encoded = "/".join(quote(segment) for segment in path.split("/") if segment)
    return f"{config.image_base_url}/{encoded}"


def is_committed(path: Path, repo_root: Path) -> bool:
    """True when the file is tracked by git with no uncommitted modifications."""
    tracked = _git(
        "ls-files", "--error-unmatch", str(path), cwd=repo_root, check=False
    )
    if tracked.returncode != 0:
        return False
    modified = _git("status", "--porcelain", "--", str(path), cwd=repo_root)
    return not modified.stdout.strip()


def commit_and_push(path: Path, repo_root: Path) -> None:
    """Commit a single image and push it, so its raw URL resolves.

    Only needed for images generated during a run. The bot identity is passed
    per-command rather than written to the repo config, so this leaves no trace
    in the runner's git configuration.
    """
    log.info("Committing %s so it can be served publicly", path.name)
    _git("add", "--", str(path), cwd=repo_root)

    staged = _git("diff", "--cached", "--quiet", cwd=repo_root, check=False)
    if staged.returncode == 0:
        log.info("Nothing staged for %s — already committed", path.name)
        return

    _git(
        "-c", f"user.name={BOT_NAME}",
        "-c", f"user.email={BOT_EMAIL}",
        "commit", "-m", f"Add generated image {path.name}",
        cwd=repo_root,
    )
    push = _git("push", cwd=repo_root, check=False)
    if push.returncode != 0:
        raise ImageHostError(
            f"Could not push {path.name}, so its URL will not resolve: {push.stderr.strip()}"
        )


def wait_until_reachable(
    url: str,
    *,
    attempts: int = REACHABILITY_ATTEMPTS,
    delay: float = REACHABILITY_DELAY_SECONDS,
    session: object | None = None,
) -> bool:
    """Poll the URL until it returns 2xx. Returns False if it never does."""
    import requests

    http = session or requests
    for attempt in range(1, attempts + 1):
        try:
            response = http.head(url, timeout=10, allow_redirects=True)
            if response.status_code < 400:
                return True
            log.info(
                "Image URL not ready (HTTP %s), attempt %d/%d",
                response.status_code,
                attempt,
                attempts,
            )
        except Exception as exc:  # network hiccup — worth retrying
            log.info("Image URL check failed (%s), attempt %d/%d", exc, attempt, attempts)
        if attempt < attempts:
            time.sleep(delay)
    return False


def ensure_public_url(
    image_path: Path,
    relative_path: str,
    *,
    config: Config | None = None,
    repo_root: Path | None = None,
    verify: bool = True,
) -> str:
    """Return a public URL for the image, committing it first if necessary."""
    config = config or load_config()
    repo_root = repo_root or Path(__file__).resolve().parent.parent

    if not image_path.exists():
        raise ImageHostError(f"Image does not exist: {image_path}")

    url = public_url(relative_path, config)

    if not is_committed(image_path, repo_root):
        commit_and_push(image_path, repo_root)

    if verify and not wait_until_reachable(url):
        raise ImageHostError(
            f"{url} did not become reachable. The Graph API fetches this URL itself, "
            "so publishing would fail with an unhelpful error. Check that the image is "
            "pushed and that IMAGE_BASE_URL matches the repo and branch."
        )
    return url
