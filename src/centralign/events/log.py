"""Append-only event log plus run snapshots.

Two stores with deliberately different jobs:

* **Events** are an immutable, ordered audit trail. Nothing updates or deletes a
  row. This is what answers "why did the agent do that?" after the fact, and
  it is what the live dashboard renders. An enterprise deploying an AI employee
  needs this far more than it needs clever prompting.
* **Snapshots** are the latest serialized :class:`~centralign.runtime.state.RunState`
  for each run. Recovery reloads a snapshot rather than folding the whole event
  stream.

Why both, rather than pure event sourcing? Folding events is the elegant
answer, but it means every state field needs a replay rule, and a single
missing rule corrupts recovery silently. Snapshots make recovery a dumb,
obviously-correct load; the event log keeps the audit guarantee that snapshots
alone would lose. The trade is a little redundancy for a much smaller blast
radius.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT    NOT NULL,
    seq      INTEGER NOT NULL,
    ts       REAL    NOT NULL,
    type     TEXT    NOT NULL,
    step_id  TEXT,
    payload  TEXT    NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_run_seq ON events (run_id, seq);
CREATE INDEX IF NOT EXISTS events_type ON events (type);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id     TEXT PRIMARY KEY,
    updated_at REAL NOT NULL,
    status     TEXT NOT NULL,
    goal       TEXT NOT NULL,
    state      TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Event:
    run_id: str
    seq: int
    ts: float
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "step_id": self.step_id,
            "payload": self.payload,
        }


def new_run_id() -> str:
    """Time-ordered, human-skimmable run identifier."""
    return f"run_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}"


class EventLog:
    """Thread-safe append-only log over SQLite.

    SQLite rather than Postgres because a prototype should be clonable and
    runnable with no services; the interface is narrow enough that swapping the
    backing store later touches only this file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # WAL lets the dashboard read while a run is writing.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()
        self._subscribers: list[Callable[[Event], None]] = []
        self._seq: dict[str, int] = {}

    # -- fan-out ------------------------------------------------------------
    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Register a listener; returns an unsubscribe callable."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    # -- writes -------------------------------------------------------------
    def append(
        self,
        run_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
    ) -> Event:
        with self._lock:
            if run_id not in self._seq:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE run_id = ?", (run_id,)
                ).fetchone()
                self._seq[run_id] = int(row["s"])
            self._seq[run_id] += 1
            event = Event(
                run_id=run_id,
                seq=self._seq[run_id],
                ts=time.time(),
                type=type,
                payload=payload or {},
                step_id=step_id,
            )
            self._conn.execute(
                "INSERT INTO events (run_id, seq, ts, type, step_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.run_id,
                    event.seq,
                    event.ts,
                    event.type,
                    event.step_id,
                    json.dumps(event.payload, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()

        # Fan out outside the lock: a slow subscriber must not stall the run,
        # and a broken one must not fail it.
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - observers are strictly best-effort
                pass
        return event

    def save_snapshot(self, run_id: str, *, status: str, goal: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (run_id, updated_at, status, goal, state) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "updated_at=excluded.updated_at, status=excluded.status, state=excluded.state",
                (run_id, time.time(), status, goal, json.dumps(state, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    # -- reads --------------------------------------------------------------
    def events(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, after_seq)
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def iter_events(self, run_id: str) -> Iterator[Event]:
        yield from self.events(run_id)

    def load_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["state"]) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, updated_at, status, goal FROM snapshots "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_run_id(self) -> str | None:
        runs = self.list_runs(limit=1)
        return runs[0]["run_id"] if runs else None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            run_id=row["run_id"],
            seq=row["seq"],
            ts=row["ts"],
            type=row["type"],
            payload=json.loads(row["payload"]),
            step_id=row["step_id"],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
