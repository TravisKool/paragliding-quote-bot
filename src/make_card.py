"""Typeset a quote onto a generated image.

This is what the bot posts when there is no photo library. It is not a
placeholder: text-on-background is the dominant format for quote accounts, and
it has the advantage that the quote is readable in the feed rather than only in
the caption.

    python -m src.make_card --preview "Some quote text" --theme fear

Cards are rendered per post, not pre-generated. A card carries the quote in the
image, so reusing yesterday's card with today's caption would publish a post
whose picture and text disagree — the single worst failure this bot could have.
Everything here is therefore derived from the quote itself, including the
filename.

Backgrounds are a vertical gradient tinted by the quote's theme, so the feed
reads as one account rather than a random assortment, while still varying.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    ALT_TEXT_MAX_CHARS,
    CARD_MARGIN,
    CARD_SIZE,
    FONTS_DIR,
    GENERATED_DIR,
)

log = logging.getLogger(__name__)


class CardRenderError(RuntimeError):
    """The card could not be rendered."""


# Theme -> (top colour, bottom colour). Deep, desaturated skies and dusk
# tones: white text has to stay legible on every one of these, so none of them
# get lighter than roughly 40% luminance.
THEME_GRADIENTS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "fear": ((26, 32, 48), (12, 14, 24)),
    "commitment": ((32, 46, 74), (14, 20, 36)),
    "risk": ((62, 36, 38), (24, 16, 22)),
    "technique": ((28, 52, 60), (12, 22, 30)),
    "judgement": ((40, 44, 66), (16, 18, 30)),
    "patience": ((36, 54, 52), (14, 24, 26)),
    "weather": ((44, 58, 78), (16, 24, 36)),
    "learning": ((48, 44, 70), (18, 18, 32)),
    "mindset": ((54, 42, 62), (20, 16, 28)),
    "safety": ((30, 50, 46), (12, 22, 22)),
}

DEFAULT_GRADIENT = ((34, 44, 62), (14, 18, 28))

# Font search order. A file dropped into assets/fonts/ wins, because it is the
# only way the Ubuntu runner and a local Windows run produce the same image.
# Pillow bundles DejaVuSans, so the last entry always resolves.
QUOTE_FONT_CANDIDATES = (
    "Georgia.ttf",
    "georgia.ttf",
    "DejaVuSerif.ttf",
    "Times New Roman.ttf",
    "times.ttf",
    "LiberationSerif-Regular.ttf",
)
ATTRIBUTION_FONT_CANDIDATES = (
    "Arial.ttf",
    "arial.ttf",
    "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
    "calibri.ttf",
)

# Point sizes tried largest-first until the text fits the text box. A long
# quote gets smaller type rather than an overflowing or clipped card.
QUOTE_SIZE_LADDER = (76, 68, 62, 56, 50, 44, 40, 36, 32)
ATTRIBUTION_SIZE = 30
LINE_SPACING = 1.34


@dataclass(frozen=True)
class Card:
    """A rendered card and the text that went onto it."""

    path: Path
    alt_text: str
    theme: str | None


def _load_font(candidates: tuple[str, ...], size: int) -> Any:
    """First available font from `candidates`, at `size`.

    Looks in assets/fonts/ first, then lets Pillow resolve the name against the
    system font path, then falls back to the copy of DejaVuSans that ships
    inside Pillow itself.
    """
    from PIL import ImageFont

    for name in candidates:
        local = FONTS_DIR / name
        if local.exists():
            try:
                return ImageFont.truetype(str(local), size)
            except OSError:
                log.warning("Font %s is present but unreadable — skipping", local)

    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue

    # Pillow ships a copy of DejaVuSans. Loading it by path rather than by name
    # is what makes this fallback reliable: `truetype("DejaVuSans.ttf")` only
    # searches system font directories, and the Ubuntu runner the daily Action
    # uses has no guarantee of having it installed there.
    import PIL

    bundled = Path(PIL.__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"
    if bundled.exists():
        return ImageFont.truetype(str(bundled), size)

    try:
        return ImageFont.load_default(size=size)
    except TypeError as exc:
        # Pre-9.2 load_default() ignores `size` and returns a bitmap font, which
        # would render a tiny unreadable card. Failing beats posting that.
        raise CardRenderError(
            "No usable TrueType font found. Drop a .ttf into assets/fonts/."
        ) from exc


def _gradient(theme: str | None, size: int) -> Any:
    """A vertical two-stop gradient for `theme`."""
    from PIL import Image

    top, bottom = THEME_GRADIENTS.get((theme or "").strip().lower(), DEFAULT_GRADIENT)
    # Build one pixel wide and stretch: far cheaper than per-pixel work, and
    # the resize interpolates the stops smoothly.
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        ratio = y / max(1, size - 1)
        strip.putpixel(
            (0, y),
            tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)),
        )
    return strip.resize((size, size), Image.BILINEAR)


def _vignette(image: Any, size: int) -> Any:
    """Darken the corners slightly so text at the edges stays legible."""
    from PIL import Image, ImageDraw, ImageFilter

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    inset = size // 8
    draw.ellipse((-inset, -inset, size + inset, size + inset), fill=90)
    mask = mask.filter(ImageFilter.GaussianBlur(size // 12))
    shadow = Image.new("RGB", (size, size), (0, 0, 0))
    return Image.composite(image, Image.blend(image, shadow, 0.35), mask)


def wrap_measured(text: str, draw: Any, font: Any, max_width: float) -> list[str]:
    """Greedy word wrap against the font's real metrics.

    Measuring beats estimating characters-per-line: the quote font is
    proportional, so a line of "Ill" and a line of "WMW" differ by a factor of
    three, and an estimate tuned for one leaves the other either overflowing or
    using half the card.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}" if current else word
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [text]


