"""Torob in-store shopping agent — web app (one origin: UI, API and admin; spec D19)."""

import html
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient, from_env
from torob import boot, live as live_torob, store
from torob.agents import places, vision
from torob.appdb import AppDB
from torob.models import MAX_TEXT, Chip
from torob.router import conditions_event, rule_route, run_turn, say

app = FastAPI(title="Torob in-store agent")


@app.on_event("startup")
def _boot() -> None:
    boot.ensure_db_async()   # fallback only: builds torob.db on the disk if it's missing and DATASET_URL is set
    live()                   # live Torob prices: the router reads cached ones for the simulated call
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def _fake_script(role: str, messages: list[dict]) -> dict | str:
    """With LLM_ENABLED off: the router 'model' is the rule-based parser (same JSON shape, zero cost);
    other roles get a plain sentence, never raw JSON in the chat."""
    if role == "router":
        return rule_route(messages[-1]["content"]).model_dump(mode="json")
    if role == "vision":
        return "نامعلوم"   # without a model the photo can't be read; the chat asks for the name instead
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
live_limit = RateLimiter(40, 60)   # the page asks for live prices card by card; Torob itself is protected in live.py
tile_limit = RateLimiter(900, 60)  # a map view is ~30–60 tiles; panning around Tehran for a minute stays well under
map_limit = RateLimiter(30, 60)
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


MAX_IMAGE, MAX_THUMB = 2_000_000, 60_000   # data-URL lengths; the page sends ~300 KB and ~15 KB


class ChatIn(BaseModel):
    text: str | None = Field(None, max_length=MAX_TEXT)
    chip: Chip | None = None
    bubble: str | None = Field(None, max_length=200)   # what the page showed as the user's message for a tapped chip
    image: str | None = Field(None, max_length=MAX_IMAGE)   # a photo (data URL, ≤1024 px JPEG made by the page)
    thumb: str | None = Field(None, max_length=MAX_THUMB)   # its small preview, kept in the chat history


WEB = Path(__file__).parent / "web"
HISTORY_MAX = 400   # events replayed on reload (a long session keeps its latest part)


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
    for name in ("app.css", "app.js", "radish-sprites.js", "map.js"):
        v = hashlib.sha256((WEB / name).read_bytes()).hexdigest()[:10]
        page = page.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return page


@app.get("/", response_class=HTMLResponse)
def index():
    stamp = tuple((WEB / n).stat().st_mtime_ns for n in ("index.html", "app.css", "app.js", "radish-sprites.js", "map.js"))
    return HTMLResponse(_index_html(stamp), headers={"Cache-Control": "no-cache"})


PRIVATE = "private, no-store"   # per-visitor answers: never kept by the CDN (ArvanCloud) or any shared cache


@app.get("/api/state")
def session_state(response: Response, sid: str | None = Cookie(default=None, max_length=64)) -> dict:
    """What the strip needs on page load (no turn, no LLM)."""
    response.headers["Cache-Control"] = PRIVATE
    _, state = appdb().load(sid)
    return {"conditions": conditions_event(state), "has_results": state.last_block_kind != "none",
            "plan": state.plan}


@lru_cache(maxsize=1)
def live() -> live_torob.LiveTorob:
    lv = live_torob.LiveTorob(appdb())
    live_torob.CURRENT = lv   # the router reads cached live prices for the simulated call from here
    return lv


