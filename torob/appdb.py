"""Writable app DB (sessions + event log), separate from the read-only dataset DB (spec §0.2 R12)."""

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from torob.models import SessionState

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, state TEXT NOT NULL, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL, event TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
"""


class AppDB:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("TOROB_APP_DB", "data/app.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        self._lock = threading.Lock()

    def new_session(self) -> tuple[str, SessionState]:
        sid, state = uuid.uuid4().hex, SessionState()
        self.save(sid, state)
        return sid, state

    def load(self, sid: str | None) -> tuple[str, SessionState]:
        if sid:
            row = self._con.execute("SELECT state FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row:
                return sid, SessionState.model_validate_json(row[0])
        return self.new_session()

    def save(self, sid: str, state: SessionState) -> None:
        now = time.time()
        with self._lock:
            self._con.execute(
                "INSERT INTO sessions(id, state, created, updated) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state, updated = excluded.updated",
                (sid, state.model_dump_json(), now, now))

    def log_event(self, sid: str, event: str, data: dict) -> None:
        with self._lock:
            self._con.execute("INSERT INTO events(session_id, ts, event, data) VALUES (?,?,?,?)",
                              (sid, time.time(), event, json.dumps(data, ensure_ascii=False)))

    def events(self, sid: str, after: int = 0) -> list[dict]:
        rows = self._con.execute("SELECT id, event, data FROM events WHERE session_id = ? AND id > ? ORDER BY id",
                                 (sid, after)).fetchall()
        return [{"id": i, "event": e, "data": json.loads(d)} for i, e, d in rows]
