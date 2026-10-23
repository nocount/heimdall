"""Terminal labeler: allow or block each fetched video, appending to bench/data/labels.csv.

    uv run --project daemon bench/label.py              # label videos from meta.jsonl
    uv run --project daemon bench/label.py --x-csv bench/data/x_samples.csv

Keys: a = allow, b = block, s = skip, d = full description, q = quit.
Label as you'd want heimdall to decide during focus hours. Aim for ~80 YouTube
labels with a deliberate mix; the running tally helps.
"""

from __future__ import annotations

import argparse
import csv
import json
import textwrap
from collections import Counter
from pathlib import Path

from heimdall import labels, pages

DATA = Path(__file__).parent / "data"


def tally(rows: dict) -> str:
    c = Counter((r["site"], r["label"]) for r in rows.values())
    return "  ".join(f"{site} {label}: {n}" for (site, label), n in sorted(c.items())) or "none yet"


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


def import_x(args) -> None:
    """CSV columns: label,url,author,text,quoted_text (url must be a /status/ link)."""
    added = 0
    with args.x_csv.open(newline="") as f:
        for i, row in enumerate(csv.DictReader(f), 2):
            page = pages.parse(row["url"])
            if page.page_type != "status":
                print(f"line {i}: skipping, not a post URL: {row['url']}")
                continue
            labels.append(
                args.labels, id=labels.label_id("x", page.item_id, row["url"]), site="x",
                label=row["label"].strip().lower(), source="bench", url=row["url"],
                metadata={k: row.get(k) or None for k in ("author", "text", "quoted_text")},
            )
            added += 1
    print(f"imported {added} X samples. Tally: {tally(labels.load(args.labels))}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meta", type=Path, default=DATA / "meta.jsonl")
    ap.add_argument("--labels", type=Path, default=DATA / "labels.csv")
    ap.add_argument("--x-csv", type=Path, help="import hand-collected X samples instead")
    args = ap.parse_args()
    if args.x_csv:
        import_x(args)
    else:
        label_videos(args)


if __name__ == "__main__":
    main()
