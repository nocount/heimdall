"""Export recent YouTube /watch visits from Firefox history to bench/data/history.csv.

Firefox holds a lock on places.sqlite and keeps recent writes in the WAL, so
both files are copied to a temp dir and read from there.

    uv run --project daemon bench/export_history.py [--days 60] [--profile DIR]
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from heimdall import pages

DATA = Path(__file__).parent / "data"
PROFILE_ROOTS = [
    Path.home() / "snap/firefox/common/.mozilla/firefox",
    Path.home() / ".mozilla/firefox",
]

QUERY = """
SELECT p.url, p.title, COUNT(v.id) AS visits, MAX(v.visit_date) AS last_visit
FROM moz_places p JOIN moz_historyvisits v ON v.place_id = p.id
WHERE p.url LIKE '%youtube.com/watch%' AND v.visit_date > ?
GROUP BY p.id
ORDER BY last_visit DESC
"""


def find_profile() -> Path:
    candidates = [p for root in PROFILE_ROOTS if root.exists() for p in root.glob("*/places.sqlite")]
    if not candidates:
        sys.exit("no Firefox profile with places.sqlite found; pass --profile")
    # The profile you actually use is the one written most recently.
    return max(candidates, key=lambda p: p.stat().st_mtime).parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", type=Path, help="Firefox profile dir (default: most recently used)")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out", type=Path, default=DATA / "history.csv")
    args = ap.parse_args()

    profile = args.profile or find_profile()
    print(f"profile: {profile}")
    since_us = int((time.time() - args.days * 86400) * 1_000_000)

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("places.sqlite", "places.sqlite-wal"):
            if (profile / name).exists():
                shutil.copy2(profile / name, Path(tmp) / name)
        conn = sqlite3.connect(Path(tmp) / "places.sqlite")
        rows = conn.execute(QUERY, (since_us,)).fetchall()
        conn.close()

    videos: dict[str, dict] = {}
    for url, title, visits, last_visit in rows:
        page = pages.parse(url)
        if page.page_type != "watch" or not page.item_id:
            continue
        v = videos.setdefault(page.item_id, {
            "video_id": page.item_id, "url": f"https://www.youtube.com/watch?v={page.item_id}",
            "title": title or "", "visits": 0, "last_visit": 0,
        })
        v["visits"] += visits
        v["last_visit"] = max(v["last_visit"], last_visit)
        v["title"] = v["title"] or title or ""

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["video_id", "url", "title", "visits", "last_visit"])
        w.writeheader()
        for v in sorted(videos.values(), key=lambda v: -v["last_visit"]):
            w.writerow(v)
    print(f"{len(videos)} videos from the last {args.days} days -> {args.out}")


if __name__ == "__main__":
    main()
