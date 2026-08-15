"""Claude calls: caption text, and alt text for the paired photo.

The caption is assembled here rather than asked for as one blob, so the layout
(quote, attribution, context, hashtags) is deterministic and the 2,200-character
Instagram limit can be enforced by trimming the parts in a sensible order.
Claude supplies only the parts that need judgement: the context paragraph and
the hashtags.

Alt text prefers the human-written `alt_text` on the manifest entry. Only when
that is missing does Claude look at the actual image file and describe it —
never write alt text for a photo the model hasn't seen, since a confident wrong
description is worse for a screen-reader user than none at all.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    ALT_TEXT_MAX_CHARS,
    CAPTION_MAX_CHARS,
    HASHTAG_MAX_COUNT,
    HASHTAG_TARGET_COUNT,
    Config,
    load_config,
)

log = logging.getLogger(__name__)

MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

# Instagram allows letters, digits and underscores in a tag.
_TAG_ALLOWED = re.compile(r"[^0-9A-Za-z_]")


class GenerationError(RuntimeError):
    """Claude did not return a usable caption."""


class CaptionRefused(GenerationError):
    """The model declined the request.

    main.py treats this as "this quote is a bad fit" and moves to the next
    candidate rather than failing the whole run — retrying the same quote
    tomorrow would just refuse again and wedge the schedule.
    """


@dataclass(frozen=True)
class GeneratedPost:
    caption: str
    alt_text: str | None
    context: str
    hashtags: list[str]


CAPTION_SYSTEM_PROMPT = """\
You write captions for an Instagram account that posts one quote a day from a \
paragliding masterclass book. The audience is cross-country pilots — people who \
fly, not people who watch videos of flying.

For the quote you are given, write two things:

1. A context paragraph of 40-80 words. Connect the quote to something concrete \
in real XC flying: a decision on a glide, reading a cycle on launch, committing \
to a crossing, managing fear on a bumpy day. Say something a pilot would nod at. \
Do not restate the quote in different words, do not open with "This quote \
reminds us", and do not address the reader as "you guys" or similar.

You may also be given an excerpt of the book page the quote came from. Use it \
only to understand the concrete scenario, technique, or reasoning the author is \
actually describing — it tells you what the quote is really about, so your take \
doesn't drift from the author's point. Do not summarize, paraphrase, or lift \
phrasing from the excerpt: write your own observation about the idea, in your \
own words, grounded in what the excerpt shows the quote means. If no excerpt is \
given, work from the quote and theme alone.

