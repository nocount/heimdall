"""The atomic questions sent to the decision model, and the state they're asked about.

The daemon and the bench both build requests through here, so a bench result
predicts what the daemon will do with the same page.
"""

from __future__ import annotations

from .pages import Page

DESCRIPTION_CHARS = 1500

YOUTUBE_CONTENT_TYPES = {
    "tutorial_or_lecture": "Teaches a skill or subject step by step: tutorials, courses, lectures, walkthroughs",
    "technical_talk": "A conference talk, paper explanation, or deep technical discussion",
    "news": "Reports recent events or announcements",
    "commentary_or_opinion": "Reactions, hot takes, drama, opinion pieces, podcasts mostly about opinions",
    "entertainment": "Comedy, challenges, sketches, compilations, shows",
    "gaming": "Gameplay, let's plays, game reviews or streams",
    "music": "Music videos, songs, mixes",
    "vlog": "Personal or lifestyle vlogs",
    "other": None,
}

X_CONTENT_TYPES = {
    "technical": "Technical substance: explains how something works, shares code, results, or a paper",
    "news": "Announces or reports something that happened",
    "opinion": "Opinion, hot take, argument, or reaction",
    "meme_or_joke": "Meme, joke, or shitpost",
    "personal": "Personal life update or chit-chat",
    "other": None,
}


def youtube_questions() -> dict:
    return {
        "content_type": {
            "type": "choice",
            "instructions": "What kind of video is `video`?",
            "criteria": YOUTUBE_CONTENT_TYPES,
        },
        "educational": {
            "type": "noul",
            "instructions": "Would watching `video` primarily teach a skill or substantive knowledge, rather than entertain?",
        },
        "clickbait_mismatch": {
            "type": "noul",
            "instructions": "Does the title of `video` promise something educational that its description suggests it is not?",
        },
    }


def x_questions(subject: str) -> dict:
    return {
        "ai_related": {
            "type": "noul",
            "instructions": f"Is `{subject}` primarily about AI, machine learning, or LLMs?",
        },
        "content_type": {
            "type": "choice",
            "instructions": f"What kind of content is `{subject}`?",
            "criteria": X_CONTENT_TYPES,
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

    if page.site == "x":
        if page.page_type == "status":
            if not m.get("text"):
                raise ValueError("no tweet text in metadata")
            post = _clean({
                "author": m.get("author"),
                "text": m.get("text"),
                "quoted_text": m.get("quoted_text"),
            })
            return {"post": post}, x_questions("post")
        if page.page_type == "profile":
            if not (m.get("bio") or m.get("recent")):
                raise ValueError("no bio or posts in metadata")
            profile = _clean({
                "handle": m.get("handle") or (page.item_id or "").lstrip("@"),
                "name": m.get("name"),
                "bio": m.get("bio"),
                "recent_posts": (m.get("recent") or [])[:5],
            })
            return {"profile": profile}, x_questions("profile")
        if page.page_type == "search":
            query = (page.item_id or "").removeprefix("q:")
            if not query:
                raise ValueError("empty search query")
            return {"search": {"query": query}}, x_questions("search")

    raise ValueError(f"no question set for {page.site}/{page.page_type}")
