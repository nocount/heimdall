"""The atomic questions sent to the decision model, and the state they're asked about.

The daemon and the bench both build requests through here, so a bench result
predicts what the daemon will do with the same page.
"""

from __future__ import annotations

from .pages import Page

DESCRIPTION_CHARS = 1500

def youtube_questions() -> dict:
    """One question per video. Phase 0 tried content_type + educational +
    clickbait_mismatch, then ai_related + substantive; this single question
    separated the labels best (leave-one-out: 16/23 allows kept vs 8/23 for
    educational) at a third of the request size."""
    return {
        "worth_learning": {
            "type": "noul",
            "instructions": "Would `video` be worth watching for someone who wants to learn about AI, "
                            "technology, or science, rather than to be entertained?",
        },
    }


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def build_request(page: Page, metadata: dict) -> tuple[dict, dict]:
    """Return (state, questions) for a page that needs the model.

    Raises ValueError when metadata is too thin to judge, so the caller can
    fail closed instead of asking the model about an empty page.
    """
    m = metadata or {}
    if page.site == "youtube":
        if not m.get("title"):
            raise ValueError("no video title in metadata")
        video = _clean({
            "title": m.get("title"),
            "channel": m.get("channel"),
            "category": m.get("category"),
            "tags": (m.get("tags") or [])[:30],
            "description": (m.get("description") or "")[:DESCRIPTION_CHARS],
        })
        return {"video": video}, youtube_questions()

    raise ValueError(f"no question set for {page.site}/{page.page_type}")
