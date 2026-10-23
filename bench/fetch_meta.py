"""Fetch title, channel, description, tags and category for each exported video via yt-dlp.

Writes bench/data/meta.jsonl, one line per video, with `metadata` in the same
shape the extension sends. Already-fetched videos are skipped, so it resumes.

    uv run --project daemon bench/fetch_meta.py [--limit N] [--workers 4]
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DATA = Path(__file__).parent / "data"


def yt_dlp_path() -> str:
    found = shutil.which("yt-dlp") or str(Path.home() / ".local/bin/yt-dlp")
    if not Path(found).exists():
        raise SystemExit("yt-dlp not found; `uv tool install yt-dlp`")
    return found


def fetch(binary: str, video_id: str, url: str) -> dict:
    proc = subprocess.run(
        [binary, "--dump-json", "--skip-download", "--no-warnings", "--no-playlist", url],
        capture_output=True, text=True, timeout=90,
    )
    if proc.returncode != 0:
        errors = [l for l in proc.stderr.splitlines() if l.startswith("ERROR:")]
        return {"video_id": video_id, "url": url, "error": (errors[-1] if errors else proc.stderr.strip())[:400]}
    info = json.loads(proc.stdout)
    return {
        "video_id": video_id,
        "url": url,
        "duration": info.get("duration"),
        "metadata": {
            "video_id": video_id,
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "channel_id": info.get("channel_id"),
            "description": info.get("description") or "",
            "tags": info.get("tags") or [],
            "category": (info.get("categories") or [None])[0],
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", type=Path, default=DATA / "history.csv")
    ap.add_argument("--out", type=Path, default=DATA / "meta.jsonl")
    ap.add_argument("--limit", type=int, default=1000, help="max videos to fetch this run")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()

    done: dict[str, dict] = {}
    if args.out.exists():
        for line in args.out.read_text().splitlines():
            row = json.loads(line)
            done[row["video_id"]] = row
    skip = {vid for vid, row in done.items() if not args.retry_failed or "error" not in row}

    with args.history.open(newline="") as f:
        todo = [r for r in csv.DictReader(f) if r["video_id"] not in skip][: args.limit]
    if not todo:
        print("nothing to fetch")
        return

    binary = yt_dlp_path()
    ok = failed = 0
    with ThreadPoolExecutor(args.workers) as pool, args.out.open("a") as out:
        futures = [pool.submit(fetch, binary, r["video_id"], r["url"]) for r in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            if "error" in row:
                failed += 1
                print(f"[{i}/{len(todo)}] {row['video_id']}: FAILED {row['error'][:100]}")
            else:
                ok += 1
                print(f"[{i}/{len(todo)}] {row['video_id']}: {row['metadata']['title']}")
    print(f"fetched {ok}, failed {failed} -> {args.out}")


if __name__ == "__main__":
    main()
