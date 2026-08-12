"""Exchange the current long-lived IG token for a fresh one.

Phase 6. Long-lived tokens last ~60 days; this runs on its own schedule (~every
50 days) and writes the new value back to the IG_ACCESS_TOKEN repo secret via
the GitHub API, using a separate repo-scoped PAT stored as its own secret.

The *first* token must be obtained by hand — see README "External setup".
"""