def _wrap_to_fit(
    text: str, draw: Any, max_width: int, max_height: int
) -> tuple[list[str], Any, int]:
    """Largest size from the ladder at which `text` fits the box.

    Returns the wrapped lines, the font, and the line height. Falls through to
    the smallest size if nothing fits, so a pathologically long quote still
    renders rather than raising — seed_quotes already caps quote length, so
    this path should not be reachable with real data.
    """
    fallback: tuple[list[str], Any, int] | None = None

    for size in QUOTE_SIZE_LADDER:
        font = _load_font(QUOTE_FONT_CANDIDATES, size)
        lines = wrap_measured(text, draw, font, max_width)
        line_height = round(size * LINE_SPACING)

        if fallback is None:
            fallback = (lines, font, line_height)
        if line_height * len(lines) <= max_height:
            return lines, font, line_height

    assert fallback is not None  # the ladder is never empty
    log.warning("Quote does not fit the card cleanly at any size — using the smallest")
    return fallback


def _slug(text: str) -> str:
    """Short stable filename fragment derived from the quote itself."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def render_card(
    quote_text: str,
    *,
    theme: str | None = None,
    attribution: str | None = None,
    output_dir: Path | None = None,
    size: int = CARD_SIZE,
) -> Card:
    """Render `quote_text` to a square PNG and return where it landed.

    The filename is derived from the quote, so re-running a failed post
    overwrites the same file rather than accumulating near-identical cards in
    the repo.
    """
    from PIL import ImageDraw

    text = " ".join((quote_text or "").split())
    if not text:
        raise CardRenderError("Cannot render a card for an empty quote.")

    output_dir = output_dir or GENERATED_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    margin = round(CARD_MARGIN * size / CARD_SIZE)
    box_width = size - margin * 2
    # Reserve the bottom strip for the attribution line.
    box_height = size - margin * 2 - round(size * 0.09)

    image = _vignette(_gradient(theme, size), size)
    draw = ImageDraw.Draw(image)

    lines, font, line_height = _wrap_to_fit(text, draw, box_width, box_height)

    block_height = line_height * len(lines)
    y = (size - block_height) // 2 - round(size * 0.02)
    for line in lines:
        width = draw.textlength(line, font=font)
        x = (size - width) / 2
        # A soft drop shadow keeps the text readable over the lighter end of
        # every gradient without needing a solid plate behind it.
        draw.text((x + 2, y + 3), line, font=font, fill=(0, 0, 0, 120))
        draw.text((x, y), line, font=font, fill=(245, 245, 242))
        y += line_height

    if attribution:
        credit_font = _load_font(ATTRIBUTION_FONT_CANDIDATES, round(ATTRIBUTION_SIZE * size / CARD_SIZE))
        credit = " ".join(attribution.split())
        width = draw.textlength(credit, font=credit_font)
        draw.text(
            ((size - width) / 2, size - margin - round(size * 0.01)),
            credit,
            font=credit_font,
            fill=(196, 200, 208),
        )

    path = output_dir / f"card-{_slug(text)}.png"
    image.save(path, "PNG", optimize=True)
    log.info("Rendered quote card %s (theme=%r)", path.name, theme)

    # Alt text for an image whose content *is* text should be that text. This
    # also spares generate_post a vision call it would only use to read the
    # card back to us.
    alt = text if not attribution else f"{text} — {attribution}"
    return Card(path=path, alt_text=alt[:ALT_TEXT_MAX_CHARS], theme=theme)


def attribution_for(quote: Any) -> str | None:
    """Build the credit line from a quote row, if there is anything to credit."""
    author = (quote.get("author") or "").strip()
    title = (quote.get("book_title") or "").strip()
    if author and title:
        return f"{author}, {title}"
    return author or title or None


def main(argv: list[str] | None = None) -> int:
    """Preview a card without touching the database or Instagram."""
    import argparse

    from .main import setup_logging

    parser = argparse.ArgumentParser(description="Render a quote card to images/generated/.")
    parser.add_argument("--preview", required=True, help="Quote text to typeset")
    parser.add_argument("--theme", default=None, help="Theme, for the background tint")
    parser.add_argument("--attribution", default=None, help="Credit line, e.g. 'Author, Title'")
    args = parser.parse_args(argv)

    setup_logging()
    card = render_card(args.preview, theme=args.theme, attribution=args.attribution)
    print(card.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Card", "CardRenderError", "THEME_GRADIENTS", "attribution_for", "render_card"]
