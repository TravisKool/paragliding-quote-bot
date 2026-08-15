"""Daily orchestration. Every external call is stubbed."""

import json
import types
from pathlib import Path

import pytest

from src import main as main_module
from src.config import load_config
from src.db import connect, insert_quotes, next_unused_quote
from src.generate_post import CaptionRefused
from src.instagram_client import InstagramError, PublishResult
from src.main import PostFailed, publish_one
from src.select_quote import QuotePoolEmpty, candidates


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A configured environment with a temp DB and a one-image library."""
    library = tmp_path / "images" / "library"
    library.mkdir(parents=True)
    (library / "a.jpg").write_bytes(b"fake-jpeg")
    (library / "manifest.json").write_text(
        json.dumps(
            {
                "images": [
                    {"filename": "a.jpg", "themes": ["fear"], "alt_text": "Manifest alt."}
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("IG_USER_ID", "ig-user")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "ig-token")
    monkeypatch.setenv("IMAGE_BASE_URL", "https://example.com/images")

    # Point image selection at the temp library, and neutralize git/network.
    monkeypatch.setattr(
        main_module,
        "pick_image",
        lambda theme: main_module.ImageChoice(
            path=library / "a.jpg",
            relative_path="images/library/a.jpg",
            source="theme-match",
            alt_text="Manifest alt.",
        ),
    )
    monkeypatch.setattr(
        main_module,
        "ensure_public_url",
        lambda path, rel, **kw: f"https://example.com/{rel}",
    )
    return types.SimpleNamespace(config=load_config(), library=library, tmp_path=tmp_path)


class FakeClaude:
    """Returns a valid caption payload for every call."""

    def __init__(self, error=None):
        self.error = error
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        if self.error:
            raise self.error
        return types.SimpleNamespace(
            stop_reason="end_turn",
            stop_details=None,
            content=[
                types.SimpleNamespace(
                    type="text",
                    text=json.dumps({"context": "Context.", "hashtags": ["paragliding"]}),
                )
            ],
        )


class FakeInstagram:
    def __init__(self, error=None):
        self.error = error
        self.published = []

    def publish(self, image_url, caption, alt_text=None):
        if self.error:
            raise self.error
        self.published.append((image_url, caption, alt_text))
        return PublishResult(media_id="media-99", container_id="container-1")


@pytest.fixture
def db(env):
    with connect(env.config) as database:
        database.init_schema()
        insert_quotes(
            database,
            [
                {"quote_text": "Best quote.", "quality_score": 0.9, "theme": "fear"},
                {"quote_text": "Second quote.", "quality_score": 0.5, "theme": "fear"},
            ],
        )
        yield database


# --- happy path --------------------------------------------------------


def test_publish_marks_the_quote_used_and_records_the_post(env, db):
    ig = FakeInstagram()
    outcome = publish_one(db, env.config, client=FakeClaude(), instagram=ig)

    assert outcome.media_id == "media-99"
    assert outcome.quote["quote_text"] == "Best quote."
    post = db.query_one("SELECT * FROM posts")
    assert post["status"] == "success"
    assert post["instagram_media_id"] == "media-99"
    assert db.query_one("SELECT used_at FROM quotes WHERE id = ?", [outcome.quote["id"]])[
        "used_at"
    ]


def test_manifest_alt_text_is_passed_to_instagram(env, db):
    ig = FakeInstagram()
    publish_one(db, env.config, client=FakeClaude(), instagram=ig)
    assert ig.published[0][2] == "Manifest alt."


def test_caption_contains_the_quote(env, db):
    ig = FakeInstagram()
    publish_one(db, env.config, client=FakeClaude(), instagram=ig)
    assert "Best quote." in ig.published[0][1]


def test_highest_scoring_quote_goes_first(env, db):
    publish_one(db, env.config, client=FakeClaude(), instagram=FakeInstagram())
    assert next_unused_quote(db)["quote_text"] == "Second quote."


# --- failure handling --------------------------------------------------


def test_publish_failure_does_not_burn_the_quote(env, db):
    """The whole point of the design: a failed post is retried tomorrow."""
    before = next_unused_quote(db)
    ig = FakeInstagram(error=InstagramError("Meta is down"))

    with pytest.raises(InstagramError):
        publish_one(db, env.config, client=FakeClaude(), instagram=ig)

    assert next_unused_quote(db)["id"] == before["id"]
    assert db.query_one("SELECT status FROM posts")["status"] == "failed"


def test_refused_quote_is_skipped_and_the_next_one_is_used(env, db):
    """A quote Claude won't caption must not wedge the schedule forever."""

    class RefuseFirst(FakeClaude):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def _create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise CaptionRefused("declined")
            return super()._create(**kwargs)

    ig = FakeInstagram()
    outcome = publish_one(db, env.config, client=RefuseFirst(), instagram=ig)
    assert outcome.quote["quote_text"] == "Second quote."


def test_all_candidates_refused_raises_post_failed(env, db):
    with pytest.raises(PostFailed, match="candidate quotes"):
        publish_one(
            db, env.config, client=FakeClaude(error=CaptionRefused("no")), instagram=FakeInstagram()
        )


