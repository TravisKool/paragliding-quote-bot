"""Public URL construction and the reachability gate. No network, no git pushes."""

import pytest

from src.config import ConfigError, load_config
from src.publish_image_host import (
    ImageHostError,
    ensure_public_url,
    public_url,
    wait_until_reachable,
)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("IMAGE_BASE_URL", "https://raw.githubusercontent.com/o/r/main/images")
    return load_config()


# --- URL building ------------------------------------------------------


def test_url_drops_the_images_prefix(config):
    """IMAGE_BASE_URL already points at images/, so keeping the prefix would
    produce .../images/images/library/a.jpg."""
    assert public_url("images/library/a.jpg", config) == (
        "https://raw.githubusercontent.com/o/r/main/images/library/a.jpg"
    )


def test_url_handles_a_path_without_the_prefix(config):
    assert public_url("library/a.jpg", config).endswith("/images/library/a.jpg")


def test_windows_separators_are_normalized(config):
    assert public_url(r"images\library\a.jpg", config).endswith("/images/library/a.jpg")


def test_filename_with_spaces_is_percent_encoded(config):
    assert public_url("images/library/bir billing.jpg", config).endswith(
        "/library/bir%20billing.jpg"
    )


def test_generated_images_resolve_too(config):
    assert public_url("images/generated/ai-01.png", config).endswith("/generated/ai-01.png")


def test_missing_base_url_is_a_config_error(monkeypatch):
    monkeypatch.delenv("IMAGE_BASE_URL", raising=False)
    with pytest.raises(ConfigError, match="IMAGE_BASE_URL"):
        public_url("images/library/a.jpg", load_config())


# --- reachability ------------------------------------------------------


class FakeHttp:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def head(self, url, timeout=None, allow_redirects=None):
        self.calls += 1
        status = self.statuses.pop(0)
        if isinstance(status, Exception):
            raise status

        class R:
            status_code = status

        return R()


def test_reachable_on_first_try():
    http = FakeHttp([200])
    assert wait_until_reachable("https://example.com/a.jpg", session=http, delay=0) is True
    assert http.calls == 1


def test_retries_while_the_cdn_catches_up():
    """raw.githubusercontent.com lags a few seconds behind a push."""
    http = FakeHttp([404, 404, 200])
    assert wait_until_reachable("https://example.com/a.jpg", session=http, delay=0) is True
    assert http.calls == 3


def test_network_errors_are_retried():
    http = FakeHttp([ConnectionError("boom"), 200])
    assert wait_until_reachable("https://example.com/a.jpg", session=http, delay=0) is True


def test_gives_up_after_the_attempt_budget():
    http = FakeHttp([404] * 3)
    assert (
        wait_until_reachable("https://example.com/a.jpg", attempts=3, delay=0, session=http)
        is False
    )


# --- ensure_public_url -------------------------------------------------


def test_missing_file_is_an_error(config, tmp_path):
    with pytest.raises(ImageHostError, match="does not exist"):
        ensure_public_url(tmp_path / "nope.jpg", "images/library/nope.jpg", config=config)


def test_committed_image_skips_the_commit_path(config, tmp_path, monkeypatch):
    image = tmp_path / "a.jpg"
    image.write_bytes(b"bytes")

    import src.publish_image_host as module

    monkeypatch.setattr(module, "is_committed", lambda *a, **k: True)
    monkeypatch.setattr(
        module, "commit_and_push", lambda *a, **k: pytest.fail("should not commit")
    )

    url = ensure_public_url(image, "images/library/a.jpg", config=config, verify=False)
    assert url.endswith("/images/library/a.jpg")


def test_uncommitted_image_is_committed_before_use(config, tmp_path, monkeypatch):
    image = tmp_path / "gen.png"
    image.write_bytes(b"bytes")
    committed = []

    import src.publish_image_host as module

    monkeypatch.setattr(module, "is_committed", lambda *a, **k: False)
    monkeypatch.setattr(module, "commit_and_push", lambda p, r: committed.append(p))

    ensure_public_url(image, "images/generated/gen.png", config=config, verify=False)
    assert committed == [image]


def test_unreachable_url_fails_loudly(config, tmp_path, monkeypatch):
    """Better to fail here than hand Meta a 404 and get back an opaque error."""
    image = tmp_path / "a.jpg"
    image.write_bytes(b"bytes")

    import src.publish_image_host as module

    monkeypatch.setattr(module, "is_committed", lambda *a, **k: True)
    monkeypatch.setattr(module, "wait_until_reachable", lambda *a, **k: False)

    with pytest.raises(ImageHostError, match="did not become reachable"):
        ensure_public_url(image, "images/library/a.jpg", config=config, verify=True)
