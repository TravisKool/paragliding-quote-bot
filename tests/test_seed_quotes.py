"""Quote extraction: batching, deduplication, and response parsing.

PDF reading itself is not tested — that needs the real book, which is not in
the repo. Everything downstream of the page text is covered.
"""

import json
import types

import pytest

from src.config import load_config
from src.seed_quotes import (
    ABSOLUTE_MAX_CHARS,
    Page,
    batch_pages,
    deduplicate,
    extract_from_batch,
)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return load_config()


class FakeClaude:
    def __init__(self, quotes):
        self.payload = {"quotes": quotes}
        self.calls = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            stop_reason="end_turn",
            stop_details=None,
            content=[types.SimpleNamespace(type="text", text=json.dumps(self.payload))],
        )


def pages(count=20):
    return [Page(number=i, text=f"Text of page {i}. " * 20) for i in range(1, count + 1)]


# --- batching ----------------------------------------------------------


def test_pages_are_batched_evenly():
    batches = batch_pages(pages(20), size=8)
    assert [len(b) for b in batches] == [8, 8, 4]


def test_batching_handles_fewer_pages_than_one_batch():
    assert len(batch_pages(pages(3), size=8)) == 1


def test_batching_empty_input():
    assert batch_pages([], size=8) == []


# --- deduplication -----------------------------------------------------


def test_identical_quotes_are_collapsed():
    quotes = [
        {"quote_text": "Trust the glider.", "quality_score": 0.5},
        {"quote_text": "Trust the glider.", "quality_score": 0.9},
    ]
    assert len(deduplicate(quotes)) == 1


def test_the_highest_scoring_version_survives():
    """Books repeat their best lines with small edits; keep the best-scored one."""
    quotes = [
        {"quote_text": "The air rewards patience, not courage.", "quality_score": 0.4},
        {"quote_text": "The air rewards patience and not courage.", "quality_score": 0.95},
    ]
    kept = deduplicate(quotes)
    assert len(kept) == 1
    assert kept[0]["quality_score"] == 0.95


def test_punctuation_and_case_differences_count_as_duplicates():
    quotes = [
        {"quote_text": "Trust the glider!", "quality_score": 0.5},
        {"quote_text": "trust the glider", "quality_score": 0.6},
    ]
    assert len(deduplicate(quotes)) == 1


def test_genuinely_different_quotes_are_kept():
    quotes = [
        {"quote_text": "Fear is information, not instruction.", "quality_score": 0.8},
        {"quote_text": "Every glide is a decision you already made on launch.", "quality_score": 0.7},
    ]
    assert len(deduplicate(quotes)) == 2


def test_empty_quotes_are_dropped():
    assert deduplicate([{"quote_text": "   ", "quality_score": 0.9}]) == []


def test_unscored_quotes_do_not_crash_sorting():
    quotes = [
        {"quote_text": "Scored.", "quality_score": 0.5},
        {"quote_text": "Unscored entirely different text.", "quality_score": None},
    ]
    assert len(deduplicate(quotes)) == 2


# --- response parsing --------------------------------------------------


def batch_call(config, quotes, **kwargs):
    client = FakeClaude(quotes)
    result = extract_from_batch(
        kwargs.get("batch") or pages(3),
        config=config,
        client=client,
        book_title=kwargs.get("book_title", "Masterclass"),
        author=kwargs.get("author", "A. Author"),
        front_matter=kwargs.get("front_matter", ""),
    )
    return client, result


def test_extraction_attaches_attribution(config):
    _, result = batch_call(
        config, [{"quote_text": "A quote.", "source_page": 4, "theme": "fear", "quality_score": 0.7}]
    )
    assert result[0]["book_title"] == "Masterclass"
    assert result[0]["author"] == "A. Author"
    assert result[0]["source_page"] == 4


def test_theme_is_lowercased(config):
    _, result = batch_call(
        config, [{"quote_text": "Q.", "source_page": 1, "theme": "  FEAR ", "quality_score": 0.7}]
    )
    assert result[0]["theme"] == "fear"


def test_scores_are_clamped_to_zero_one(config):
    _, result = batch_call(
        config,
        [
            {"quote_text": "High.", "source_page": 1, "theme": "fear", "quality_score": 1.7},
            {"quote_text": "Low.", "source_page": 2, "theme": "fear", "quality_score": -0.4},
        ],
    )
    assert [q["quality_score"] for q in result] == [1.0, 0.0]


def test_surrounding_quotation_marks_are_stripped(config):
    _, result = batch_call(
        config,
        [{"quote_text": "“Quoted.”", "source_page": 1, "theme": "fear", "quality_score": 0.7}],
    )
    assert result[0]["quote_text"] == "Quoted."


def test_overlong_quotes_are_rejected(config):
    """A quote longer than this could not fit in a caption at all."""
    _, result = batch_call(
        config,
        [
            {
                "quote_text": "x" * (ABSOLUTE_MAX_CHARS + 1),
                "source_page": 1,
                "theme": "fear",
                "quality_score": 0.9,
            }
        ],
    )
    assert result == []


def test_empty_batch_result_is_fine(config):
    """Most pages of most books contain nothing quotable."""
    _, result = batch_call(config, [])
    assert result == []


def test_page_numbers_are_sent_to_the_model(config):
    client, _ = batch_call(config, [])
    sent = client.calls[0]["messages"][0]["content"]
    assert "--- Page 1 ---" in sent
    assert "--- Page 3 ---" in sent


def test_request_uses_structured_output(config):
    client, _ = batch_call(config, [])
    request = client.calls[0]
    assert request["model"] == "claude-opus-5"
    assert request["output_config"]["format"]["type"] == "json_schema"


# --- chapter and context excerpt ----------------------------------------


def test_context_excerpt_is_the_source_pages_text(config):
    """Grounding material for the caption step: the full text of the page the
    quote came from, so generate_post can lean on it later."""
    _, result = batch_call(
        config, [{"quote_text": "A quote.", "source_page": 2, "theme": "fear", "quality_score": 0.7}]
    )
    assert result[0]["context_excerpt"] == pages(3)[1].text


def test_context_excerpt_is_none_for_an_unknown_page(config):
    """The model can misreport source_page; that shouldn't crash the batch."""
    _, result = batch_call(
        config, [{"quote_text": "A quote.", "source_page": 999, "theme": "fear", "quality_score": 0.7}]
    )
    assert result[0]["context_excerpt"] is None


def test_chapter_is_carried_through(config):
    _, result = batch_call(
        config,
        [
            {
                "quote_text": "A quote.",
                "source_page": 1,
                "chapter": "Valley flow",
                "theme": "fear",
                "quality_score": 0.7,
            }
        ],
    )
    assert result[0]["chapter"] == "Valley flow"


def test_missing_or_blank_chapter_is_none(config):
    _, result = batch_call(
        config, [{"quote_text": "A quote.", "source_page": 1, "chapter": "", "theme": "fear", "quality_score": 0.7}]
    )
    assert result[0]["chapter"] is None


def test_front_matter_is_prepended_when_given(config):
    client, _ = batch_call(config, [], front_matter="Valley flow\nRoute adaptation")
    sent = client.calls[0]["messages"][0]["content"]
    assert "Front matter" in sent
    assert "Valley flow" in sent


def test_no_front_matter_block_when_none_given(config):
    client, _ = batch_call(config, [])
    sent = client.calls[0]["messages"][0]["content"]
    assert "Front matter" not in sent