2. Between 5 and 10 hashtags. Specific and paragliding-relevant. No generic \
engagement tags (#love, #instagood, #photooftheday), no tag longer than 30 \
characters, and no duplicates.

Write plainly. No emoji, no exclamation marks, no hype."""

ALT_TEXT_SYSTEM_PROMPT = """\
You write alt text for photographs on a paragliding Instagram account, for \
readers using a screen reader.

Describe only what is actually visible: the glider and its position, the pilot \
if visible, the terrain, the weather and light, the framing. One or two \
sentences, under 400 characters. Do not speculate about location, mood, or what \
the pilot is feeling, do not begin with "Image of" or "A photo of", and do not \
mention the quote."""

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": "The 40-80 word context paragraph.",
        },
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "5-10 hashtags, with or without the leading '#'.",
        },
    },
    "required": ["context", "hashtags"],
    "additionalProperties": False,
}


def build_client(config: Config):
    """Build an Anthropic client. Imported lazily so modules that never call
    Claude (and the test suite) don't need the SDK loaded."""
    import anthropic

    config.require_anthropic()
    return anthropic.Anthropic(api_key=config.anthropic_api_key)


def first_text(response: Any) -> str:
    """First text block of a response, checking for a refusal first.

    Claude Opus 5 returns HTTP 200 with stop_reason "refusal" when its safety
    classifiers decline, and `content` is then empty — indexing it blindly
    would raise an IndexError that says nothing useful.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise CaptionRefused(f"Claude declined the request (category={category!r})")
    for block in response.content:
        if block.type == "text":
            return block.text
    raise GenerationError("Response contained no text block")


def normalize_hashtags(raw: list[str]) -> list[str]:
    """Clean model-supplied tags into things Instagram will accept.

    Strips punctuation and whitespace, collapses to a single leading '#',
    removes duplicates case-insensitively while keeping the first spelling, and
    caps the count.
    """
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        cleaned = _TAG_ALLOWED.sub("", str(item).strip().lstrip("#"))
        if not cleaned or len(cleaned) > 30:
            continue
        if cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        tags.append(f"#{cleaned}")
        if len(tags) >= HASHTAG_MAX_COUNT:
            break
    return tags


def _attribution(quote: dict[str, Any]) -> str:
    author = (quote.get("author") or "").strip()
    book = (quote.get("book_title") or "").strip()
    parts = [part for part in (author, book) if part]
    return "— " + ", ".join(parts) if parts else ""


def assemble_caption(quote: dict[str, Any], context: str, hashtags: list[str]) -> str:
    """Lay out the caption and keep it under Instagram's 2,200-char limit.

    Trimming order when it's too long: drop hashtags from the end first (each is
    cheap to lose), then truncate the context paragraph. The quote and its
    attribution are never trimmed — posting a half-quote would misrepresent the
    author, which is the one thing this account must not do.
    """
    quote_text = (quote.get("quote_text") or "").strip()
    if not quote_text:
        raise GenerationError("Quote has no text")

    head_parts = [f"“{quote_text}”"]
    attribution = _attribution(quote)
    if attribution:
        head_parts.append(attribution)
    head = "\n".join(head_parts)

    def build(ctx: str, tags: list[str]) -> str:
        sections = [head]
        if ctx.strip():
            sections.append(ctx.strip())
        if tags:
            sections.append(" ".join(tags))
        return "\n\n".join(sections)

    caption = build(context, hashtags)
    while len(caption) > CAPTION_MAX_CHARS and hashtags:
        hashtags = hashtags[:-1]
        caption = build(context, hashtags)

    if len(caption) > CAPTION_MAX_CHARS:
        overflow = len(caption) - CAPTION_MAX_CHARS
        context = context[: max(0, len(context) - overflow - 1)].rstrip() + "…"
        caption = build(context, hashtags)

    if len(caption) > CAPTION_MAX_CHARS:
        # Only reachable if the quote alone exceeds the limit.
        raise GenerationError(
            f"Quote is too long to caption: {len(caption)} chars exceeds {CAPTION_MAX_CHARS}"
        )
    return caption


def generate_caption_parts(
    quote: dict[str, Any], *, config: Config | None = None, client: Any = None
) -> tuple[str, list[str]]:
    """Ask Claude for the context paragraph and hashtags."""
    config = config or load_config()
    client = client or build_client(config)

    details = [f"Quote: {quote.get('quote_text', '').strip()}"]
    for label, key in (
        ("Author", "author"),
        ("Book", "book_title"),
        ("Chapter", "chapter"),
        ("Theme", "theme"),
    ):
        value = quote.get(key)
        if value:
            details.append(f"{label}: {value}")
    excerpt = (quote.get("context_excerpt") or "").strip()
    if excerpt:
        details.append(f"Excerpt from the book page (for grounding only):\n{excerpt}")
    details.append(f"Aim for about {HASHTAG_TARGET_COUNT} hashtags.")

    response = client.messages.create(
        model=config.anthropic_model,
        max_tokens=8000,
        system=CAPTION_SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": CAPTION_SCHEMA},
        },
        messages=[{"role": "user", "content": "\n".join(details)}],
    )

    raw = first_text(response)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GenerationError(f"Caption response was not valid JSON: {raw[:200]!r}") from exc

    context = str(payload.get("context", "")).strip()
    if not context:
        raise GenerationError("Caption response had an empty context paragraph")
    hashtags = normalize_hashtags(payload.get("hashtags") or [])
    if not hashtags:
        log.warning("Model returned no usable hashtags for quote %s", quote.get("id"))
    return context, hashtags


def generate_alt_text(
    image_path: Path, *, config: Config | None = None, client: Any = None
) -> str | None:
    """Describe the image for screen readers, by actually looking at it.

    Returns None rather than raising if the description can't be produced — alt
    text is a real accessibility win but not worth failing a post over.
    """
    config = config or load_config()
    media_type = MEDIA_TYPES.get(image_path.suffix.lower())
    if not media_type:
        log.warning("No media type for %s — skipping alt text", image_path.name)
        return None

    try:
        client = client or build_client(config)
        encoded = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
        response = client.messages.create(
            model=config.anthropic_model,
            max_tokens=2000,
            system=ALT_TEXT_SYSTEM_PROMPT,
            output_config={"effort": "low"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": "Write the alt text for this photo."},
                    ],
                }
            ],
        )
        return first_text(response).strip()[:ALT_TEXT_MAX_CHARS] or None
    except Exception:
        log.warning("Alt-text generation failed for %s — posting without it", image_path.name,
                    exc_info=True)
        return None


def build_post(
    quote: dict[str, Any],
    *,
    image_path: Path | None = None,
    manifest_alt_text: str | None = None,
    config: Config | None = None,
    client: Any = None,
) -> GeneratedPost:
    """Produce the caption and alt text for one post."""
    config = config or load_config()
    client = client or build_client(config)

    context, hashtags = generate_caption_parts(quote, config=config, client=client)
    caption = assemble_caption(quote, context, hashtags)

    alt_text = (manifest_alt_text or "").strip() or None
    if alt_text:
        alt_text = alt_text[:ALT_TEXT_MAX_CHARS]
    elif image_path is not None:
        alt_text = generate_alt_text(image_path, config=config, client=client)

    return GeneratedPost(
        caption=caption, alt_text=alt_text, context=context, hashtags=hashtags
    )
