from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Outcome(Enum):
    SUCCESS = "success"
    CONTENT_FAILURE = "content_failure"


@dataclass(frozen=True)
class StateRecord:
    relpath: str
    fingerprint: str
    outcome: Outcome
    created_at: float
    updated_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    relpath     TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
) WITHOUT ROWID;
"""


class StateStore:
    def __init__(self, path: Path | str, *, now=time.time) -> None:
        self._path = Path(path)
        self._now = now
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def upsert(self, relpath: str, fingerprint: str, outcome: Outcome) -> StateRecord:
        now = self._now()
        self._conn.execute(
            """
            INSERT INTO processed (relpath, fingerprint, outcome, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(relpath) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                outcome     = excluded.outcome,
                updated_at  = excluded.updated_at
            """,
            (relpath, fingerprint, outcome.value, now, now),
        )
        record = self.get(relpath)
        assert record is not None
        return record

    def get(self, relpath: str) -> StateRecord | None:
        row = self._conn.execute(
            "SELECT * FROM processed WHERE relpath = ?", (relpath,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_all(self) -> list[StateRecord]:
        rows = self._conn.execute(
            "SELECT * FROM processed ORDER BY relpath"
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def delete(self, relpath: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM processed WHERE relpath = ?", (relpath,)
        )
        return cur.rowcount > 0


def _row_to_record(row: sqlite3.Row) -> StateRecord:
    return StateRecord(
        relpath=row["relpath"],
        fingerprint=row["fingerprint"],
        outcome=Outcome(row["outcome"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
