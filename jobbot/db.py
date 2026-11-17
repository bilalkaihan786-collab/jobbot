"""SQLite tracker: every job seen, its score, documents and application status."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

from jobbot.models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    -- new | filtered | scored | rejected | ready | applied | needs_manual | failed | skipped
    score INTEGER,
    assessment TEXT,
    resume_path TEXT,
    cover_letter_path TEXT,
    note TEXT,
    first_seen TEXT,
    updated TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add_new(self, jobs: list[Job]) -> list[Job]:
        """Insert jobs we haven't seen before; return only the new ones."""
        new = []
        for j in jobs:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO jobs(key, data, first_seen, updated) VALUES (?,?,?,?)",
                (j.key, json.dumps(asdict(j)), now(), now()))
            if cur.rowcount:
                new.append(j)
        self.conn.commit()
        return new

    def update(self, key: str, **fields):
        fields["updated"] = now()
        if "assessment" in fields and not isinstance(fields["assessment"], str):
            fields["assessment"] = json.dumps(fields["assessment"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE key=?", (*fields.values(), key))
        self.conn.commit()

    def get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()

    def by_status(self, *statuses: str, order: str = "score DESC") -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY {order}", statuses).fetchall()

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status")}

    def applied_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='applied' AND updated LIKE ?",
            (today + "%",)).fetchone()[0]

    @staticmethod
    def job(row: sqlite3.Row) -> Job:
        return Job(**json.loads(row["data"]))
