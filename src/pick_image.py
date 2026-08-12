"""Match a photo to a quote's theme.

Phase 3. Reads images/library/manifest.json and resolves, in order:
  1. best tag match against the quote's theme
  2. random pick from the library
  3. images/generated/ (logs a warning — should be rare once the library
     is populated)
"""
