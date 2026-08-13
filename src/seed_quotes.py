"""One-off: parse the source PDF into a pool of candidate quotes.

    python -m src.seed_quotes book/source.pdf [--limit-pages N] [--dry-run]

This is a manual curation step, not part of the daily Action. Run it, read the
summary, and spot-check the pool before going anywhere near a live account.

Pages are batched into groups before being sent to Claude, because a single
page rarely contains a quotable passage and the surrounding text is what tells
the model whether a sentence stands alone. Each extracted quote comes back with
a quality_score and a theme label.

Near-duplicate filtering happens here rather than in the database: books repeat
their best lines across chapters, and the UNIQUE constraint on quote_text only
catches character-identical repeats.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .db import connect, insert_quotes, pool_stats
from .generate_post import GenerationError, build_client, first_text
from .main import setup_logging

log = logging.getLogger("pgbot.seed")

# Pages per Claude call. Large enough for context, small enough to stay well
# inside a request and to keep one bad batch from costing much.
PAGES_PER_BATCH = 8

# Quotes above this similarity ratio are treated as the same quote.
DUPLICATE_THRESHOLD = 0.88

# Ideal length for a social quote. The model is asked to prefer this, not
# forced — a great 300-character quote beats a mediocre 200-character one.
IDEAL_MAX_CHARS = 280

# Hard ceiling, so a quote can never blow the caption limit on its own.
ABSOLUTE_MAX_CHARS = 1200

SEED_SYSTEM_PROMPT = f"""\
You extract quotable passages from a paragliding instructional book for a daily \
Instagram quote account. The audience is cross-country pilots.

From the pages you are given, extract only passages that genuinely stand alone. \
A passage qualifies when a pilot who has not read the book would understand it \
and get something from it. Reject anything that:

- refers to "the previous chapter", "figure 3", "as we saw above", or similar
- depends on a diagram, table, photo, or worked example
- is a bare instruction stripped of its reasoning ("set your trimmers to 50%")
- is administrative, biographical, or a caption
- reads as advice about specific gear models or regulations that will date

Prefer passages under {IDEAL_MAX_CHARS} characters. Never return one over \
{ABSOLUTE_MAX_CHARS} characters.

Quote text verbatim. Do not paraphrase, do not stitch sentences together from \
different paragraphs, and do not fix the author's grammar. Omit the surrounding \
quotation marks.

For each passage give:
- quote_text: the passage, verbatim
- source_page: the page number it appeared on
- theme: one lowercase word, chosen from fear, commitment, risk, technique, \
judgement, patience, weather, learning, mindset, or safety
- quality_score: 0.0-1.0, how well it works as a standalone social post. \
Reserve above 0.8 for passages that are genuinely memorable. Be strict — a pool \
where everything scores 0.9 is useless for ranking.

