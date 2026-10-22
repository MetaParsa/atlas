"""Torob in-store shopping agent — web app (one origin: UI, API and admin; spec D19)."""

import html
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient, from_env
from torob import boot, store
from torob.appdb import AppDB
from torob.models import MAX_TEXT, Chip
from torob.router import conditions_event, rule_route, run_turn

app = FastAPI(title="Torob in-store agent")


@app.on_event("startup")
def _boot() -> None:
    boot.ensure_db_async()   # fallback only: builds torob.db on the disk if it's missing and DATASET_URL is set
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def _fake_script(role: str, messages: list[dict]) -> dict | str:
    """With LLM_ENABLED off: the router 'model' is the rule-based parser (same JSON shape, zero cost);
    other roles get a plain sentence, never raw JSON in the chat."""
    if role == "router":
        return rule_route(messages[-1]["content"]).model_dump(mode="json")
    return "در حالت آزمایشی این جواب ساده است؛ از کارت‌ها و دکمه‌ها استفاده کن."


@lru_cache(maxsize=1)
def llm() -> LLMClient:
    return from_env(fake=FakeLLM(_fake_script))


@lru_cache(maxsize=1)
def appdb() -> AppDB:
    return AppDB()


# --- abuse limits (spec §8: 20 req/min/IP; one running turn per session) ---------------------------------------

class RateLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    rejected = 0   # shown on /admin

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                self.rejected += 1
                return False
            q.append(now)
            if len(self._hits) > 50_000:
                self._hits.clear()
            return True


chat_limit = RateLimiter(20, 60)
_busy: dict[str, float] = {}     # sid -> turn start; expires so a stream that never started can't lock a session
_busy_lock = threading.Lock()
BUSY_TTL_S = 60


def client_ip(request: Request) -> str:
    """Behind the host's proxy (TRUST_PROXY=1) the last X-Forwarded-For hop is the one the proxy added; the first
    entries are client-supplied and spoofable. Without a trusted proxy, use the connection address."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd and os.environ.get("TRUST_PROXY") == "1":
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


# --- routes ------------------------------------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    try:
        m = store.meta(store.get())
        return {"status": "ok", "dataset": m.get("dataset_version"), "built_at": m.get("built_at"),
                "llm_enabled": llm().enabled}
    except Exception as e:  # DB missing or still uploading: answer 200 so the host doesn't kill the container
        store.reset()
        return {"status": "building", "detail": type(e).__name__, "boot": boot.status}


@app.get("/api/areas")
def areas() -> list[dict]:
    try:
        return store.areas(store.get())
    except (FileNotFoundError, sqlite3.DatabaseError):
        store.reset()
        raise HTTPException(503, "داده‌ها در حال آماده‌سازی…")


class ChatIn(BaseModel):
    text: str | None = Field(None, max_length=MAX_TEXT)
    chip: Chip | None = None


WEB = Path(__file__).parent / "web"


class _Static(StaticFiles):
    """CSS/JS are revalidated on every load (cheap 304 via ETag); the font is immutable.
    Without a Cache-Control header, browsers and the CDN kept a stale app.css after a deploy."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "public, max-age=31536000, immutable" if path.replace("\\", "/").startswith("fonts/") else "no-cache"
        return r


app.mount("/static", _Static(directory=WEB), name="static")


@lru_cache(maxsize=1)
def _index_html(stamp: tuple) -> str:
    """index.html with ?v=<content hash> on app.css/app.js, so a new deploy is a new URL for every cache."""
    import hashlib
    page = (WEB / "index.html").read_text(encoding="utf-8")
    for name in ("app.css", "app.js"):
        v = hashlib.sha256((WEB / name).read_bytes()).hexdigest()[:10]
        page = page.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return page


@app.get("/", response_class=HTMLResponse)
def index():
    stamp = tuple((WEB / n).stat().st_mtime_ns for n in ("index.html", "app.css", "app.js"))
    return HTMLResponse(_index_html(stamp), headers={"Cache-Control": "no-cache"})


@app.get("/api/state")
def session_state(sid: str | None = Cookie(default=None, max_length=64)) -> dict:
    """What the strip needs on page load (no turn, no LLM)."""
    _, state = appdb().load(sid)
    return {"conditions": conditions_event(state), "has_results": state.last_block_kind != "none",
            "plan": state.plan}


