"""URL -> (site, page_type, item_id). The daemon owns this; the extension only reports URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
X_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}

# First path segments on X that are app routes, not user handles.
X_RESERVED = {
    "home", "explore", "notifications", "messages", "i", "settings", "search",
    "compose", "login", "logout", "signup", "tos", "privacy", "jobs", "hashtag",
    "communities", "premium", "account", "intent", "share",
}
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


def parse(url: str) -> Page:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    query = parse_qs(parts.query)
    if host in YOUTUBE_HOSTS:
        return _youtube(url, host, path, query)
    if host in X_HOSTS:
        return _x(url, host, path, query)
    return Page(url, "other", host, path, "other")


def host_matches(host: str, domains: list[str]) -> bool:
    """True if host is one of `domains` or a subdomain of one."""
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in (d.lower().strip(".") for d in domains))


# Page types that need the classifier; everything else is decided by rules.
MODEL_PAGE_TYPES = {
    "youtube": {"watch"},
    "x": {"status", "profile", "search"},
}
