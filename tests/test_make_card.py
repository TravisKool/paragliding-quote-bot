"""Quote-card rendering.

These assert structure rather than pixels: the card has to be square, contain
the whole quote in its alt text, scale type down instead of overflowing, and
never collide with a different quote's file. What it *looks* like is a taste
question and is checked by eye with `python -m src.make_card --preview`.
"""

import pytest
from PIL import Image, ImageDraw

from src.config import ALT_TEXT_MAX_CHARS, CARD_SIZE
from src.make_card import (
    QUOTE_FONT_CANDIDATES,
    QUOTE_SIZE_LADDER,
    THEME_GRADIENTS,
    CardRenderError,
    _load_font,
    attribution_for,
    render_card,
    wrap_measured,
)

QUOTE = "Fear is information. It tells you where the edge of your judgement is."


@pytest.fixture
def out(tmp_path):
    return tmp_path / "generated"


# --- output shape ------------------------------------------------------


def test_card_is_a_square_png_at_the_configured_size(out):
    card = render_card(QUOTE, theme="fear", output_dir=out)
    with Image.open(card.path) as image:
        assert image.size == (CARD_SIZE, CARD_SIZE)
        assert image.format == "PNG"


def test_output_directory_is_created(tmp_path):
    nested = tmp_path / "does" / "not" / "exist"
    card = render_card(QUOTE, output_dir=nested)
    assert card.path.parent == nested


def test_rendering_at_a_smaller_size_scales_the_whole_card(out):
    card = render_card(QUOTE, output_dir=out, size=540)
    with Image.open(card.path) as image:
        assert image.size == (540, 540)


# --- alt text ----------------------------------------------------------


def test_alt_text_is_the_quote_itself(out):
    """The image *is* the text, so describing it any other way loses content
    for a screen reader — and spares generate_post a vision call."""
    card = render_card(QUOTE, output_dir=out)
    assert card.alt_text == QUOTE


def test_attribution_is_appended_to_alt_text(out):
    card = render_card(QUOTE, attribution="Ada Thermalwright, Reading The Air", output_dir=out)
    assert card.alt_text.endswith("— Ada Thermalwright, Reading The Air")


def test_alt_text_respects_the_instagram_limit(out):
    card = render_card("word " * 400, attribution="Someone", output_dir=out)
    assert len(card.alt_text) <= ALT_TEXT_MAX_CHARS


# --- filenames ---------------------------------------------------------


def test_the_same_quote_always_lands_on_the_same_file(out):
    """A retried run should overwrite its card, not litter the repo."""
    first = render_card(QUOTE, output_dir=out)
    second = render_card(QUOTE, output_dir=out)
    assert first.path == second.path
    assert len(list(out.iterdir())) == 1


def test_different_quotes_get_different_files(out):
    a = render_card(QUOTE, output_dir=out)
    b = render_card("The sky does not negotiate.", output_dir=out)
    assert a.path != b.path


def test_whitespace_differences_do_not_create_a_second_file(out):
    a = render_card(QUOTE, output_dir=out)
    b = render_card(f"  {QUOTE}\n ".replace(". ", ".  "), output_dir=out)
    assert a.path == b.path


# --- text fitting ------------------------------------------------------


def test_wrapping_uses_the_full_width(out):
    """Regression: an estimated character width wrapped at half the card."""
    image = Image.new("RGB", (CARD_SIZE, CARD_SIZE))
    draw = ImageDraw.Draw(image)
    font = _load_font(QUOTE_FONT_CANDIDATES, 76)
    max_width = 860

    lines = wrap_measured(QUOTE, draw, font, max_width)
    widest = max(draw.textlength(line, font=font) for line in lines)
    assert widest <= max_width
    # Every line but the last should be near the limit, or the wrap is timid.
    assert widest > max_width * 0.75


def test_wrapping_never_exceeds_the_box(out):
    image = Image.new("RGB", (CARD_SIZE, CARD_SIZE))
    draw = ImageDraw.Draw(image)
    font = _load_font(QUOTE_FONT_CANDIDATES, 76)
    for text in (QUOTE, "word " * 200, "Short.", "Supercalifragilisticexpialidocious"):
        lines = wrap_measured(text, draw, font, 860)
        # A single unbreakable word may overflow; anything with a space cannot.
        multiword = [line for line in lines if " " in line]
        assert all(draw.textlength(line, font=font) <= 860 for line in multiword)


def test_a_long_quote_renders_rather_than_raising(out):
    long_quote = (
        "The pilot who thumbs a lift home after landing out has learned more about air "
        "than the pilot who stayed in the bar, and rather more than the pilot who pushed "
        "on into a valley wind they did not understand and got away with it."
    )
    card = render_card(long_quote, theme="risk", output_dir=out)
    assert card.path.exists()


def test_the_size_ladder_descends(out):
    assert list(QUOTE_SIZE_LADDER) == sorted(QUOTE_SIZE_LADDER, reverse=True)


def test_font_resolution_survives_a_machine_with_no_matching_fonts():
    """The daily job runs on a bare Ubuntu runner. If every named font misses,
    the bundled DejaVu has to catch it — otherwise the post fails at 2am."""
    font = _load_font(("NoSuchFont-Zzz.ttf", "AlsoMissing.ttf"), 48)
    image = Image.new("RGB", (100, 100))
    # A bitmap fallback would measure far too small to be readable.
    assert ImageDraw.Draw(image).textlength("MMMM", font=font) > 40


def test_an_empty_quote_is_refused(out):
    with pytest.raises(CardRenderError, match="empty"):
        render_card("   ", output_dir=out)


# --- theming -----------------------------------------------------------


def test_each_theme_gets_its_own_background(out):
    """Two different themes must not produce byte-identical cards, or the feed
    reads as one repeated image."""
    a = render_card(QUOTE, theme="fear", output_dir=out / "a")
    b = render_card(QUOTE, theme="weather", output_dir=out / "b")
    assert a.path.read_bytes() != b.path.read_bytes()


def test_an_unknown_theme_falls_back_to_the_default_gradient(out):
    card = render_card(QUOTE, theme="not-a-real-theme", output_dir=out)
    assert card.path.exists()


def test_no_theme_at_all_is_fine(out):
    assert render_card(QUOTE, theme=None, output_dir=out).path.exists()


def test_theme_matching_is_case_insensitive(out):
    upper = render_card(QUOTE, theme="FEAR", output_dir=out / "upper")
    lower = render_card(QUOTE, theme="fear", output_dir=out / "lower")
    assert upper.path.read_bytes() == lower.path.read_bytes()


def test_every_gradient_is_dark_enough_for_white_text():
    """White text over a light background is the one way this can be unusable,
    and it would not be caught until the post was live."""
    for theme, (top, bottom) in THEME_GRADIENTS.items():
        for colour in (top, bottom):
            luminance = (0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]) / 255
            assert luminance < 0.4, f"{theme} is too light for white text"


# --- attribution -------------------------------------------------------


def test_attribution_combines_author_and_title():
    assert attribution_for({"author": "Ada Thermalwright", "book_title": "Reading The Air"}) == (
        "Ada Thermalwright, Reading The Air"
    )


def test_attribution_falls_back_to_whichever_is_present():
    assert attribution_for({"author": "Ada Thermalwright", "book_title": None}) == "Ada Thermalwright"
    assert attribution_for({"author": None, "book_title": "Reading The Air"}) == (
        "Reading The Air"
    )


def test_attribution_is_none_when_there_is_nothing_to_credit():
    assert attribution_for({"author": "", "book_title": None}) is None
