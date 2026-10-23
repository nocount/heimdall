"""SQLite store: decisions, appeals, grants, heartbeats, and the answer cache."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    url           TEXT NOT NULL,
    site          TEXT NOT NULL,
    page_type     TEXT NOT NULL,
    item_id       TEXT,
    metadata      TEXT,
    source        TEXT NOT NULL,   -- schedule | grant | rule | channel | cache | model | error
    model         TEXT,
    probs         TEXT,            -- raw answers JSON from the decision model
    verdict       TEXT NOT NULL,   -- what the policy decided (the would-be verdict in shadow)
    served        TEXT NOT NULL,   -- what the extension was told
    shadow        INTEGER NOT NULL,
    focus         INTEGER NOT NULL,
    latency_ms    INTEGER,
    reason        TEXT,
    category      TEXT,
    review_label  TEXT,            -- allow | block, set by `heimdall review`
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS appeals (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    decision_id    INTEGER REFERENCES decisions(id),
    justification  TEXT NOT NULL,
    model_output   TEXT,
    granted        INTEGER NOT NULL,
    minutes        INTEGER,
    site           TEXT,
    key            TEXT              -- same key as grants: item id when known, else the URL
);

CREATE TABLE IF NOT EXISTS grants (
    id          INTEGER PRIMARY KEY,
    site        TEXT NOT NULL,
    key         TEXT NOT NULL,     -- item id when known, else the URL
    expires_at  TEXT NOT NULL,
    appeal_id   INTEGER REFERENCES appeals(id)
);
CREATE INDEX IF NOT EXISTS grants_key ON grants(site, key);

CREATE TABLE IF NOT EXISTS heartbeats (
    client     TEXT PRIMARY KEY,
    last_seen  TEXT NOT NULL,
    version    TEXT
);

CREATE TABLE IF NOT EXISTS answer_cache (
    site        TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    model       TEXT NOT NULL,
    answers     TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    PRIMARY KEY (site, item_id)
);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across uvicorn's threadpool; the lock keeps it safe.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        added = {"appeals": [("site", "TEXT"), ("key", "TEXT")]}
        for table, cols in added.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock, self.conn:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    # decisions

    def log_decision(self, **d) -> int:
        for k in ("metadata", "probs"):
            if d.get(k) is not None:
                d[k] = json.dumps(d[k])
        cols = ", ".join(d)
        marks = ", ".join("?" for _ in d)
        cur = self.execute(f"INSERT INTO decisions ({cols}) VALUES ({marks})", tuple(d.values()))
        return cur.lastrowid

    def decision(self, decision_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        return rows[0] if rows else None

    def recent_decisions(self, limit: int, verdict: str | None = None, unreviewed: bool = False,
                         model_only: bool = False) -> list[sqlite3.Row]:
        where, params = [], []
        if verdict:
            where.append("verdict = ?")
            params.append(verdict)
        if unreviewed:
            where.append("review_label IS NULL")
        if model_only:
            where.append("source IN ('model', 'cache')")
        sql = "SELECT * FROM decisions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        return self.query(sql, (*params, limit))

    def set_review(self, decision_id: int, label: str, now: datetime) -> None:
        self.execute("UPDATE decisions SET review_label = ?, reviewed_at = ? WHERE id = ?",
                     (label, iso(now), decision_id))

    # grants

    def active_grant(self, site: str, key: str, now: datetime) -> str | None:
        """The latest expiry of a live grant for this page, or None."""
        rows = self.query("SELECT MAX(expires_at) AS e FROM grants WHERE site = ? AND key = ? AND expires_at > ?",
                          (site, key, iso(now)))
        return rows[0]["e"] if rows else None

    def add_grant(self, site: str, key: str, expires_at: datetime, appeal_id: int) -> None:
        self.execute("INSERT INTO grants (site, key, expires_at, appeal_id) VALUES (?, ?, ?, ?)",
                     (site, key, iso(expires_at), appeal_id))

    # appeals

    def add_appeal(self, *, ts: datetime, decision_id: int, justification: str, model_output: dict | None,
                   granted: bool, minutes: int | None, site: str, key: str) -> int:
        cur = self.execute(
            "INSERT INTO appeals (ts, decision_id, justification, model_output, granted, minutes, site, key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(ts), decision_id, justification, json.dumps(model_output) if model_output else None,
             int(granted), minutes, site, key),
        )
        return cur.lastrowid

    def appeals_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.query(
            "SELECT a.*, d.url FROM appeals a LEFT JOIN decisions d ON d.id = a.decision_id "
            "WHERE a.ts >= ? ORDER BY a.id", (iso(since),))

    def last_denial(self, site: str, key: str) -> str | None:
        rows = self.query("SELECT MAX(ts) AS t FROM appeals WHERE site = ? AND key = ? AND granted = 0", (site, key))
        return rows[0]["t"] if rows else None

    # heartbeats

    def heartbeat(self, client: str, version: str | None, now: datetime) -> None:
        self.execute(
            "INSERT INTO heartbeats (client, last_seen, version) VALUES (?, ?, ?) "
            "ON CONFLICT(client) DO UPDATE SET last_seen = excluded.last_seen, version = excluded.version",
            (client, iso(now), version),
        )

    def last_heartbeat(self) -> str | None:
        rows = self.query("SELECT MAX(last_seen) AS t FROM heartbeats")
        return rows[0]["t"] if rows else None

    # answer cache

    def cached_answers(self, site: str, item_id: str, now: datetime) -> tuple[str, dict] | None:
        rows = self.query("SELECT model, answers FROM answer_cache WHERE site = ? AND item_id = ? AND expires_at > ?",
                          (site, item_id, iso(now)))
        if not rows:
            return None
        return rows[0]["model"], json.loads(rows[0]["answers"])

    def cache_answers(self, site: str, item_id: str, model: str, answers: dict, expires_at: datetime) -> None:
        self.execute(
            "INSERT OR REPLACE INTO answer_cache (site, item_id, model, answers, expires_at) VALUES (?, ?, ?, ?, ?)",
            (site, item_id, model, json.dumps(answers), iso(expires_at)),
        )
