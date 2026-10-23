"""Writable app DB (sessions + event log), separate from the read-only dataset DB (spec §0.2 R12)."""

import json
import os
import re
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
CREATE TABLE IF NOT EXISTS searches(text TEXT PRIMARY KEY, n INTEGER NOT NULL, last REAL NOT NULL);
CREATE TABLE IF NOT EXISTS live_cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS browser_sessions(sid TEXT PRIMARY KEY, bid TEXT NOT NULL, created REAL);
CREATE INDEX IF NOT EXISTS browser_sessions_bid ON browser_sessions(bid);
"""
KEEP_DAYS = 7


def ref_of(sid: str) -> str:
    """What the page sees for a past conversation: a hash of its session id, so the id itself (the session's key)
    never reaches page scripts."""
    import hashlib
    return hashlib.sha256(f"past|{sid}".encode()).hexdigest()[:16]
_PRIVATE = re.compile(r"[0-9۰-۹٠-٩]{5,}|@|https?:|www\.", re.I)   # phone/card numbers, emails, links never become suggestions


_D = "[0-9۰-۹٠-٩]"
_SCRUB = [
    (re.compile(r"sk-ant-[\w-]+|\b(?:sk|pk|rk)-[\w-]{16,}", re.I), "[کلید حذف شد]"),
    (re.compile(r"\bbearer\s+[\w.~+/=-]{8,}", re.I), "[کلید حذف شد]"),
    (re.compile(r"(?:x-api-key|api[_-]?key|token|password|secret)\s*[\"':=]+\s*[\"']?[^\s\"',}]{6,}", re.I), "[کلید حذف شد]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[ایمیل حذف شد]"),
    (re.compile(rf"(?<!{_D})(?:\+98|0098|[0۰])?[9۹](?:[\s-]?{_D}){{9}}(?!{_D})"), "[شماره حذف شد]"),   # mobile numbers
    (re.compile(rf"{_D}(?:[\s-]?{_D}){{10,}}"), "[شماره حذف شد]"),   # card, account or landline numbers (11+ digits)
]


def scrub(value):
    """Contacts and secrets out of anything kept on disk; prices (up to 10 digits, not shaped like a mobile number)
    stay readable."""
    if isinstance(value, str):
        if value.startswith("data:image/"):   # a photo's small preview, not text
            return value
        for pattern, repl in _SCRUB:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def searchable(text: str | None) -> str | None:
    """The text as the start screen may show it to other people, or None if it shouldn't be shown."""
    t = " ".join((text or "").split())
    return t if 2 <= len(t) <= 40 and not _PRIVATE.search(t) else None


class AppDB:
    """One connection shared across threads; every access goes through the lock."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("TOROB_APP_DB", "data/app.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            # Rollback journal, not WAL: WAL fails on mounted/network disks («disk I/O error» on a Docker volume);
            # writes are already serialised by the lock in this single process.
            self._con.execute("PRAGMA journal_mode=DELETE")
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
        if now - self._purged > 3600:   # a long-running container still forgets old sessions (README: 7 days)
            self.purge()

    def log_event(self, sid: str, event: str, data: dict) -> None:
        text = json.dumps(data, ensure_ascii=False)
        if event in ("user", "error"):   # what a shopper typed or a provider answered: no contacts or secrets on disk
            text = json.dumps(scrub(data), ensure_ascii=False)
        with self._lock:
            self._con.execute("INSERT INTO events(session_id, ts, event, data) VALUES (?,?,?,?)",
                              (sid, time.time(), event, text))

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
                "SELECT ts, data FROM events WHERE event = 'error' AND ts >= ? ORDER BY id DESC LIMIT 10", since)]
        pct = lambda p: done_ms[min(len(done_ms) - 1, int(p * len(done_ms)))] if done_ms else None
        outcomes: dict[str, int] = {}
        for c in calls:
            outcomes[c["outcome"]] = outcomes.get(c["outcome"], 0) + 1
        return {"sessions": sessions, "events": by_event, "turns": len(done_ms), "p50_ms": pct(0.5),
                "p95_ms": pct(0.95), "calls": len(calls), "call_outcomes": outcomes,
                "errors": [{"at": time.strftime("%m-%d %H:%M", time.localtime(ts)), "text": d.get("text"),
                            "detail": d.get("detail")} for ts, d in errors]}

    # --- chat history: the conversations of one browser (a private `bid` cookie), never anyone else's ----------

    def link(self, bid: str, sid: str) -> None:
        with self._lock:
            self._con.execute("INSERT OR IGNORE INTO browser_sessions(sid, bid, created) VALUES (?,?,?)",
                              (sid, bid, time.time()))

    def past(self, bid: str, exclude: str | None, limit: int = 6) -> list[dict]:
        """This browser's earlier conversations, newest first: a short ref (not the session id), the first thing
        the shopper said, and when."""
        with self._lock:
            rows = self._con.execute(
                "SELECT b.sid, s.updated FROM browser_sessions b JOIN sessions s ON s.id = b.sid "
                "WHERE b.bid = ? AND b.sid != ? ORDER BY s.updated DESC LIMIT ?", (bid, exclude or "", limit * 2)).fetchall()
            out = []
            for sid, updated in rows:
                first = self._con.execute("SELECT data FROM events WHERE session_id = ? AND event = 'user' ORDER BY id "
                                          "LIMIT 1", (sid,)).fetchone()
                text = (json.loads(first[0]).get("text") or "").strip() if first else ""
                if text:
                    out.append({"ref": ref_of(sid), "title": text[:80], "at": updated})
        return out[:limit]

    def owned(self, bid: str, ref: str) -> str | None:
        """The session id behind `ref`, only if it belongs to this browser."""
        with self._lock:
            sids = [r[0] for r in self._con.execute("SELECT sid FROM browser_sessions WHERE bid = ?", (bid,)).fetchall()]
        return next((s for s in sids if ref_of(s) == ref), None)

    def forget(self, bid: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM browser_sessions WHERE bid = ?", (bid,))

    def record_search(self, text: str | None) -> None:
        """A first message that found products: counted for the start screen's popular searches."""
        t = searchable(text)
        if t:
            with self._lock:
                self._con.execute("INSERT INTO searches(text, n, last) VALUES (?, 1, ?) "
                                  "ON CONFLICT(text) DO UPDATE SET n = n + 1, last = excluded.last", (t, time.time()))

    def popular_searches(self, limit: int = 3) -> list[str]:
        with self._lock:
            rows = self._con.execute("SELECT text FROM searches ORDER BY n DESC, last DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def live_get(self, key: str, max_age: float) -> dict | None:
        """A cached live-Torob answer no older than `max_age` seconds (torob/live.py)."""
        with self._lock:
            row = self._con.execute("SELECT value, fetched FROM live_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row and time.time() - row[1] <= max_age else None

    def live_put(self, key: str, value: dict) -> None:
        with self._lock:
            self._con.execute("INSERT INTO live_cache(key, value, fetched) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                              "SET value = excluded.value, fetched = excluded.fetched",
                              (key, json.dumps(value, ensure_ascii=False), time.time()))

    def purge(self, keep_days: int = KEEP_DAYS) -> None:
        self._purged = time.time()
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            self._con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._con.execute("DELETE FROM sessions WHERE updated < ?", (cutoff,))
            self._con.execute("DELETE FROM searches WHERE last < ?", (cutoff,))
            self._con.execute("DELETE FROM live_cache WHERE fetched < ?", (time.time() - 2 * 86400,))
            self._con.execute("DELETE FROM browser_sessions WHERE sid NOT IN (SELECT id FROM sessions)")
