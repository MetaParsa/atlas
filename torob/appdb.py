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
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""
KEEP_DAYS = 7


class AppDB:
    """One connection shared across threads; every access goes through the lock."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("TOROB_APP_DB", "data/app.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.executescript(SCHEMA)
        self.purge()

    def load(self, sid: str | None) -> tuple[str, SessionState]:
        """Existing session, or a fresh id + state that is only written on save (no row per anonymous request)."""
        if sid:
            with self._lock:
                row = self._con.execute("SELECT state FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row:
                return sid, SessionState.model_validate_json(row[0])
        return uuid.uuid4().hex, SessionState()

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
        with self._lock:
            rows = self._con.execute("SELECT id, event, data FROM events WHERE session_id = ? AND id > ? ORDER BY id",
                                     (sid, after)).fetchall()
        return [{"id": i, "event": e, "data": json.loads(d)} for i, e, d in rows]

    def stats(self, hours: int = 24) -> dict:
        """Numbers for /admin over the last `hours`: sessions, turns, latency, calls, errors. No user text."""
        since = time.time() - hours * 3600
        with self._lock:
            q = lambda sql, *a: self._con.execute(sql, a).fetchall()
            sessions = q("SELECT count(*) FROM sessions WHERE updated >= ?", since)[0][0]
            by_event = dict(q("SELECT event, count(*) FROM events WHERE ts >= ? GROUP BY 1", since))
            done_ms = sorted(json.loads(d).get("ms", 0) for (d,) in q(
                "SELECT data FROM events WHERE ts >= ? AND event = 'done'", since))
            calls = [json.loads(d)["result"] for (d,) in q(
                "SELECT data FROM events WHERE ts >= ? AND event = 'call_result'", since)]
            errors = [(ts, json.loads(d)) for ts, d in q(
                "SELECT ts, data FROM events WHERE event = 'error' ORDER BY id DESC LIMIT 10")]
        pct = lambda p: done_ms[min(len(done_ms) - 1, int(p * len(done_ms)))] if done_ms else None
        outcomes: dict[str, int] = {}
        for c in calls:
            outcomes[c["outcome"]] = outcomes.get(c["outcome"], 0) + 1
        return {"sessions": sessions, "events": by_event, "turns": len(done_ms), "p50_ms": pct(0.5),
                "p95_ms": pct(0.95), "calls": len(calls), "call_outcomes": outcomes,
                "errors": [{"at": time.strftime("%m-%d %H:%M", time.localtime(ts)), "text": d.get("text"),
                            "detail": d.get("detail")} for ts, d in errors]}

    def purge(self, keep_days: int = KEEP_DAYS) -> None:
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            self._con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._con.execute("DELETE FROM sessions WHERE updated < ?", (cutoff,))
