"""Caption assembly, hashtag cleaning, and the Claude call (stubbed).

No test here reaches the real API.
"""

import json
import types

import pytest

from src.config import CAPTION_MAX_CHARS, load_config
from src.generate_post import (
    CaptionRefused,
    GenerationError,
    assemble_caption,
    build_post,
    generate_caption_parts,
    normalize_hashtags,
)

QUOTE = {
    "id": 1,
    "quote_text": "The air rewards patience more than it rewards courage.",
    "author": "A. Author",
    "book_title": "Masterclass",
    "theme": "fear",
}


# --- fake client -------------------------------------------------------


def fake_response(text, stop_reason="end_turn", stop_details=None):
    block = types.SimpleNamespace(type="text", text=text)
    return types.SimpleNamespace(
        content=[block], stop_reason=stop_reason, stop_details=stop_details
    )


class FakeClient:
    """Stands in for anthropic.Anthropic, recording the request."""

    def __init__(self, response):
        self._response = response
        self.calls = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def caption_client(context="Context paragraph.", hashtags=("paragliding", "xc")):
    return FakeClient(
        fake_response(json.dumps({"context": context, "hashtags": list(hashtags)}))
    )


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return load_config()


# --- hashtag normalization ---------------------------------------------


def test_hashtags_get_a_single_leading_hash():
    assert normalize_hashtags(["paragliding", "#xc"]) == ["#paragliding", "#xc"]


def test_hashtag_punctuation_and_spaces_are_stripped():
    assert normalize_hashtags(["cross country!", "free-flight"]) == [
        "#crosscountry",
        "#freeflight",
    ]


def test_duplicate_hashtags_are_dropped_case_insensitively():
    assert normalize_hashtags(["XC", "xc", "#Xc"]) == ["#XC"]


def test_overlong_and_empty_hashtags_are_dropped():
    assert normalize_hashtags(["#", "   ", "a" * 31, "ok"]) == ["#ok"]


def test_hashtag_count_is_capped_at_instagram_limit():
    assert len(normalize_hashtags([f"tag{i}" for i in range(50)])) == 30


# --- caption assembly --------------------------------------------------


def test_caption_layout():
    caption = assemble_caption(QUOTE, "Context here.", ["#a", "#b"])
    assert caption == (
        "“The air rewards patience more than it rewards courage.”\n"
        "— A. Author, Masterclass\n\n"
        "Context here.\n\n"
        "#a #b"
    )


def test_attribution_omitted_when_unknown():
    caption = assemble_caption({"quote_text": "Bare."}, "Ctx.", [])
    assert caption == "“Bare.”\n\nCtx."


def test_attribution_handles_author_without_book():
    caption = assemble_caption({"quote_text": "X."}, "Ctx.", [])
    assert "—" not in caption
    caption = assemble_caption({"quote_text": "X.", "author": "A."}, "Ctx.", [])
    assert "— A." in caption


def test_hashtags_are_dropped_first_when_over_length():
    context = "c" * (CAPTION_MAX_CHARS - 150)
    caption = assemble_caption(QUOTE, context, [f"#tag{i}" for i in range(20)])
    assert len(caption) <= CAPTION_MAX_CHARS
    assert context[:50] in caption  # context survived intact


def test_context_is_truncated_only_after_hashtags_are_gone():
    caption = assemble_caption(QUOTE, "c" * (CAPTION_MAX_CHARS + 500), ["#a", "#b"])
    assert len(caption) <= CAPTION_MAX_CHARS
    assert "#a" not in caption
    assert caption.endswith("…")


def test_quote_is_never_truncated():
    quote = {"quote_text": "q" * (CAPTION_MAX_CHARS - 10), "author": "A"}
    caption = assemble_caption(quote, "context", ["#a"])
    assert "q" * (CAPTION_MAX_CHARS - 10) in caption


def test_quote_longer_than_the_limit_is_an_error():
    with pytest.raises(GenerationError, match="too long"):
        assemble_caption({"quote_text": "q" * (CAPTION_MAX_CHARS + 1)}, "", [])


def test_empty_quote_is_an_error():
    with pytest.raises(GenerationError, match="no text"):
        assemble_caption({"quote_text": "   "}, "Ctx.", [])