def _product_title(product_id: str) -> str:
    if len(product_id) > 64:
        raise HTTPException(422, "شناسه‌ی نامعتبر")
    row = store.get().execute("SELECT title FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "این کالا در دیتاست نیست")
    return row["title"]


@app.get("/api/live/product/{product_id}")
def live_product(product_id: str, request: Request) -> dict:
    """Today's lowest price for a dataset product on torob.com (cached, polite; see torob/live.py)."""
    if not live_limit.allow(client_ip(request)):
        return {"status": "busy"}
    return live().product(product_id, _product_title(product_id))


@app.get("/api/live/shops/{product_id}")
def live_shops(product_id: str, request: Request, ids: str = "") -> dict:
    """Live in-person prices in Tehran for a dataset product, matched to the shown dataset shops by location."""
    if not live_limit.allow(client_ip(request)):
        return {"status": "busy"}
    title = _product_title(product_id)
    shop_ids = [i for i in ids.split(",") if i][:30]
    if len(ids) > 2000 or any(len(i) > 64 for i in shop_ids):
        raise HTTPException(422, "شناسه‌ی نامعتبر")
    rows = store.get().execute(f"SELECT shop_id, lat, lon FROM shops WHERE shop_id IN ({','.join('?' * len(shop_ids))})",
                               shop_ids).fetchall() if shop_ids else []
    return live().shops(product_id, title, [dict(r) for r in rows if r["lat"] is not None])


# --- maps: OpenStreetMap tiles through this server (cached on disk, Tehran only), and the map data ---------------
# OSM's own tiles carry Persian street names and need no key (Carto's now return an «API key required» watermark).
# The page softens them, and inverts them for dark mode, with a CSS filter.

TILE_SOURCES = {"osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png"}
TILE_Z = (10, 19)
TILE_DAILY_FETCHES = int(os.environ.get("TILE_DAILY_FETCHES", "20000"))   # new tiles asked of OSM per day, whole server
_tile_day = {"day": "", "n": 0}
TEHRAN_BOX = (35.45, 35.95, 50.95, 51.85)   # lat_min, lat_max, lon_min, lon_max


def _tile_range(z: int) -> tuple[int, int, int, int]:
    import math
    n = 2 ** z
    def tx(lon): return int((lon + 180) / 360 * n)
    def ty(lat):
        r = math.radians(lat)
        return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return tx(TEHRAN_BOX[2]), tx(TEHRAN_BOX[3]), ty(TEHRAN_BOX[1]), ty(TEHRAN_BOX[0])


@lru_cache(maxsize=1)
def _tile_http():
    import httpx
    return httpx.Client(timeout=8, headers={"User-Agent": "dastyar-kharid-hozoori/1.0 (hackathon demo)"})


@app.get("/tiles/{style}/{z}/{x}/{y}.png")
def tile(style: str, z: int, x: int, y: int, request: Request):
    """One map tile. Only Tehran and zoom 10–19 (this is not a general tile server); each tile is fetched once, kept
    on the data disk and served with a long cache so the CDN keeps it too. Shoppers' browsers only talk to us."""
    if style not in TILE_SOURCES or not TILE_Z[0] <= z <= TILE_Z[1]:
        raise HTTPException(404)
    x0, x1, y0, y1 = _tile_range(z)
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        raise HTTPException(404)
    path = appdb().path.parent / "tiles" / style / str(z) / str(x) / f"{y}.png"
    headers = {"Cache-Control": "public, max-age=2592000, immutable"}
    if path.exists():
        return FileResponse(path, media_type="image/png", headers=headers)
    today = time.strftime("%Y-%m-%d")
    if _tile_day["day"] != today:
        _tile_day.update(day=today, n=0)
    if not tile_limit.allow(client_ip(request)) or _tile_day["n"] >= TILE_DAILY_FETCHES:   # OSM's tile policy: no bulk use
        raise HTTPException(429)
    _tile_day["n"] += 1
    url = TILE_SOURCES[style].format(z=z, x=x, y=y)
    try:
        resp = _tile_http().get(url)
    except Exception:
        raise HTTPException(502)
    if resp.status_code != 200 or not resp.content.startswith(b"\x89PNG"):
        raise HTTPException(502)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return Response(resp.content, media_type="image/png", headers=headers)


@app.get("/api/map/product/{product_id}")
def map_product(product_id: str, request: Request, response: Response,
                sid: str | None = Cookie(default=None, max_length=64)) -> dict:
    """Every in-person seller of the selected product for the full map, planned against this session's conditions."""
    response.headers["Cache-Control"] = PRIVATE
    if not map_limit.allow(client_ip(request)):
        raise HTTPException(429, "کمی صبر کن")
    _product_title(product_id)
    _, state = appdb().load(sid)
    if product_id != state.selected_product_id and product_id not in state.product_ids:
        raise HTTPException(404, "این کالا الان روی صفحه نیست")
    return places.product_map(product_id, state.conditions, [s["shop_id"] for s in state.shops_shown]
                              if product_id == state.selected_product_id else [])


@app.get("/api/map/category")
def map_category(request: Request, c: list[str] = Query(default=[], max_length=2)) -> dict:
    """Sellers of a category as dots, and its hubs: the «بورس» map. Same for everyone (no session)."""
    if not map_limit.allow(client_ip(request)):
        raise HTTPException(429, "کمی صبر کن")
    cats = tuple(dict.fromkeys(x for x in c if 0 < len(x) <= 120))
    con = store.get()
    if not cats or any(con.execute("SELECT 1 FROM products WHERE category = ? LIMIT 1", (x,)).fetchone() is None
                       for x in cats):
        raise HTTPException(404, "این دسته در دیتاست نیست")
    from torob.models import Conditions
    data = places.category_map(cats, Conditions())
    data.pop("center", None)
    return data


@app.get("/api/popular")
def popular() -> dict:
    """The start screen's suggestions: the searches people typed most (only ones that found products)."""
    return {"searches": appdb().popular_searches()}


@app.get("/api/history")
def history(response: Response, sid: str | None = Cookie(default=None, max_length=64)) -> dict:
    """This session's conversation (only the caller's own cookie), so a reload shows the chat again."""
    response.headers["Cache-Control"] = PRIVATE
    if not sid:
        return {"events": []}
    return {"events": [{"event": e["event"], "data": e["data"]} for e in appdb().events(sid)[-HISTORY_MAX:]]}


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
                        ("مدل", "، ".join(sorted(set(c.models.values())))),
                        ("توکن مصرف‌شده امروز", fmt(c.guard.used_today())), ("سقف روزانه", fmt(c.guard.daily_tokens)),
                        ("سقف هر نشست", fmt(c.guard.session_tokens)),
                        ("آخرین فراخوانی‌ها", "، ".join(f"{x['role']}:{x['tokens']}" for x in c.log[-5:]) or "—")]),
        ("۲۴ ساعت اخیر", [("نشست", st["sessions"]), ("نوبت", st["turns"]),
                          ("زمان پاسخ سرور p50 / p95 (ms، بدون مکث نمایشی تماس)", f"{fmt(st['p50_ms'])} / {fmt(st['p95_ms'])}"),
                          ("تماس شبیه‌سازی‌شده", st["calls"]),
                          *[(outcomes.get(k, k), v) for k, v in st["call_outcomes"].items()]]),
        ("قیمت زنده از ترب", [("درخواست امروز به ترب", f"{fmt(live().polite.used_today)} از {fmt(live().polite.cap)}"),
                              ("فعال", "بله" if live().enabled else "خیر")]),
        ("از آخرین راه‌اندازی", [("درخواست ردشده (محدودیت نرخ)", chat_limit.rejected + live_limit.rejected)]),
    ]
    errs = "".join(f"<tr><td>{e(x['at'])}</td><td>{e(x['text'] or '')}</td><td>{e(x['detail'] or '')}</td></tr>"
                   for x in st["errors"]) or "<tr><td colspan=3>خطایی ثبت نشده</td></tr>"
    from torob.router import ROUTER_FALLBACKS
    errs += "".join(f"<tr><td>{e(x['at'])}</td><td>فهم پیام به قواعد برگشت</td><td>{e(x['error'].split(':')[0])}</td></tr>"
                    for x in list(ROUTER_FALLBACKS)[::-1])   # the kind of failure only: never what a shopper typed
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
    image = body.image if vision.is_image(body.image, MAX_IMAGE) else None
    if not image and not (body.text or "").strip() and not body.chip:
        return JSONResponse({"error": "یه چیزی بنویس یا یه عکس بفرست."}, status_code=422)
    thumb = body.thumb if image and vision.is_image(body.thumb, MAX_THUMB) else None
    return _turn_response(request, sid, text=(body.text or "").strip() or None, chip=body.chip, bubble=body.bubble,
                          image=image, thumb=thumb)


