"""Meta Graph API wrapper for publishing a single image post.

Publishing is three steps, not two: create a media container, wait for Meta to
finish fetching the image URL, then publish the container. Skipping the wait is
the classic cause of intermittent failures — the publish call rejects a
container that is still IN_PROGRESS, and the error text doesn't say so.

Graph API failures are surfaced rather than swallowed. It can report an error
inside an HTTP 200 body, so the status code alone is not a success signal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import ALT_TEXT_MAX_CHARS, GRAPH_API_BASE, Config, load_config

log = logging.getLogger(__name__)

CONTAINER_POLL_ATTEMPTS = 12
CONTAINER_POLL_DELAY_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 30


class InstagramError(RuntimeError):
    """A Graph API call failed.

    Carries the parsed `error` object where Meta supplied one, since the
    `code`/`error_subcode` pair is what you actually need to diagnose these.
    """

    def __init__(self, message: str, *, error: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error = error or {}

    @property
    def code(self) -> int | None:
        value = self.error.get("code")
        return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None

    @property
    def is_auth_error(self) -> bool:
        """True for expired/invalid token errors — the ones a token refresh fixes."""
        return self.code in {102, 190}


@dataclass(frozen=True)
class PublishResult:
    media_id: str
    container_id: str


def _describe(error: dict[str, Any]) -> str:
    parts = [str(error.get("message", "unknown error"))]
    for key in ("type", "code", "error_subcode", "error_user_msg"):
        if error.get(key) is not None:
            parts.append(f"{key}={error[key]}")
    return " | ".join(parts)


class InstagramClient:
    """Thin wrapper over the two publishing endpoints."""

    def __init__(self, config: Config | None = None, session: Any = None) -> None:
        self.config = config or load_config()
        self.config.require_instagram()
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    # --- plumbing ------------------------------------------------------

    def _request(self, method: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "access_token": self.config.ig_access_token}
        response = getattr(self._session, method)(
            url, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )

        try:
            payload = response.json()
        except ValueError:
            raise InstagramError(
                f"Graph API returned non-JSON (HTTP {response.status_code}): "
                f"{response.text[:200]!r}"
            ) from None

        # An error object can arrive with any status code, including 200.
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            raise InstagramError(
                f"Graph API error on {method.upper()} {url}: {_describe(error)}", error=error
            )
        if response.status_code >= 400:
            raise InstagramError(
                f"Graph API returned HTTP {response.status_code} for {url}: {payload!r}"
            )
        return payload

    # --- steps ---------------------------------------------------------

    def create_container(
        self, image_url: str, caption: str, alt_text: str | None = None
    ) -> str:
        """Step 1: hand Meta the image URL and caption."""
        params: dict[str, Any] = {"image_url": image_url, "caption": caption}
        if alt_text:
            params["alt_text"] = alt_text[:ALT_TEXT_MAX_CHARS]

        payload = self._request(
            "post", f"{GRAPH_API_BASE}/{self.config.ig_user_id}/media", params
        )
        container_id = payload.get("id")
        if not container_id:
            raise InstagramError(f"Container response had no id: {payload!r}")
        log.info("Created media container %s", container_id)
        return str(container_id)

    def wait_for_container(
        self,
        container_id: str,
        *,
        attempts: int = CONTAINER_POLL_ATTEMPTS,
        delay: float = CONTAINER_POLL_DELAY_SECONDS,
    ) -> None:
        """Step 2: block until Meta has fetched the image.

        Publishing an unfinished container fails with a message that doesn't
        mention the container state, so this wait is what keeps failures legible.
        """
        for attempt in range(1, attempts + 1):
            payload = self._request(
                "get", f"{GRAPH_API_BASE}/{container_id}", {"fields": "status_code,status"}
            )
            status = payload.get("status_code")

            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                raise InstagramError(
                    f"Container {container_id} ended in state {status}: "
                    f"{payload.get('status', 'no detail')}"
                )

            log.info(
                "Container %s is %s, attempt %d/%d", container_id, status, attempt, attempts
            )
            if attempt < attempts:
                time.sleep(delay)

        raise InstagramError(
            f"Container {container_id} was still not FINISHED after {attempts} checks. "
            "Meta could not fetch the image URL in time."
        )

    def publish_container(self, container_id: str) -> str:
        """Step 3: publish the finished container."""
        payload = self._request(
            "post",
            f"{GRAPH_API_BASE}/{self.config.ig_user_id}/media_publish",
            {"creation_id": container_id},
        )
        media_id = payload.get("id")
        if not media_id:
            raise InstagramError(f"Publish response had no id: {payload!r}")
        log.info("Published media %s", media_id)
        return str(media_id)

    def publish(
        self,
        image_url: str,
        caption: str,
        alt_text: str | None = None,
        *,
        poll_delay: float = CONTAINER_POLL_DELAY_SECONDS,
    ) -> PublishResult:
        """Run all three steps and return the published media id."""
        container_id = self.create_container(image_url, caption, alt_text)
        self.wait_for_container(container_id, delay=poll_delay)
        media_id = self.publish_container(container_id)
        return PublishResult(media_id=media_id, container_id=container_id)