It is entirely acceptable to return an empty list for a batch. Most pages of \
most books contain nothing quotable, and padding the pool with weak passages \
costs more than it gains."""

SEED_SCHEMA = {
    "type": "object",
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote_text": {"type": "string"},
                    "source_page": {"type": "integer"},
                    "theme": {"type": "string"},
                    "quality_score": {"type": "number"},
                },
                "required": ["quote_text", "source_page", "theme", "quality_score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["quotes"],
    "additionalProperties": False,
}


@dataclass
class Page:
    number: int
    text: str


def extract_pages(pdf_path: Path, limit: int | None = None) -> list[Page]:
    """Pull page-level text out of the PDF, skipping pages that are mostly blank."""
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            if limit and index > limit:
                break
            text = (page.extract_text() or "").strip()
            # Front matter, plate pages and section dividers carry no quotes but
            # do cost tokens.
            if len(text) < 200:
                continue
            pages.append(Page(number=index, text=text))
    return pages


def batch_pages(pages: list[Page], size: int = PAGES_PER_BATCH) -> list[list[Page]]:
    return [pages[i : i + size] for i in range(0, len(pages), size)]


def _normalize(text: str) -> str:
    """Lowercased, punctuation-free form used only for similarity comparison."""
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def deduplicate(quotes: list[dict[str, Any]], threshold: float = DUPLICATE_THRESHOLD) -> list[dict]:
    """Drop near-identical quotes, keeping the highest-scoring version.

    Books repeat their best lines, often with small edits, so exact-match
    deduplication is not enough. Sorting by score first means the survivor of
    each cluster is the best-scored one.
    """
    ordered = sorted(quotes, key=lambda q: q.get("quality_score") or 0, reverse=True)
    kept: list[dict[str, Any]] = []
    kept_normalized: list[str] = []

    for quote in ordered:
        normalized = _normalize(quote.get("quote_text", ""))
        if not normalized:
            continue
        if any(
            SequenceMatcher(None, normalized, existing).ratio() >= threshold
            for existing in kept_normalized
        ):
            log.debug("Dropping near-duplicate: %.60s", quote.get("quote_text"))
            continue
        kept.append(quote)
        kept_normalized.append(normalized)
    return kept


def extract_from_batch(
    batch: list[Page], *, config: Config, client: Any, book_title: str, author: str | None
) -> list[dict[str, Any]]:
    """Ask Claude for the quotable passages in one batch of pages."""
    body = "\n\n".join(f"--- Page {page.number} ---\n{page.text}" for page in batch)

    response = client.messages.create(
        model=config.anthropic_model,
        max_tokens=16000,
        system=SEED_SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SEED_SCHEMA},
        },
        messages=[{"role": "user", "content": body}],
    )

    try:
        payload = json.loads(first_text(response))
    except json.JSONDecodeError as exc:
        raise GenerationError("Extraction response was not valid JSON") from exc

    results = []
    for item in payload.get("quotes", []):
        text = str(item.get("quote_text", "")).strip().strip('"“”')
        if not text or len(text) > ABSOLUTE_MAX_CHARS:
            log.debug("Rejecting quote of length %d", len(text))
            continue
        score = item.get("quality_score")
        results.append(
            {
                "quote_text": text,
                "author": author,
                "book_title": book_title,
                "source_page": item.get("source_page"),
                "theme": str(item.get("theme", "")).strip().lower() or None,
                "quality_score": max(0.0, min(1.0, float(score))) if score is not None else None,
            }
        )
    return results


def print_summary(stats: dict[str, Any]) -> None:
    """Score distribution, so the pool can be judged before going live."""
    print()
    print("=" * 60)
    print(f"Pool: {stats['total']} quotes ({stats['unused']} unused)")
    if stats["total"]:
        print(
            f"Score: min {stats['min_score']:.2f} / "
            f"avg {stats['avg_score']:.2f} / max {stats['max_score']:.2f}"
        )
        print()
        print("Distribution:")
        for bucket in stats["buckets"]:
            value = bucket["bucket"]
            count = bucket["n"]
            bar = "#" * min(count, 50)
            print(f"  {value:.1f}  {count:>4}  {bar}")
    print("=" * 60)
    print()
    print("Spot-check a few before going live:")
    print("  SELECT quality_score, theme, quote_text FROM quotes ORDER BY quality_score DESC;")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the quote pool from the source PDF.")
    parser.add_argument("pdf", type=Path, help="Path to the source book, e.g. book/source.pdf")
    parser.add_argument("--book-title", default=None, help="Attribution title (default: filename)")
    parser.add_argument("--author", default=None, help="Attribution author")
    parser.add_argument("--limit-pages", type=int, default=None, help="Only read the first N pages")
    parser.add_argument(
        "--batch-size", type=int, default=PAGES_PER_BATCH, help="Pages per Claude call"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Extract and summarize without writing to the database"
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.log_level)

    if not args.pdf.exists():
        log.error("No such file: %s", args.pdf)
        return 1

    book_title = args.book_title or args.pdf.stem.replace("-", " ").replace("_", " ").title()

    log.info("Reading %s", args.pdf)
    pages = extract_pages(args.pdf, args.limit_pages)
    if not pages:
        log.error("No readable text found. If the PDF is scanned images, it needs OCR first.")
        return 1

    batches = batch_pages(pages, args.batch_size)
    log.info("Extracted %d pages of text in %d batch(es)", len(pages), len(batches))

    client = build_client(config)
    collected: list[dict[str, Any]] = []
    failed_batches = 0

    for index, batch in enumerate(batches, start=1):
        span = f"pages {batch[0].number}-{batch[-1].number}"
        log.info("Batch %d/%d (%s)", index, len(batches), span)
        try:
            found = extract_from_batch(
                batch, config=config, client=client, book_title=book_title, author=args.author
            )
        except Exception as exc:
            # One bad batch shouldn't cost the whole book — a partial pool is
            # still useful and the step can be re-run.
            failed_batches += 1
            log.warning("Batch %d (%s) failed: %s", index, span, exc)
            continue
        log.info("  found %d quote(s)", len(found))
        collected.extend(found)

    if failed_batches:
        log.warning("%d of %d batches failed", failed_batches, len(batches))

    log.info("Extracted %d candidate quotes", len(collected))
    unique = deduplicate(collected)
    log.info("After near-duplicate filtering: %d", len(unique))

    if args.dry_run:
        log.info("--dry-run: not writing to the database")
        for quote in unique[:20]:
            print(f"\n[{quote['quality_score']:.2f}] ({quote['theme']}) {quote['quote_text']}")
        return 0

    with connect(config) as db:
        db.init_schema()
        inserted = insert_quotes(db, unique)
        log.info("Inserted %d new quote(s) (%d were already present)", inserted, len(unique) - inserted)
        print_summary(pool_stats(db))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