@app.post("/api/refine")
def refine(body: RefineIn, request: Request, sid: str | None = Cookie(default=None, max_length=64)):
    """The strip re-runs the latest block without the LLM (R15)."""
    chip = Chip(label="شرایط", kind="direct_call", tool="set_conditions", args=body.conditions)
    return _turn_response(request, sid, chip=chip)


def same_origin(request: Request) -> bool:
    """A browser sends Origin on every POST: another website can't drive a visitor's session (and spend its tokens).
    No Origin (curl, server-to-server) is allowed; the cookie still ties a turn to its own session."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    from urllib.parse import urlsplit
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return urlsplit(origin).netloc.lower() == host.split(",")[0].strip().lower()


def _turn_response(request: Request, sid: str | None, text: str | None = None, chip: Chip | None = None,
                   bubble: str | None = None, image: str | None = None, thumb: str | None = None):
    if not same_origin(request):
        return JSONResponse({"error": "این درخواست از سایت دیگری آمده."}, status_code=403)
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
    said = (text if text is not None else bubble or "").strip()
    if said or image:   # the user's side of the chat, for /api/history (a photo keeps only its small preview)
        adb.log_event(sid, "user", {"text": said, **({"image": thumb} if thumb else {})})
    first_search = text is not None and state.last_block_kind == "none"

    delay = float(os.environ.get("CALL_LINE_DELAY", "0.7"))

    def stream():
        try:
            query = text
            if image:   # a photo becomes a search query; an unreadable one asks for the name instead of guessing
                yield em.emit("status", {"text": "دارم عکس رو نگاه می‌کنم…", "done": False})
                seen = vision.identify(llm(), image, sid)
                if seen:   # what the photo was read as stays in the chat (and its history), so a misreading is visible
                    yield say(state, em, f"تو عکس «{seen}» رو می‌بینم؛ دنبالش می‌گردم.")
                else:
                    yield em.emit("status", {"text": "عکس رو نشناختم", "done": True})
                if seen:
                    query = f"{text} {seen}" if text else seen
                elif not text:
                    yield em.emit("message", {"text": "از عکس نفهمیدم چه کالاییه؛ اسم یا مدلش رو بنویس تا پیداش کنم."})
                    yield em.emit("done", {})
                    return
            for chunk in run_turn(llm(), state, em, text=query, chip=chip):
                yield chunk
                if delay and chunk.startswith("event: call_line"):
                    time.sleep(delay)   # the call reads like a call; the facts are already decided
                    em.paused += delay  # …and the pacing isn't counted as response time on /admin
        finally:
            adb.save(sid, state)   # also when the client disconnects mid-stream
            if first_search and any(e == "product_cards" and d.get("items") for e, d in em.events):
                adb.record_search(text)
            with _busy_lock:
                _busy.pop(sid, None)

    resp = StreamingResponse(stream(), media_type="text/event-stream", headers=STREAM_HEADERS)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", secure=is_https(request), max_age=7 * 24 * 3600)
    return resp
