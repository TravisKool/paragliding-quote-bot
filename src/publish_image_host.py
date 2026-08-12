"""Make a local image publicly reachable and return its URL.

Phase 5. Library images are already committed, so the common path just joins
config.IMAGE_BASE_URL with the repo-relative path. The uncommon path — an image
generated mid-run — needs a git config/commit/push from inside the Action
before the URL resolves.
"""
