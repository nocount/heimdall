"""Terminal labeler: allow or block each fetched video, appending to bench/data/labels.csv.

    uv run --project daemon bench/label.py              # label videos from meta.jsonl

Keys: a = allow, b = block, s = skip, d = full description, q = quit.
Label as you'd want heimdall to decide during focus hours.

Most of a watch history should be blocked, and the bench needs >= 30 labels
of each kind to say anything. So by default, likely allows come first (by
category, length and title/tag keywords); --order recent follows the history.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from collections import Counter
from pathlib import Path

from heimdall import labels

DATA = Path(__file__).parent / "data"
MIN_PER_CLASS = 30  # matches run_bench's GATE["min_labels"]

CATEGORY_WEIGHT = {
    "Education": 3, "Science & Technology": 3, "Howto & Style": 2, "News & Politics": 1,
    "Gaming": -3, "Comedy": -3, "Music": -3, "Entertainment": -2, "Film & Animation": -2, "Sports": -2,
}
ALLOW_WORDS = {
    "tutorial", "lecture", "course", "explained", "guide", "learn", "how", "paper", "research",
    "interview", "podcast", "talk", "ai", "a.i.", "llm", "llms", "agents", "inference", "model",
    "models", "math", "physics", "programming", "python", "rust", "engineering", "science",
}


def tally(rows: dict) -> str:
    c = Counter((r["site"], r["label"]) for r in rows.values())
    out = "  ".join(f"{site} {label}: {n}" for (site, label), n in sorted(c.items())) or "none yet"
    short = [f"{site} {label}" for site in {r["site"] for r in rows.values()} for label in labels.LABELS
             if c[(site, label)] < MIN_PER_CLASS]
    return out + (f"   (need {MIN_PER_CLASS}+: {', '.join(sorted(short))})" if short else "")


def likely_allow(row: dict) -> float:
    """Rough prior that a video is one you'd allow. Only orders the queue."""
    m = row["metadata"]
    score = CATEGORY_WEIGHT.get(m.get("category"), 0)
    words = set(f"{m.get('title', '')} {' '.join(m.get('tags') or [])}".lower().replace("|", " ").split())
    score += min(3, len(words & ALLOW_WORDS))
    if (row.get("duration") or 0) >= 20 * 60:  # lectures, talks and podcasts run long
        score += 1
    return score


def load_meta(path: Path) -> list[dict]:
    latest: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        latest[row["video_id"]] = row
    return [r for r in latest.values() if "metadata" in r]


def show(row: dict, full: bool = False) -> None:
    m = row["metadata"]
    mins = f"{round(row['duration'] / 60)} min" if row.get("duration") else "?"
    print(f"\n\033[1m{m['title']}\033[0m")
    print(f"  {m['channel']}  ·  {m.get('category')}  ·  {mins}  ·  {row['url']}")
    if m.get("tags"):
        print(f"  tags: {textwrap.shorten(', '.join(m['tags']), 160)}")
    desc = m.get("description") or ""
    if full:
        print(textwrap.indent(desc, "  | "))
    elif desc:
        print(textwrap.indent(textwrap.fill(textwrap.shorten(desc, 400), 100), "  | "))


def label_videos(args) -> None:
    existing = labels.load(args.labels)
    todo = [r for r in load_meta(args.meta) if labels.label_id("youtube", r["video_id"], r["url"]) not in existing]
    if args.order == "likely":
        todo.sort(key=likely_allow, reverse=True)  # stable: ties stay in history order
    print(f"{len(todo)} unlabeled videos. Tally: {tally(existing)}")
    for row in todo:
        show(row)
        while True:
            key = input("  [a]llow [b]lock [s]kip [d]esc [q]uit: ").strip().lower()
            if key == "d":
                show(row, full=True)
                continue
            if key in {"a", "b", "s", "q"}:
                break
        if key == "q":
            break
        if key == "s":
            continue
        label = "allow" if key == "a" else "block"
        lid = labels.label_id("youtube", row["video_id"], row["url"])
        labels.append(args.labels, id=lid, site="youtube", label=label, source="bench",
                      url=row["url"], metadata=row["metadata"])
        existing[lid] = {"site": "youtube", "label": label}
        print(f"  -> {label}.  Tally: {tally(existing)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meta", type=Path, default=DATA / "meta.jsonl")
    ap.add_argument("--labels", type=Path, default=DATA / "labels.csv")
    ap.add_argument("--order", choices=["likely", "recent"], default="likely",
                    help="likely allows first (default), or history order")
    args = ap.parse_args()
    label_videos(args)


if __name__ == "__main__":
    main()
