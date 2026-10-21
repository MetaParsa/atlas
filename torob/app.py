"""Torob in-store shopping agent — web app. S0: health and areas only; chat/refine/call arrive in S1–S6."""

from fastapi import FastAPI

from torob import store

app = FastAPI(title="Torob in-store agent")
_db = None


def db():
    global _db
    if _db is None:
        _db = store.connect()
    return _db


@app.get("/health")
def health() -> dict:
    try:
        m = store.meta(db())
        return {"status": "ok", "dataset": m.get("dataset_version"), "built_at": m.get("built_at")}
    except Exception as e:  # DB missing or still uploading: answer 200 so the host doesn't kill the container
        return {"status": "building", "detail": type(e).__name__}


@app.get("/api/areas")
def areas() -> list[dict]:
    return store.areas(db())
