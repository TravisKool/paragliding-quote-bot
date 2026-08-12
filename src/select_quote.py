"""Pick the next quote to post.

Phase 7. Selects from `quotes` where used_at IS NULL, ordered by quality_score
DESC. Never marks the quote used — main.py does that only after a successful
publish, so a failed run doesn't burn a quote.
"""