# --- the Claude call ---------------------------------------------------


def test_generate_caption_parts_parses_the_response(config):
    client = caption_client("A real paragraph.", ["paragliding", "xccontest"])
    context, hashtags = generate_caption_parts(QUOTE, config=config, client=client)
    assert context == "A real paragraph."
    assert hashtags == ["#paragliding", "#xccontest"]


def test_request_uses_the_configured_model_and_structured_output(config):
    client = caption_client()
    generate_caption_parts(QUOTE, config=config, client=client)
    request = client.calls[0]
    assert request["model"] == "claude-opus-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_quote_metadata_is_sent_to_the_model(config):
    client = caption_client()
    generate_caption_parts(QUOTE, config=config, client=client)
    sent = client.calls[0]["messages"][0]["content"]
    assert QUOTE["quote_text"] in sent
    assert "fear" in sent


def test_refusal_raises_caption_refused(config):
    client = FakeClient(
        types.SimpleNamespace(
            content=[],
            stop_reason="refusal",
            stop_details=types.SimpleNamespace(category="cyber"),
        )
    )
    with pytest.raises(CaptionRefused, match="cyber"):
        generate_caption_parts(QUOTE, config=config, client=client)


def test_non_json_response_is_an_error(config):
    client = FakeClient(fake_response("Sorry, here's a caption instead!"))
    with pytest.raises(GenerationError, match="not valid JSON"):
        generate_caption_parts(QUOTE, config=config, client=client)


def test_empty_context_is_an_error(config):
    client = caption_client(context="   ")
    with pytest.raises(GenerationError, match="empty context"):
        generate_caption_parts(QUOTE, config=config, client=client)


def test_missing_hashtags_warn_but_do_not_fail(config, caplog):
    client = caption_client(hashtags=[])
    with caplog.at_level("WARNING"):
        context, hashtags = generate_caption_parts(QUOTE, config=config, client=client)
    assert hashtags == []
    assert "hashtags" in caplog.text


# --- build_post --------------------------------------------------------


def test_build_post_prefers_manifest_alt_text(config):
    client = caption_client()
    post = build_post(QUOTE, manifest_alt_text="Human-written alt.", config=config, client=client)
    assert post.alt_text == "Human-written alt."
    assert len(client.calls) == 1  # no vision call needed


def test_build_post_without_image_or_alt_text_posts_without_alt(config):
    client = caption_client()
    post = build_post(QUOTE, config=config, client=client)
    assert post.alt_text is None
    assert post.caption.startswith("“The air rewards")


def test_build_post_generates_alt_text_from_the_image(config, tmp_path):
    image = tmp_path / "shot.jpg"
    image.write_bytes(b"fake-jpeg-bytes")

    class TwoCallClient(FakeClient):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return fake_response(json.dumps({"context": "Ctx.", "hashtags": ["xc"]}))
            return fake_response("A glider banking against a ridge.")

    client = TwoCallClient(None)
    post = build_post(QUOTE, image_path=image, config=config, client=client)
    assert post.alt_text == "A glider banking against a ridge."
    assert client.calls[1]["messages"][0]["content"][0]["type"] == "image"


def test_alt_text_failure_does_not_fail_the_post(config, tmp_path, caplog):
    image = tmp_path / "shot.jpg"
    image.write_bytes(b"bytes")

    class FlakyClient(FakeClient):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return fake_response(json.dumps({"context": "Ctx.", "hashtags": ["xc"]}))
            raise RuntimeError("vision call exploded")

    with caplog.at_level("WARNING"):
        post = build_post(QUOTE, image_path=image, config=config, client=FlakyClient(None))
    assert post.alt_text is None
    assert post.caption  # the post is still publishable
    assert "alt-text generation failed" in caplog.text.lower()


def test_unsupported_image_type_skips_alt_text(config, tmp_path, caplog):
    image = tmp_path / "shot.gif"
    image.write_bytes(b"bytes")
    with caplog.at_level("WARNING"):
        post = build_post(QUOTE, image_path=image, config=config, client=caption_client())
    assert post.alt_text is None
