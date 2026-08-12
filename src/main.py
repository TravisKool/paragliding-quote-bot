"""Daily orchestration entrypoint: python -m src.main

Phase 7. select_quote -> generate_post -> pick_image -> publish_image_host ->
instagram_client.publish -> write the posts row and stamp quotes.used_at.

Two invariants:
  - nothing marks a quote used until the publish succeeds
  - any failure exits non-zero, so the GitHub Actions failure email fires
"""
