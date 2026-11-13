"""URL -> (site, page_type, item_id). The daemon owns this; the extension only reports URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 # Privacy-enhanced embeds; same player, same video IDs.
                 "youtube-nocookie.com", "www.youtube-nocookie.com"}
X_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}

# First path segments on X that are app routes, not user handles.
X_RESERVED = {
    "home", "explore", "notifications", "messages", "i", "settings", "search",
    "compose", "login", "logout", "signup", "tos", "privacy", "jobs", "hashtag",
    "communities", "premium", "account", "intent", "share",
}
REDDIT_POST = re.compile(r"^/(?:r/[^/]+/|u(?:ser)?/[^/]+/)?comments/([a-z0-9]+)", re.I)
REDDIT_SORTS = {"hot", "new", "top", "rising", "controversial", "best"}
X_STATUS = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)")
X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


@dataclass(frozen=True)
class Page:
    url: str
    site: str  # youtube | x | other
    host: str
    path: str
    page_type: str
    item_id: str | None = None  # cache key within the site


def _youtube(url: str, host: str, path: str, query: dict) -> Page:
    def page(page_type: str, item_id: str | None = None) -> Page:
        return Page(url, "youtube", host, path, page_type, item_id)

    if path == "/watch":
        vid = query.get("v", [None])[0]
        return page("watch", vid)
    if path.startswith("/shorts/"):
        return page("shorts", path.split("/")[2] or None)
    if path.startswith("/embed/"):
        # The player inside an iframe on some other site. Keyed by video ID,
        # so a grant for /watch?v=ID also opens the embed. "videoseries" is
        # a playlist embed, with no single video to key on.
        vid = path.split("/")[2]
        return page("embed", vid if vid and vid != "videoseries" else None)
    if path == "/":
        return page("home")
    if path.startswith("/feed/"):
        return page("feed")
    if path == "/results":
        return page("search")
    if path.startswith(("/@", "/channel/", "/c/", "/user/")):
        return page("channel")
    if path == "/playlist":
        return page("playlist")
    return page("other")


def _x(url: str, host: str, path: str, query: dict) -> Page:
    def page(page_type: str, item_id: str | None = None) -> Page:
        return Page(url, "x", host, path, page_type, item_id)

    if m := X_STATUS.match(path):
        return page("status", m.group(2))
    if path == "/search":
        q = query.get("q", [""])[0].strip()
        return page("search", f"q:{q.lower()}" if q else None)
    first = path.strip("/").split("/")[0]
    if first in {"home", "explore", "notifications"}:
        return page(first)
    if path.startswith("/i/lists/") or (first and "/lists" in path):
        return page("list")
    if first and first.lower() not in X_RESERVED and X_HANDLE.match(first):
        return page("profile", f"@{first.lower()}")
    if path in ("", "/"):
        return page("root")
    return page("other")


def _reddit(url: str, host: str, path: str) -> Page:
    def page(page_type: str, item_id: str | None = None) -> Page:
        return Page(url, "reddit", host, path, page_type, item_id)

    if m := REDDIT_POST.match(path):
        return page("post", m.group(1).lower())
    parts = [p for p in path.split("/") if p]
    if not parts or (len(parts) == 1 and parts[0] in REDDIT_SORTS):
        return page("home")
    if parts[0] == "r" and len(parts) >= 2:
        if len(parts) == 2 or parts[2] in REDDIT_SORTS:
            # r/all and r/popular are the front page by another name.
            return page("home" if parts[1].lower() in {"all", "popular"} else "subreddit")
        return page("other")  # wiki, about, a share link mid-redirect...
    if parts[0] in {"user", "u"} and len(parts) >= 2:
        return page("user", f"u:{parts[1].lower()}")
    if parts[0] == "search":
        return page("search")
    return page("other")


def parse(url: str) -> Page:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    query = parse_qs(parts.query)
    if host in YOUTUBE_HOSTS:
        return _youtube(url, host, path, query)
    if host in X_HOSTS:
        return _x(url, host, path, query)
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return _reddit(url, host, path)
    return Page(url, "other", host, path, "other")


def host_matches(host: str, domains: list[str]) -> bool:
    """True if host is one of `domains` or a subdomain of one."""
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in (d.lower().strip(".") for d in domains))


# Feeds and Shorts can't be appealed: a grant opens one page, and on these that
# page is the endless scroll itself.
NOT_APPEALABLE = {
    "youtube": {"home", "shorts", "feed"},
    "x": {"home", "explore", "notifications", "list", "root"},
    "reddit": {"home", "subreddit"},
}


def appealable(page: Page) -> bool:
    return page.page_type not in NOT_APPEALABLE.get(page.site, set())


# Page types that need the classifier; everything else is decided by rules.
# X isn't classified: it's in hard_block_domains, and appeals open single pages.
MODEL_PAGE_TYPES = {
    "youtube": {"watch"},
}
