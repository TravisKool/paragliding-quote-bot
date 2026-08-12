"""Claude call: turn a quote row into an Instagram caption.

Phase 4. Produces quote + attribution + a short paragraph connecting it to real
XC flying + 5-10 hashtags, and alt text for the image (Phase 10).

Constrained by config.CAPTION_MAX_CHARS (2200) and HASHTAG_TARGET_COUNT.
Keep the prompt and the output parsing in one place so tone is easy to tune.
"""
