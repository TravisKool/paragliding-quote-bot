"""Rotate the long-lived Instagram access token before it expires.

Long-lived tokens last ~60 days and can be refreshed for another 60 at any point
after they're 24 hours old. This runs on its own schedule (every ~50 days) and
writes the new value back to the IG_ACCESS_TOKEN repository secret.

    python scripts/refresh_ig_token.py [--dry-run]

The *first* token must be obtained by hand — see the README. This script can
only extend a token that already works; once one expires there is nothing left
to refresh and you have to reauthorise the account in the App Dashboard under
"API setup with Instagram login".

Writing the secret uses `gh secret set`, which is preinstalled on GitHub-hosted
runners. That avoids a PyNaCl dependency just to libsodium-seal one value for
the REST API. `gh` authenticates from GH_TOKEN, which must be a repo-scoped PAT
with the `secrets` write permission — the default GITHUB_TOKEN cannot write
secrets.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

# Allow running as a script from anywhere in the repo.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import GRAPH_API_HOST, ConfigError, load_config  # noqa: E402

log = logging.getLogger("refresh_ig_token")

SECRET_NAME = "IG_ACCESS_TOKEN"
RENEW_WARNING_DAYS = 7


def exchange_token(config, session=None) -> tuple[str, int]:
    """Swap the current long-lived token for a fresh one.

    Returns (token, seconds_until_expiry).

    Instagram Login refreshes a long-lived token in place rather than exchanging
    one credential for another, so this needs only the token itself — no app id
    and no app secret. That is why the CI job carries no Meta app credentials.
    """
    import requests

    http = session or requests

    if not config.ig_access_token:
        raise ConfigError("IG_ACCESS_TOKEN is required to refresh the token.")

    response = http.get(
        f"{GRAPH_API_HOST}/refresh_access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": config.ig_access_token,
        },
        timeout=30,
    )
    payload = response.json()

    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        raise RuntimeError(
            f"Token refresh failed: {error.get('message')} (code={error.get('code')}). "
            "If the current token has already expired, reauthorise the account by hand "
            "in the App Dashboard under 'API setup with Instagram login'."
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Token exchange returned HTTP {response.status_code}: {payload!r}")

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Token exchange response had no access_token: {payload!r}")
    return token, int(payload.get("expires_in") or 0)


def write_secret(token: str, repo: str | None = None) -> None:
    """Store the token as the IG_ACCESS_TOKEN repository secret."""
    command = ["gh", "secret", "set", SECRET_NAME]
    if repo:
        command += ["--repo", repo]

    result = subprocess.run(command, input=token, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not write the {SECRET_NAME} secret: {result.stderr.strip()}. "
            "Check that GH_TOKEN is a repo-scoped PAT with secrets write access."
        )
    log.info("Updated the %s repository secret", SECRET_NAME)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exchange the token but do not write the secret.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name of the repo whose secret to update.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()

    token, expires_in = exchange_token(config)
    days = expires_in // 86400
    # Never log the token itself — CI logs are not a secret store.
    log.info("Refreshed token successfully; the new one expires in ~%d days", days)

    if days and days < RENEW_WARNING_DAYS:
        log.warning(
            "New token expires in only %d days. Meta refuses to extend a token that is "
            "less than 24 hours old — check the refresh schedule is running.",
            days,
        )

    if args.dry_run:
        log.info("--dry-run: not writing the secret")
        return 0

    write_secret(token, args.repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