def test_empty_pool_raises(env):
    with connect(env.config) as database:
        database.init_schema()
        with pytest.raises(QuotePoolEmpty, match="Re-seed"):
            publish_one(database, env.config, client=FakeClaude())


def test_low_pool_logs_a_warning(env, db, caplog):
    with caplog.at_level("WARNING"):
        candidates(db, limit=1)
    assert "Re-seed soon" in caplog.text


# --- dry run -----------------------------------------------------------


def test_dry_run_does_not_publish_or_record(env, db):
    ig = FakeInstagram()
    outcome = publish_one(db, env.config, dry_run=True, client=FakeClaude(), instagram=ig)

    assert outcome.dry_run is True
    assert outcome.media_id is None
    assert ig.published == []
    assert db.query("SELECT * FROM posts") == []
    assert next_unused_quote(db)["quote_text"] == "Best quote."


def test_dry_run_still_builds_a_full_caption(env, db):
    outcome = publish_one(db, env.config, dry_run=True, client=FakeClaude())
    assert "Best quote." in outcome.post.caption
    assert outcome.post.hashtags == ["#paragliding"]


# --- CLI ---------------------------------------------------------------


def test_main_returns_zero_on_success(env, db, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "publish_one",
        lambda *a, **k: main_module.PostOutcome(
            quote={"id": 1, "theme": "fear", "quality_score": 0.9},
            image=main_module.ImageChoice(
                path=Path("a.jpg"), relative_path="images/library/a.jpg", source="theme-match"
            ),
            post=types.SimpleNamespace(caption="c", alt_text=None, hashtags=[]),
            image_url="https://example.com/a.jpg",
            media_id="media-1",
            dry_run=False,
        ),
    )
    assert main_module.main([]) == 0


def test_main_returns_nonzero_on_failure(env, db, monkeypatch):
    """Non-zero exit is what makes GitHub send the failure email."""

    def explode(*args, **kwargs):
        raise PostFailed("nothing worked")

    monkeypatch.setattr(main_module, "publish_one", explode)
    assert main_module.main([]) == 1


def test_dry_run_flag_is_honoured(env, db, monkeypatch):
    seen = {}

    def capture(db_, config, **kwargs):
        seen.update(kwargs)
        raise PostFailed("stop here")

    monkeypatch.setattr(main_module, "publish_one", capture)
    main_module.main(["--dry-run"])
    assert seen["dry_run"] is True


# --- image mode --------------------------------------------------------
#
# With no photo library the run has to fall back to a generated quote card,
# because an empty library is the normal state of a new account rather than a
# fault. These drive choose_image directly: it is the only place that decides.


@pytest.fixture
def quote_row():
    return {
        "id": 1,
        "quote_text": "Fear is information, not instruction.",
        "theme": "fear",
        "author": "Ada Thermalwright",
        "book_title": "Reading The Air",
    }


def empty_library(monkeypatch):
    """Make pick_image behave as it does with nothing in images/library/."""
    from src.pick_image import NoImagesAvailable

    def raise_empty(theme):
        raise NoImagesAvailable("No usable images in images/library/.")

    monkeypatch.setattr(main_module, "pick_image", raise_empty)


def test_auto_mode_prefers_a_photo_when_the_library_has_one(env, quote_row):
    choice = main_module.choose_image(quote_row, env.config)
    assert choice.source == "theme-match"


def test_auto_mode_renders_a_card_when_the_library_is_empty(
    env, quote_row, monkeypatch, tmp_path
):
    empty_library(monkeypatch)
    monkeypatch.setattr(main_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.make_card.GENERATED_DIR", tmp_path / "images" / "generated")

    choice = main_module.choose_image(quote_row, env.config)
    assert choice.source == "generated-card"
    assert choice.path.exists()
    assert choice.relative_path.startswith("images/generated/")


def test_card_alt_text_carries_the_quote(env, quote_row, monkeypatch, tmp_path):
    """Set here rather than left to generate_post, which would otherwise spend
    a vision call reading the card's own text back off the image."""
    empty_library(monkeypatch)
    monkeypatch.setattr(main_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.make_card.GENERATED_DIR", tmp_path / "images" / "generated")

    choice = main_module.choose_image(quote_row, env.config)
    assert quote_row["quote_text"] in choice.alt_text
    assert "Ada Thermalwright, Reading The Air" in choice.alt_text


def test_card_mode_skips_the_library_entirely(env, quote_row, monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_MODE", "card")
    monkeypatch.setattr(main_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.make_card.GENERATED_DIR", tmp_path / "images" / "generated")

    choice = main_module.choose_image(quote_row, load_config())
    assert choice.source == "generated-card"


def test_photo_mode_fails_rather_than_posting_a_card(env, quote_row, monkeypatch):
    from src.pick_image import NoImagesAvailable

    monkeypatch.setenv("IMAGE_MODE", "photo")
    empty_library(monkeypatch)

    with pytest.raises(NoImagesAvailable):
        main_module.choose_image(quote_row, load_config())


def test_an_unrecognised_image_mode_degrades_to_auto(monkeypatch):
    monkeypatch.setenv("IMAGE_MODE", "carrd")
    assert load_config().image_mode == "auto"