@app.post("/api/reset")
def reset(sid: str | None = Cookie(default=None, max_length=64)) -> JSONResponse:
    """«جدید»: forget this session's search (a new session id)."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sid")
    return resp


@app.get("/admin", response_class=HTMLResponse)
def admin() -> HTMLResponse:
    """«پشت صحنه» for judges: read-only numbers, no user text, no keys. Server-rendered, every value escaped."""
    e = html.escape
    try:
        m = store.meta(store.get())
    except Exception:
        m = {}
    st = appdb().stats()
    c = llm()
    fmt = lambda v: "—" if v is None else f"{v:,}"
    rows = lambda items: "".join(f"<tr><th>{e(str(k))}</th><td>{e(str(v))}</td></tr>" for k, v in items)
    outcomes = {"ok": "تأیید (شبیه‌سازی)", "unavailable": "ناموجود (شبیه‌سازی)", "no_answer": "جواب نداد (شبیه‌سازی)"}
    sections = [
        ("دیتاست", [("نسخه", m.get("dataset_version", "—")), ("ساخته‌شده", m.get("built_at", "—")),
                    ("خروجی ترب", m.get("exported_at", "—")), ("فروشگاه", fmt(int(m.get("rows_shops", 0) or 0))),
                    ("محصول", fmt(int(m.get("rows_products", 0) or 0))), ("پیشنهاد (offer)", fmt(int(m.get("rows_offers", 0) or 0))),
                    ("قیمت مشکوک کنار گذاشته", fmt(int(m.get("flagged_offers", 0) or 0)))]),
        ("هوش مصنوعی", [("LLM فعال", "بله" if c.enabled else "خیر (قواعد + FakeLLM؛ بدون هزینه)"),
                        ("توکن مصرف‌شده امروز", fmt(c.guard.used_today())), ("سقف روزانه", fmt(c.guard.daily_tokens)),
                        ("سقف هر نشست", fmt(c.guard.session_tokens)),
                        ("آخرین فراخوانی‌ها", "، ".join(f"{x['role']}:{x['tokens']}" for x in c.log[-5:]) or "—")]),
        ("۲۴ ساعت اخیر", [("نشست", st["sessions"]), ("نوبت", st["turns"]),
                          ("زمان پاسخ سرور p50 / p95 (ms، بدون مکث نمایشی تماس)", f"{fmt(st['p50_ms'])} / {fmt(st['p95_ms'])}"),
                          ("تماس شبیه‌سازی‌شده", st["calls"]),
                          *[(outcomes.get(k, k), v) for k, v in st["call_outcomes"].items()]]),
        ("از آخرین راه‌اندازی", [("درخواست ردشده (محدودیت نرخ)", chat_limit.rejected)]),
    ]
    errs = "".join(f"<tr><td>{e(x['at'])}</td><td>{e(x['text'] or '')}</td><td>{e(x['detail'] or '')}</td></tr>"
                   for x in st["errors"]) or "<tr><td colspan=3>خطایی ثبت نشده</td></tr>"
    body = "".join(f"<h2>{e(t)}</h2><table>{rows(items)}</table>" for t, items in sections)
    page = f"""<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>پشت صحنه</title>
<style>body{{font-family:Vazirmatn,Tahoma,sans-serif;max-width:640px;margin:auto;padding:16px;background:#f6f7f8;color:#1d2330}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:20px;color:#0f766e}}table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{text-align:start;padding:6px 8px;border-bottom:1px solid #e3e6ea;font-size:14px;vertical-align:top}}th{{width:55%;font-weight:500;color:#5f6878}}
p{{font-size:13px;color:#5f6878}}</style>
<h1>پشت صحنه</h1>
<p>عددها از دیتاست ترب و لاگ رویدادهای همین سرورند. تماس‌ها شبیه‌سازی است (شماره‌های دیتاست ساختگی‌اند).</p>
{body}<h2>آخرین خطاها</h2><table>{errs}</table><p><a href="/">بازگشت به دستیار</a></p></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


class RefineIn(BaseModel):
    """A conditions-strip change; same keys as the set_conditions tool (validated there)."""
    conditions: dict = Field(default_factory=dict)


@app.post("/api/chat")
def chat(body: ChatIn, request: Request, sid: str | None = Cookie(default=None, max_length=64)):
    return _turn_response(request, sid, text=body.text, chip=body.chip)


@app.post("/api/refine")
def refine(body: RefineIn, request: Request, sid: str | None = Cookie(default=None, max_length=64)):
    """The strip re-runs the latest block without the LLM (R15)."""
    chip = Chip(label="شرایط", kind="direct_call", tool="set_conditions", args=body.conditions)
    return _turn_response(request, sid, chip=chip)


def _turn_response(request: Request, sid: str | None, text: str | None = None, chip: Chip | None = None):
    if not chat_limit.allow(client_ip(request)):
        return JSONResponse({"error": "یه کم آروم‌تر؛ چند لحظه بعد دوباره امتحان کن."}, status_code=429)
    adb = appdb()
    sid, state = adb.load(sid)
    with _busy_lock:
        started = _busy.get(sid)
        if started and time.monotonic() - started < BUSY_TTL_S:   # double tap / chip during a stream
            return JSONResponse({"error": "صبر کن جواب قبلی تموم بشه."}, status_code=409)
        _busy[sid] = time.monotonic()
    em = Emitter(sid, sink=adb.log_event)

    delay = float(os.environ.get("CALL_LINE_DELAY", "0.7"))

    def stream():
        try:
            for chunk in run_turn(llm(), state, em, text=text, chip=chip):
                yield chunk
                if delay and chunk.startswith("event: call_line"):
                    time.sleep(delay)   # the call reads like a call; the facts are already decided
                    em.paused += delay  # …and the pacing isn't counted as response time on /admin
        finally:
            adb.save(sid, state)   # also when the client disconnects mid-stream
            with _busy_lock:
                _busy.pop(sid, None)

    resp = StreamingResponse(stream(), media_type="text/event-stream", headers=STREAM_HEADERS)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", secure=is_https(request), max_age=7 * 24 * 3600)
    return resp
