"""`heimdall serve | status | review`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from . import labels
from .config import load_config
from .db import DB

DEFAULT_LABELS = Path(__file__).resolve().parents[2] / "bench" / "data" / "labels.csv"


def cmd_serve(args) -> None:
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(load_config(args.config))
    # Never bind anything but loopback: the API has no auth beyond the origin check.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info" if args.access_log else "warning",
                access_log=args.access_log)


def cmd_status(args) -> None:
    import httpx

    try:
        resp = httpx.get(f"http://127.0.0.1:{args.port}/v1/status", timeout=2)
        print(json.dumps(resp.json(), indent=2))
    except httpx.HTTPError as e:
        sys.exit(f"heimdall daemon not reachable on :{args.port}: {e}")


def _summary(row) -> str:
    meta = json.loads(row["metadata"] or "null") or {}
    if row["site"] == "youtube":
        lines = [f"{meta.get('title', '?')}  —  {meta.get('channel', '?')} ({meta.get('channel_id', '?')})"
                 f"  [{meta.get('category', '?')}]"]
        if desc := (meta.get("description") or "").strip():
            lines.append(textwrap.shorten(desc, 220))
    else:
        lines = [row["url"]]
    return "\n      ".join(lines)


def cmd_review(args) -> None:
    config = load_config(args.config)
    db = DB(config.db_path)
    verdict = {"blocks": "block", "allows": "allow"}.get(args.which)
    rows = db.recent_decisions(args.n, verdict=verdict, unreviewed=not args.all, model_only=True)
    if not rows:
        print("nothing to review")
        return

    if args.list:
        for r in rows:
            mark = f" [reviewed: {r['review_label']}]" if r["review_label"] else ""
            print(f"#{r['id']} {r['ts']} {r['verdict'].upper():5} {r['reason']}{mark}\n      {_summary(r)}")
        return

    print(f"{len(rows)} decisions. For each: [c]orrect, [w]rong, [s]kip, [q]uit.\n")
    marked = 0
    for r in rows:
        print(f"#{r['id']} {r['ts']} {r['url']}")
        print(f"      {_summary(r)}")
        print(f"   -> {r['verdict'].upper()}: {r['reason']}")
        while (key := input("   [c/w/s/q] ").strip().lower()) not in {"c", "w", "s", "q"}:
            pass
        if key == "q":
            break
        if key == "s":
            continue
        label = r["verdict"] if key == "c" else ("allow" if r["verdict"] == "block" else "block")
        db.set_review(r["id"], label, datetime.now(timezone.utc))
        labels.append(
            args.labels, id=labels.label_id(r["site"], r["item_id"], r["url"]), site=r["site"],
            label=label, source="review", url=r["url"], metadata=json.loads(r["metadata"] or "{}"),
        )
        marked += 1
        print()
    print(f"labeled {marked}; appended to {args.labels}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="heimdall")
    p.add_argument("--config", help="config path (default: $HEIMDALL_CONFIG or /etc/heimdall/config.yaml)")
    p.add_argument("--port", type=int, default=int(os.environ.get("HEIMDALL_PORT", 7878)))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the daemon on 127.0.0.1")
    s.add_argument("--access-log", action="store_true", help="log every request (noisy: heartbeats)")
    s.set_defaults(func=cmd_serve)
    sub.add_parser("status", help="show daemon status").set_defaults(func=cmd_status)

    r = sub.add_parser("review", help="review recent model decisions and label mistakes")
    r.add_argument("which", nargs="?", choices=["all", "blocks", "allows"], default="all")
    r.add_argument("-n", type=int, default=20, help="how many recent decisions")
    r.add_argument("--all", action="store_true", help="include already-reviewed decisions")
    r.add_argument("--list", action="store_true", help="just list, don't prompt")
    r.add_argument("--labels", type=Path, default=DEFAULT_LABELS, help="labels CSV to append to")
    r.set_defaults(func=cmd_review)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
