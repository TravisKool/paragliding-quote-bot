"""Meta Graph API wrapper: the two-step image publish flow.

Phase 6. POST /{ig-user-id}/media with image_url + caption (+ alt_text) to get a
container id, then POST /{ig-user-id}/media_publish with that id.

Graph API returns HTTP 200 with an `error` object on some failures — surface
those loudly rather than treating a 200 as success.
"""
