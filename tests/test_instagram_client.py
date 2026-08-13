"""Graph API client. Every HTTP call is mocked — nothing here touches Meta."""

import types

import pytest

from src.config import load_config
from src.instagram_client import InstagramClient, InstagramError


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else str(payload)

    def json(self):
        if self._payload is _INVALID_JSON:
            raise ValueError("not json")
        return self._payload


_INVALID_JSON = object()


class FakeSession:
    """Returns queued responses in order and records every request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def _next(self, method, url, params=None, timeout=None):
        self.requests.append({"method": method, "url": url, "params": params})
        if not self._responses:
            raise AssertionError(f"unexpected extra {method} to {url}")
        return self._responses.pop(0)

    def post(self, url, params=None, timeout=None):
        return self._next("post", url, params, timeout)

    def get(self, url, params=None, timeout=None):
        return self._next("get", url, params, timeout)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "17841400000000000")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("IMAGE_BASE_URL", "https://example.com/images")
    return load_config()


def client(config, responses):
    return InstagramClient(config=config, session=FakeSession(responses))


def ok_publish_flow():
    return [
        FakeResponse({"id": "container-1"}),
        FakeResponse({"status_code": "FINISHED"}),
        FakeResponse({"id": "media-1"}),
    ]


# --- happy path --------------------------------------------------------


def test_publish_runs_container_wait_and_publish(config):
    ig = client(config, ok_publish_flow())
    result = ig.publish("https://example.com/a.jpg", "caption", "alt text", poll_delay=0)

    assert result.media_id == "media-1"
    assert result.container_id == "container-1"
    assert [r["method"] for r in ig._session.requests] == ["post", "get", "post"]


def test_container_request_carries_image_caption_and_alt_text(config):
    ig = client(config, ok_publish_flow())
    ig.publish("https://example.com/a.jpg", "my caption", "my alt", poll_delay=0)

    params = ig._session.requests[0]["params"]
    assert params["image_url"] == "https://example.com/a.jpg"
    assert params["caption"] == "my caption"
    assert params["alt_text"] == "my alt"
    assert params["access_token"] == "test-token"


def test_alt_text_is_omitted_when_absent(config):
    ig = client(config, ok_publish_flow())
    ig.publish("https://example.com/a.jpg", "caption", None, poll_delay=0)
    assert "alt_text" not in ig._session.requests[0]["params"]


def test_alt_text_is_truncated_to_the_api_limit(config):
    ig = client(config, ok_publish_flow())
    ig.publish("https://example.com/a.jpg", "caption", "x" * 2000, poll_delay=0)
    assert len(ig._session.requests[0]["params"]["alt_text"]) == 1000


def test_publish_uses_the_container_id_as_creation_id(config):
    ig = client(config, ok_publish_flow())
    ig.publish("https://example.com/a.jpg", "caption", poll_delay=0)
    assert ig._session.requests[2]["params"]["creation_id"] == "container-1"


# --- error surfacing ---------------------------------------------------


def test_error_object_in_a_200_body_still_raises(config):
    """Graph API reports some failures inside an HTTP 200 — status alone is
    not a success signal."""
    ig = client(
        config,
        [FakeResponse({"error": {"message": "Invalid image", "code": 9004, "type": "OAuthException"}})],
    )
    with pytest.raises(InstagramError) as exc:
        ig.create_container("https://example.com/a.jpg", "caption")
    assert "Invalid image" in str(exc.value)
    assert exc.value.code == 9004


def test_http_error_without_error_object_raises(config):
    ig = client(config, [FakeResponse({"something": "else"}, status_code=500)])
    with pytest.raises(InstagramError, match="HTTP 500"):
        ig.create_container("https://example.com/a.jpg", "caption")


def test_non_json_response_raises_readably(config):
    ig = client(config, [FakeResponse(_INVALID_JSON, status_code=502, text="<html>bad gateway</html>")])
    with pytest.raises(InstagramError, match="non-JSON"):
        ig.create_container("https://example.com/a.jpg", "caption")


def test_expired_token_is_flagged_as_an_auth_error(config):
    ig = client(
        config,
        [FakeResponse({"error": {"message": "Session has expired", "code": 190}})],
    )
    with pytest.raises(InstagramError) as exc:
        ig.create_container("https://example.com/a.jpg", "caption")
    assert exc.value.is_auth_error is True


def test_ordinary_error_is_not_an_auth_error(config):
    ig = client(config, [FakeResponse({"error": {"message": "Bad image", "code": 9004}})])
    with pytest.raises(InstagramError) as exc:
        ig.create_container("https://example.com/a.jpg", "caption")
    assert exc.value.is_auth_error is False


def test_missing_container_id_raises(config):
    ig = client(config, [FakeResponse({})])
    with pytest.raises(InstagramError, match="no id"):
        ig.create_container("https://example.com/a.jpg", "caption")


def test_missing_media_id_raises(config):
    ig = client(config, [FakeResponse({})])
    with pytest.raises(InstagramError, match="no id"):
        ig.publish_container("container-1")


# --- container polling -------------------------------------------------


def test_polling_waits_for_finished(config):
    ig = client(
        config,
        [
            FakeResponse({"id": "container-1"}),
            FakeResponse({"status_code": "IN_PROGRESS"}),
            FakeResponse({"status_code": "IN_PROGRESS"}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "media-1"}),
        ],
    )
    assert ig.publish("https://example.com/a.jpg", "caption", poll_delay=0).media_id == "media-1"


def test_container_error_state_raises_without_publishing(config):
    ig = client(
        config,
        [
            FakeResponse({"id": "container-1"}),
            FakeResponse({"status_code": "ERROR", "status": "Media download failed"}),
        ],
    )
    with pytest.raises(InstagramError, match="ERROR"):
        ig.publish("https://example.com/a.jpg", "caption", poll_delay=0)
    # No publish attempt was made.
    assert [r["method"] for r in ig._session.requests] == ["post", "get"]


def test_container_expired_state_raises(config):
    ig = client(config, [FakeResponse({"status_code": "EXPIRED"})])
    with pytest.raises(InstagramError, match="EXPIRED"):
        ig.wait_for_container("container-1", delay=0)


def test_container_that_never_finishes_times_out(config):
    ig = client(config, [FakeResponse({"status_code": "IN_PROGRESS"}) for _ in range(3)])
    with pytest.raises(InstagramError, match="still not FINISHED"):
        ig.wait_for_container("container-1", attempts=3, delay=0)


# --- construction ------------------------------------------------------


def test_missing_credentials_are_rejected_at_construction(monkeypatch):
    monkeypatch.delenv("IG_USER_ID", raising=False)
    monkeypatch.delenv("IG_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("IMAGE_BASE_URL", raising=False)
    from src.config import ConfigError

    with pytest.raises(ConfigError):
        InstagramClient(config=load_config(), session=types.SimpleNamespace())
