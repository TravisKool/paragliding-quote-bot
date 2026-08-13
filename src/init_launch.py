"""One-off go-live seeding.

    python -m src.init_launch --dry-run    # review captions and pairings first
    python -m src.init_launch              # publish for real

Runs the same flow as main.py eight times against the highest-scoring unused
quotes, pausing between posts so the account doesn't look like a burst of spam
on day one. Not wired into any schedule.

Always dry-run this first. It is the only chance to see all eight captions and
image pairings before they are on a public account — and unlike the daily job,
a bad batch here is eight posts to delete, not one.
"""

from __future__ import annotations

import argparse
import logging
import time

from .config import LAUNCH_DELAY_SECONDS, LAUNCH_POST_COUNT, load_config
from .db import connect, unused_quote_count
from .main import PostFailed, publish_one, setup_logging

log = logging.getLogger("pgbot.launch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the go-live batch of posts.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except publishing, and print each caption for review.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=LAUNCH_POST_COUNT,
        help=f"How many posts to publish (default {LAUNCH_POST_COUNT}).",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=LAUNCH_DELAY_SECONDS,
        help=f"Seconds between posts (default {LAUNCH_DELAY_SECONDS}).",
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.log_level)
    dry_run = args.dry_run or config.dry_run

    if not dry_run:
        log.warning(
            "PUBLISHING FOR REAL: %d posts, %ds apart. Ctrl-C now if you meant --dry-run.",
            args.count,
            args.delay,
        )

    published = 0
    with connect(config) as db:
        db.init_schema()

        available = unused_quote_count(db)
        if available < args.count:
            log.warning(
                "Only %d unused quote(s) available but %d requested — will publish %d.",
                available,
                args.count,
                available,
            )

        for number in range(1, args.count + 1):
            log.info("--- Launch post %d of %d ---", number, args.count)
            try:
                publish_one(db, config, dry_run=dry_run)
                published += 1
            except PostFailed as exc:
                # Stop rather than churn through the pool: whatever broke will
                # almost certainly break on the next quote too.
                log.error("Stopping after %d post(s): %s", published, exc)
                return 1
            except Exception as exc:
                log.error("Stopping after %d post(s): %s", published, exc, exc_info=True)
                return 1

            # A dry run has no rate-limit or spam concern, so don't make the
            # reviewer sit through the delays.
            if number < args.count and not dry_run:
                log.info("Sleeping %ds before the next post", args.delay)
                time.sleep(args.delay)

    verb = "would publish" if dry_run else "published"
    log.info("Launch complete: %s %d post(s).", verb, published)
    if dry_run:
        log.info("Re-run without --dry-run to publish for real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
