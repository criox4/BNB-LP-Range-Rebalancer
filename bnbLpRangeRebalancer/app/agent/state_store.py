"""Durable agent state in SQLite — replaces the whole-file JSON rewrite.

## Why this exists

The state file was rewritten in full on every write, via
``Path.write_text``, on a path that runs **every 60 seconds** (``last_check`` +
the fee snapshot). That has two faults, and the second one costs money:

1. **O(n) writes.** ``history`` and ``snapshots`` are append-only and only ever
   grow, so each 60s tick serialised the entire accumulated record to add one
   row.
2. **Non-atomic writes.** ``write_text`` truncates and then writes. A crash, an
   OOM kill, or a full disk between those two leaves a truncated file.
   ``load_state`` catches the parse error, logs "starting fresh", and returns
   defaults — at which point ``token_id`` falls back to the ``studio.toml``
   BOOTSTRAP value, which is very likely an NFT a past rebalance already
   emptied (B10). A badly-timed restart could therefore point the agent at a
   dead position, and the only symptom is one WARNING line.

SQLite fixes both: appends are one INSERT, and every write is a transaction
that either lands or does not. Both are properties the file could not have.

## Why SQLite specifically

It is in the standard library — no service, no credentials, no container, no
backup story beyond "copy the volume". The agent and the service are two
processes on one host sharing a volume, which is exactly what WAL mode handles.

The one thing it does NOT give is cross-HOST coordination. The single-flight
rebalance guard is still ``flock`` and still cannot see a process on another
machine (spec 11). If the two layers are ever split across hosts, that is the
reason to move to Postgres — advisory locks, not storage.

## Shape

``load_state()`` returns the SAME dict the JSON file did, so every reader
(``api.py``, ``strategy.py``, the tests) is unchanged:

    scalars    key/value rows, JSON-encoded    -> merged into the top level
    history    one row per rebalance           -> state["history"]
    snapshots  one row per fee/TVL sample      -> state["snapshots"]
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS scalars (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL          -- JSON-encoded, so None/int/float/str round-trip
);
CREATE TABLE IF NOT EXISTS history (
    seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    entry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    entry TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots (ts);
"""


class StateStore:
    """One database file, one network. Cheap to construct; opens per operation."""

    def __init__(self, path: Path, defaults: dict[str, Any]):
        self.path = Path(path)
        self._defaults = defaults
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        # timeout: the monitor thread and an operator CLI run in different
        # PROCESSES, so a writer can genuinely be mid-transaction. Wait rather
        # than raising "database is locked" into a rebalance.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")     # concurrent readers
            conn.execute("PRAGMA synchronous=FULL")     # money: durability over speed
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:                                   # commit / rollback
                yield conn
        finally:
            conn.close()

    # --- reads ------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        state = dict(self._defaults)
        with self._conn() as c:
            for key, value in c.execute("SELECT key, value FROM scalars"):
                state[key] = json.loads(value)
            state["history"] = [
                json.loads(r[0]) for r in c.execute(
                    "SELECT entry FROM history ORDER BY seq")
            ]
            state["snapshots"] = [
                json.loads(r[0]) for r in c.execute(
                    "SELECT entry FROM snapshots ORDER BY ts")
            ]
        return state

    # --- writes -----------------------------------------------------------
    def update(self, **fields: Any) -> None:
        """Set scalar fields in one transaction. Lists are NOT accepted here —
        appending is what makes this cheaper than the file, so callers use
        ``append_history`` / ``append_snapshot`` and cannot accidentally rewrite
        the whole record."""
        for name in ("history", "snapshots"):
            if name in fields:
                raise TypeError(
                    f"{name} is append-only: use append_{name.rstrip('s')}() "
                    f"rather than passing the whole list to update()"
                )
        with self._conn() as c:
            c.executemany(
                "INSERT INTO scalars (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, json.dumps(v, default=str)) for k, v in fields.items()],
            )

    def append_history(self, entry: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO history (entry) VALUES (?)",
                      (json.dumps(entry, default=str),))

    def append_snapshot(self, entry: dict[str, Any], *, keep: int) -> None:
        """Append a sample and prune to the newest ``keep``.

        Pruning is in the same transaction as the insert: a crash between them
        would otherwise leave the table growing without bound.
        """
        with self._conn() as c:
            c.execute("INSERT INTO snapshots (ts, entry) VALUES (?, ?)",
                      (float(entry["ts"]), json.dumps(entry, default=str)))
            c.execute(
                "DELETE FROM snapshots WHERE seq NOT IN "
                "(SELECT seq FROM snapshots ORDER BY ts DESC LIMIT ?)",
                (keep,),
            )

    def last_snapshot_ts(self) -> float | None:
        with self._conn() as c:
            row = c.execute("SELECT MAX(ts) FROM snapshots").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # --- migration --------------------------------------------------------
    def migrate_from_json(self, json_path: Path) -> bool:
        """Import a legacy ``.lp_state.<network>.json`` exactly once.

        Returns True when an import happened. Guarded on the database being
        EMPTY rather than on the file's existence, so re-running never
        duplicates history — and a half-finished import cannot double-count,
        because the whole thing is one transaction.
        """
        json_path = Path(json_path)
        if not json_path.is_file():
            return False
        with self._conn() as c:
            if c.execute("SELECT COUNT(*) FROM scalars").fetchone()[0]:
                return False
        try:
            legacy = json.loads(json_path.read_text())
        except (OSError, ValueError):
            # A corrupt legacy file is exactly the failure this store exists to
            # prevent. Refuse to import rather than silently starting fresh —
            # starting fresh is what loses token_id (B10).
            raise

        history = legacy.pop("history", []) or []
        snapshots = legacy.pop("snapshots", []) or []
        with self._conn() as c:
            c.executemany(
                "INSERT INTO scalars (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, json.dumps(v, default=str)) for k, v in legacy.items()],
            )
            c.executemany("INSERT INTO history (entry) VALUES (?)",
                          [(json.dumps(e, default=str),) for e in history])
            c.executemany(
                "INSERT INTO snapshots (ts, entry) VALUES (?, ?)",
                [(float(s.get("ts") or 0), json.dumps(s, default=str)) for s in snapshots],
            )
        return True
