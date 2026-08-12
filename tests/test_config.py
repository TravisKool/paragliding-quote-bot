"""Config loading — the one piece of Phase 0 with real behaviour to pin down."""

import pytest

from src.config import ConfigError, load_config

ALL_VARS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "LOCAL_DB_PATH",
    "IG_USER_ID",
    "IG_ACCESS_TOKEN",
    "META_APP_ID",
    "META_APP_SECRET",
    "IMAGE_BASE_URL",
    "DRY_RUN",
    "LOG_LEVEL",
]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_loads_with_empty_environment(clean_env):
    """Loading must never raise — commands validate only what they need."""
    cfg = load_config()
    assert cfg.anthropic_model == "claude-opus-5"
    assert cfg.local_db_path == "local.db"
    assert cfg.dry_run is False
    assert cfg.use_turso is False


def test_dry_run_flag_accepts_common_spellings(clean_env, monkeypatch):
    for raw in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("DRY_RUN", raw)
        assert load_config().dry_run is True
    for raw in ("0", "false", "no", ""):
        monkeypatch.setenv("DRY_RUN", raw)
        assert load_config().dry_run is False


def test_image_base_url_trailing_slash_stripped(clean_env, monkeypatch):
    monkeypatch.setenv("IMAGE_BASE_URL", "https://example.com/images/")
    assert load_config().image_base_url == "https://example.com/images"


def test_use_turso_true_when_url_present(clean_env, monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://db.turso.io")
    assert load_config().use_turso is True


def test_require_instagram_names_the_missing_variable(clean_env):
    with pytest.raises(ConfigError, match="IG_USER_ID"):
        load_config().require_instagram()


def test_require_anthropic_passes_when_key_set(clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    load_config().require_anthropic()  # must not raise
