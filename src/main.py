"""Daily orchestration entrypoint.

    python -m src.main [--dry-run]

Flow: pick a quote -> pick a photo -> write the caption and alt text -> make the
image publicly reachable -> publish -> record the post and mark the quote used.

Two invariants:

  - Nothing marks a quote used until the publish succeeds. A failed run leaves
    the quote in the pool for tomorrow.
  - Any unrecoverable failure exits non-zero, so GitHub's workflow-failure email
    fires. That email is the only alerting this system has, so nothing here
    swallows an exception.

Image selection runs *before* caption generation, so alt text can be written
from the actual photo. (The original plan ordered it the other way round.)
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from .config import Config, load_config
from .db import Database, Row, connect, record_failed_post, record_successful_post
from .generate_post import CaptionRefused, GeneratedPost, GenerationError, build_post
from .instagram_client import InstagramClient
from .pick_image import ImageChoice, pick_image
from .publish_image_host import ensure_public_url
from .select_quote import candidates

log = logging.getLogger("pgbot")

# How many quotes to try before giving up on a run.
MAX_CANDIDATES = 3


class PostFailed(RuntimeError):
    """The run could not publish anything."""


@dataclass
class PostOutcome:
    quote: Row
    image: ImageChoice
    post: GeneratedPost
    image_url: str
    media_id: str | None  # None on a dry run
    dry_run: bool


def setup_logging(level: str = "INFO") -> None:
    """Timestamped, level-prefixed logs — Action logs need to be readable at 2am."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
        force=True,
    )


def _preview(outcome: PostOutcome) -> str:
    """Human-readable summary, for dry runs and for the Action log."""
    lines = [
        "",
        "=" * 72,
        f"Quote #{outcome.quote['id']}  theme={outcome.quote.get('theme')!r}  "
        f"score={outcome.quote.get('quality_score')}",
        f"Image: {outcome.image.relative_path}  ({outcome.image.source})",
        f"URL:   {outcome.image_url}",
        f"Alt:   {outcome.post.alt_text or '(none)'}",
        "-" * 72,
        outcome.post.caption,
        "=" * 72,
    ]
    return "\n".join(lines)


def publish_one(
    db: Database,
    config: Config,
    *,
    dry_run: bool = False,
    client: object | None = None,
    instagram: InstagramClient | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> PostOutcome:
    """Publish a single post. Raises PostFailed if no candidate could be posted."""
    quotes = candidates(db, limit=max_candidates)
    last_error: Exception | None = None

    for index, quote in enumerate(quotes, start=1):
        log.info(
            "Candidate %d/%d: quote #%s (score=%s, theme=%r)",
            index,
            len(quotes),
            quote["id"],
            quote.get("quality_score"),
            quote.get("theme"),
        )
        try:
            return _attempt(
                db, config, quote, dry_run=dry_run, client=client, instagram=instagram
            )
        except (CaptionRefused, GenerationError) as exc:
            # This quote is the problem, not the run. Move on rather than
            # failing the same way again tomorrow.
            last_error = exc
            log.warning("Quote #%s is not usable (%s) — trying the next one", quote["id"], exc)

    raise PostFailed(
        f"None of the {len(quotes)} candidate quotes could be posted. Last error: {last_error}"
    ) from last_error


def _attempt(
    db: Database,
    config: Config,
    quote: Row,
    *,
    dry_run: bool,
    client: object | None,
    instagram: InstagramClient | None,
) -> PostOutcome:
    image = pick_image(quote.get("theme"))
    log.info("Selected %s (%s)", image.relative_path, image.source)

    post = build_post(
        quote,
        image_path=image.path,
        manifest_alt_text=image.alt_text,
        config=config,
        client=client,
    )
    log.info("Caption is %d chars with %d hashtags", len(post.caption), len(post.hashtags))

    image_url = ensure_public_url(
        image.path, image.relative_path, config=config, verify=not dry_run
    )

    if dry_run:
        outcome = PostOutcome(
            quote=quote, image=image, post=post, image_url=image_url,
            media_id=None, dry_run=True,
        )
        log.info("DRY RUN — not publishing%s", _preview(outcome))
        return outcome

    ig = instagram or InstagramClient(config=config)
    try:
        result = ig.publish(image_url, post.caption, post.alt_text)
    except Exception:
        # Record the attempt for the audit trail, but leave used_at alone so
        # the quote comes back around tomorrow.
        record_failed_post(
            db,
            quote_id=quote["id"],
            caption_body=post.caption,
            image_path=image.relative_path,
            image_url=image_url,
        )
        raise

    record_successful_post(
        db,
        quote_id=quote["id"],
        caption_body=post.caption,
        image_path=image.relative_path,
        image_url=image_url,
        instagram_media_id=result.media_id,
    )
    log.info("Published media %s for quote #%s", result.media_id, quote["id"])
    return PostOutcome(
        quote=quote, image=image, post=post, image_url=image_url,
        media_id=result.media_id, dry_run=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish today's paragliding quote.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except the Instagram publish, and print the result.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.log_level)
    dry_run = args.dry_run or config.dry_run

    try:
        with connect(config) as db:
            db.init_schema()
            outcome = publish_one(db, config, dry_run=dry_run)
    except Exception as exc:
        # Exit non-zero so the workflow fails and GitHub emails about it.
        log.error("Run failed: %s", exc, exc_info=True)
        return 1

    if not dry_run:
        log.info("Done.%s", _preview(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
