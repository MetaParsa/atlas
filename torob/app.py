"""Torob in-store shopping agent — web app. S0: health and areas only; chat/refine/call arrive in S1–S6."""

import sqlite3

from fastapi import FastAPI, HTTPException

from torob import store

app = FastAPI(title="Torob in-store agent")
_db: sqlite3.Connection | None = None
_db_sig: tuple[int, int] | None = None


def db() -> sqlite3.Connection:
    """Dataset connection, reopened whenever the DB file changes (e.g. a finished upload replaces a partial one)."""
    global _db, _db_sig
    sig = store.file_signature()
    if sig is None:
        raise FileNotFoundError(store.DB_PATH)
    if _db is None or sig != _db_sig:
        if _db is not None:
            _db.close()
        _db, _db_sig = store.connect(), sig
    return _db


def _reset() -> None:
    global _db, _db_sig
    if _db is not None:
        _db.close()
    _db = _db_sig = None


@app.get("/health")
def health() -> dict:
    try:
        m = store.meta(db())
        return {"status": "ok", "dataset": m.get("dataset_version"), "built_at": m.get("built_at")}
    except Exception as e:  # DB missing or still uploading: answer 200 so the host doesn't kill the container
        _reset()
        return {"status": "building", "detail": type(e).__name__}


@app.get("/api/areas")
def areas() -> list[dict]:
    try:
        return store.areas(db())
    except (FileNotFoundError, sqlite3.DatabaseError):
        _reset()
        raise HTTPException(503, "داده‌ها در حال آماده‌سازی…")
