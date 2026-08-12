"""One-off go-live seeding: python -m src.init_launch [--dry-run]

Phase 9. Runs the main flow LAUNCH_POST_COUNT times against the highest-scoring
unused quotes, sleeping LAUNCH_DELAY_SECONDS between posts so it doesn't read as
a spam burst. --dry-run does everything except the Graph API publish, so the
captions and image pairings can be reviewed first.

Not wired into any schedule.
"""
