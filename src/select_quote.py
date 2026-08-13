"""Pick the next quote(s) to post.

Selection never marks anything used — main.py does that only after a successful
publish, so a failed run doesn't burn a quote.

`candidates()` returns several rather than one because a quote can turn out to
be unpostable (Claude declines to caption it, its text exceeds the caption
limit). Retrying the same quote tomorrow would fail the same way and wedge the
schedule, so main.py moves down the list instead.
"""

from __future__ import annotations

import logging

from .db import Database, Row, top_unused_quotes, unused_quote_count

log = logging.getLogger(__name__)

# Warn while there's still time to re-seed the pool before it runs dry.
LOW_POOL_THRESHOLD = 14


class QuotePoolEmpty(RuntimeError):
    """Every quote has been posted. Re-run the seed step."""


def candidates(db: Database, limit: int = 3) -> list[Row]:
    """The best unused quotes, best first."""
    quotes = top_unused_quotes(db, limit)
    if not quotes:
        raise QuotePoolEmpty(
            "No unused quotes left. Re-seed with: python -m src.seed_quotes book/source.pdf"
        )

    remaining = unused_quote_count(db)
    if remaining <= LOW_POOL_THRESHOLD:
        log.warning(
            "Only %d unused quote(s) left — about %d days of posts. Re-seed soon.",
            remaining,
            remaining,
        )
    return quotes


def next_quote(db: Database) -> Row:
    """The single best unused quote."""
    return candidates(db, limit=1)[0]
