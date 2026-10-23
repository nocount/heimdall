"""The labeled set (bench/data/labels.csv), shared by the bench labeler and `heimdall review`.

Each row carries the URL and the same metadata dict the extension sends, so the
bench can rebuild the exact classifier request the daemon would make.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ["id", "site", "label", "source", "url", "metadata", "ts"]
LABELS = ("allow", "block")


def label_id(site: str, item_id: str | None, url: str) -> str:
    return f"{site}:{item_id or url}"


def append(path: Path, *, id: str, site: str, label: str, source: str, url: str, metadata: dict) -> None:
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "id": id, "site": site, "label": label, "source": source, "url": url,
            "metadata": json.dumps(metadata, ensure_ascii=False),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })


def load(path: Path) -> dict[str, dict]:
    """id -> row, with metadata decoded. A later row for the same id wins (relabels)."""
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            row["metadata"] = json.loads(row["metadata"] or "{}")
            rows[row["id"]] = row
    return rows
