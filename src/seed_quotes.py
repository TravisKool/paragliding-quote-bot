"""One-off: parse the source PDF into a pool of candidate quotes.

Phase 2. Run manually, never from the daily Action:

    python -m src.seed_quotes book/source.pdf

Flow: pdfplumber page text -> batched Claude calls that extract self-contained
quotes (<=280 chars ideally) with a quality_score (0-1) and a short theme label
-> near-duplicate filtering -> insert into `quotes`. Prints a count and score
distribution afterwards so the pool can be spot-checked before going live.
"""
