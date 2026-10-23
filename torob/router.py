"""Step machine (spec §0.2 R10): one JSON LLM call per free-text turn, then code decides what runs.

Chips with kind=direct_call skip the LLM entirely; their args are validated per tool. `rule_route` parses simple
Persian requests without any model; it is FakeLLM's script in development and the fallback when a real model
returns unusable JSON or the budget is spent.
"""

import re
from concurrent.futures import ThreadPoolExecutor
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator
from typing import Literal
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from core.agent_loop import Emitter, plain_text
from core.llm import LLMClient, LLMError
from core.text import format_million, format_price_short, format_toman, normalize_fa, to_fa_digits, to_latin_digits
from torob import neshan, search
from torob import store
from torob import clock
from torob.agents import advisor
from torob.agents import call as call_engine
from torob.agents import compose as composer
from torob.agents import places
from torob.agents import filters as filter_builder
from torob.agents import followup
from torob.agents import relevance
from torob import live as live_torob
from torob.agents import shops as shops_agent
from torob.logic import hours as H
from torob.logic.geo import haversine_m
from torob.models import DEFAULT_AREA, DEFAULT_CENTER, Chip, Conditions, Location, Need, RouterOutput, SessionState

ROUTER_PROMPT = (Path(__file__).parent / "prompts" / "router.md").read_text(encoding="utf-8")

_NUM = r"(\d+(?:[.,]\d+)?)"
_BUDGET = re.compile(rf"(?:تا|زیر|حداکثر|کمتر از|نهایتا|نهایت)\s*{_NUM}\s*(میلیون|م(?![ء-ی])|هزار|تومن|تومان)?")
_TIME_WORDS = re.compile(r"^\s*(?:شب|صبح|عصر|ظهر|بعد ?از ?ظهر|ساعت|دقیقه|روز)")
_CREDIT = re.compile(r"قسط|اقساط|اعتباری|ترب ?پی|اسنپ ?پی|دیجی ?پی")
_CALL = re.compile(r"بپرس|زنگ بزن|تماس بگیر")
_HUB = re.compile(r"بورس|راسته|مرکز (?:خرید|فروش)|کجا (?:بخرم|برم|بگیرم)")
_ORDINAL_INDEX = {"اولی": 0, "اولین": 0, "اول": 0, "دومی": 1, "دومین": 1, "دوم": 1, "سومی": 2, "سومین": 2, "سوم": 2,
                  "چهارمی": 3, "چهارم": 3, "پنجمی": 4, "پنجم": 4}
# Whole words only: «دوم» must not match inside «کدومشون».
_ORDINAL = re.compile(r"(?<![ء-ی])(" + "|".join(sorted(_ORDINAL_INDEX, key=len, reverse=True))
                      + r")(?![ء-ی])")
_ASK = re.compile(r"کدوم|کدام|کدومش|کدومشون|کجاش|زودتر|دیرتر|کی می ?بنده|تا کی|ارزون ?تر|ارزانتر|نزدیک ?تر")
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_STOP = set("یه یک می خوام میخوام خواستم برای با از که رو را و تا زیر میلیون تومن تومان هزار دارم لازم دنبال ای هم "
            "خوب یدونه یکی چی چیه هست باشه بشه قسطی اقساطی حداکثر نهایتا".split())


def _budget(t: str) -> int | None:
    """«تا ۳ میلیون» → 3M, «تا ۵۰۰ هزار» → 500k, «زیر ۳۰۰ تومن» → 300k and «تا ۲ تومن» → 2M (Tehran slang: a few
    «تومن» are millions, hundreds are thousands), «تا ۲۵۰۰۰۰۰ تومان» → 2.5M. A bare small number counts as millions;
    «تا ۸ شب» is a time, not a budget."""
    for m in _BUDGET.finditer(t):
        n, unit = float(m.group(1).replace(",", "")), m.group(2) or ""
        if not unit and _TIME_WORDS.match(t[m.end():]):
            continue
        if unit.startswith("م"):
            return int(n * 1_000_000)
        if unit == "هزار":
            return int(n * 1_000)
        if unit in ("تومن", "تومان"):
            return int(n) if n >= 10_000 else int(n * 1_000) if n >= 50 else int(n * 1_000_000)
        return int(n) if n >= 10_000 else int(n * 1_000_000)
    return None


_ARRIVE = re.compile(r"(?:ساعت\s*)?(\d{1,2})(?:[:.](\d{2})|\s*و\s*نیم)?\s*(صبح|ظهر|عصر|شب|بعد ?از ?ظهر)?\s*"
                     r"(?:میام|می ?رسم|اونجام|میرم|برسم|بیام)")


def _arrival(t: str) -> str | None:
    """«ساعت ۸:۳۰ میام» / «۸ و نیم شب می‌رسم» → "HH:MM". A small hour already past today means the evening
    (at 18:05, «۸:۳۰» = 20:30)."""
    m = _ARRIVE.search(t)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2)) if m.group(2) else (30 if "نیم" in m.group(0) else 0)
    part = m.group(3) or ""
    if part in ("عصر", "شب") or part.startswith("بعد"):
        h = h + 12 if h < 12 else h
    elif not part and h < 12 and clock.now().hour >= h:
        h += 12
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def rule_route(text: str, allow_ask: bool = True) -> RouterOutput:
    """No-model understanding of the common cases. Deliberately simple; never invents numbers."""
    t = to_latin_digits(normalize_fa(text))
    patch: dict = {}
    arrive = _arrival(t)
    if arrive:   # «ساعت ۸:۳۰ میام» is a visit condition, not a search for clocks
        return RouterOutput(intent="refine", conditions_patch={"arrive_at": arrive})
    budget = _budget(t)
    if budget:
        patch["budget_max"] = budget
    if _CREDIT.search(t):
        patch["payment"] = "credit"
    if allow_ask and re.search(r"کدوم|کدام", t) and _ASK.search(t) and not _ORDINAL.search(t):
        return RouterOutput(intent="ask_results", conditions_patch=patch)   # «بپرس ببین کدومشون قسطی دارن»
    hub = bool(_HUB.search(t))
    if _CALL.search(t) and not hub:
        m = _ORDINAL.search(t)
        return RouterOutput(intent="call_shop", conditions_patch=patch, target=m.group(1) if m else "")
    if allow_ask and _ASK.search(t) and not budget:
        return RouterOutput(intent="ask_results", conditions_patch=patch)
    # Numbers outside the budget phrase are specs («۲۰ وات», «۲۰۰۰۰», «۲۵ آمپر») and stay in the query.
    words = [w for w in re.sub(r"[^\w\s]", " ", _BUDGET.sub(" ", t)).split() if w not in _STOP]
    if words and all(w.isdigit() for w in words):
        words = []
    if not words:
        return RouterOutput(intent="refine" if patch else "other", conditions_patch=patch)
    if hub:   # «بورس هندزفری کجاست» → the item is what's left after the hub words
        words = [w for w in words if not _HUB.fullmatch(w) and w not in ("کجاست", "کجا", "کجاس", "مرکز", "خرید")]
        if not words:
            return RouterOutput(intent="hub")
    need = Need(product_type=" ".join(words[:3]), keywords=[" ".join(words)], budget_max=budget)
    return RouterOutput(intent="hub" if hub else "need", need=need, conditions_patch=patch)


ROUTER_FALLBACKS: deque = deque(maxlen=20)   # why the model's reading wasn't used (shown on /admin)

# Structured outputs: the router's reply always matches this, so small talk can't come back as prose. Every field is
# required and nullable (the safe subset); conditions are a list of explicit changes, so «unchanged» and «set to
# don't-care (null)» never get confused.
_S, _N = {"type": "string"}, {"type": "null"}
_ARR = {"type": "array", "items": _S}
_null = lambda s: {"anyOf": [s, _N]}
_NEED_FIELDS = {"product_type": _S, "keywords": _ARR, "category_hints": _ARR, "budget_min": _null({"type": "integer"}),
                "budget_max": _null({"type": "integer"}), "use_case": _null(_S), "must_have": _ARR, "brand_pref": _ARR,
                "named_product": _null(_S), "missing_info": _ARR,
                "price_pref": {"type": "string", "enum": ["cheap", "any"]}}
ROUTER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["intent", "need", "conditions_patch", "target", "message", "question"],
    "properties": {
        "intent": {"type": "string", "enum": ["need", "named_product", "refine", "call_shop", "ask_results", "hub",
                                              "show_shops", "other"]},
        "need": _null({"type": "object", "additionalProperties": False, "required": list(_NEED_FIELDS), "properties": _NEED_FIELDS}),
        "conditions_patch": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "value"],
            "properties": {"key": {"type": "string", "enum": ["radius_km", "budget_min", "budget_max", "payment",
                                                             "fast_delivery", "open", "arrive_at", "area"]},
                           "value": _null(_S)}}},
        "target": _S, "message": _S,
        "question": _null({"type": "object", "additionalProperties": False, "required": ["text", "options", "wait"],
                           "properties": {"text": _S, "options": _ARR, "wait": {"type": "boolean"}}}),
    },
}


def _patch_from_pairs(v) -> dict:
    """[{"key": "budget_max", "value": "3000000"}, …] → {"budget_max": 3000000}; a dict (rules, older replies) passes."""
    if isinstance(v, dict) or not v:
        return v or {}
    out = {}
    for p in v:
        k, val = (p or {}).get("key"), (p or {}).get("value")
        try:
            if val is None or str(val).strip().lower() in ("", "null", "none"):
                out[k] = None
            elif k == "radius_km":
                out[k] = float(to_latin_digits(str(val)))
            elif k in ("budget_min", "budget_max"):
                out[k] = int(float(to_latin_digits(str(val))))
            elif k == "fast_delivery":
                out[k] = str(val).strip().lower() in ("true", "1", "yes", "بله")
            else:
                out[k] = val
        except ValueError:
            continue   # an unreadable value changes nothing (apply_patch validates the rest)
    return out


def _router_output(data: dict) -> RouterOutput:
    if isinstance(data, dict) and "conditions_patch" in data:
        data = {**data, "conditions_patch": _patch_from_pairs(data["conditions_patch"])}
    return RouterOutput.model_validate(data)


def place_facts(state: SessionState) -> dict:
    """Where the shopper is and how they get around: every model step gets this with the cards, so «نزدیکه؟»,
    «کجاست؟», «کدوم نزدیک‌تره؟» are answered from data, never with «فاصله ثبت نشده»."""
    c, now = state.conditions, clock.now()
    return {"shopper_at": c.center.label,
            "shopper_location_known": c.center.source != "default",   # False: distances are from the city-centre default
            "radius_km": c.radius_km or "کل تهران", "travel": "پیاده" if c.mode == "walk" else "ماشین",
            "now": f"{shops_agent.WEEKDAYS[H.iran_weekday(now.date())]} {now.strftime('%H:%M')}", "arrive_at": c.arrive_at.strftime("%H:%M") if c.arrive_at else "الان + زمان رفتن",
            "known_for_every_shop": "مختصات، پاساژ/طبقه/پلاک، فاصله و دقیقه تا خریدار، ساعت کاری و باز بودن موقع رسیدن"}


def _place_line(state: SessionState) -> str:
    f = place_facts(state)
    who = (f"خریدار در «{f['shopper_at']}» است" if f["shopper_location_known"]
           else f"مکان خریدار را هنوز نمی‌دانیم؛ فاصله‌ها از مرکز پیش‌فرض «{f['shopper_at']}» حساب می‌شود")
    return (f"مکان: {who}؛ محدوده {f['radius_km']} کیلومتر، رفت‌وآمد {f['travel']}. برای هر فروشگاه "
            f"{f['known_for_every_shop']} را داریم، و برای هر کارت محصول دقیقه‌ی نزدیک‌ترین فروشنده را.")


def _on_screen(state: SessionState) -> str:
    """What the shopper is looking at, for the router: «کدومش بهتره؟» is about these cards, not a new search."""
    cost = lambda n: f" ({format_price_short(n)} تومان)" if n else ""
    if state.last_block_kind == "shops" and state.shops_shown:
        product = next((p["title"] for p in state.products_shown if p["product_id"] == state.selected_product_id), "")
        open_fa = {"yes": "باز", "tight": "وقت کم", "no": "بسته", "unknown": "ساعت نامعلوم"}
        return f"الان روی صفحه: فروشنده‌های «{product}»: " + "، ".join(
            f"{s['name']}{cost(s['price_min'])} — {to_fa_digits(s['minutes'])} دقیقه، {s.get('where') or ''}، "
            f"{open_fa.get(s.get('open_state'), '')}" for s in state.shops_shown[:5])
    if state.last_block_kind == "products" and state.products_shown:
        near = lambda p: (f"، نزدیک‌ترین فروشنده {to_fa_digits(p['nearest_minutes'])} دقیقه "
                          f"{'پیاده' if p.get('nearest_by') == 'walk' else 'با ماشین'}"
                          if p.get("nearest_minutes") is not None else "")
        return "الان روی صفحه: کارت‌های محصول: " + "، ".join(f"«{p['title']}»" + cost(p["price_min"]) + near(p)
                                                           for p in state.products_shown[:4])
    return "الان روی صفحه نتیجه‌ای نیست."


def understand(llm: LLMClient, state: SessionState, text: str, session_id: str) -> RouterOutput:
    """History (previous turns only) + what is on screen + this message → RouterOutput. Any failure → rule_route
    (and is recorded)."""
    messages = [{"role": "system", "content": ROUTER_PROMPT, "cache": True},   # unchanging: cached across turns and users
                {"role": "system", "content": f"{_place_line(state)}\n{_on_screen(state)} سؤال درباره‌ی همین‌ها ask_results است."},
                *state.history[-6:], {"role": "user", "content": text}]

    def note(e: Exception, reply: str = "") -> None:
        if getattr(llm, "enabled", False):
            ROUTER_FALLBACKS.append({"at": time.strftime("%H:%M:%S"), "error": f"{type(e).__name__}: {str(e)[:200]}",
                                     "reply": reply[:300]})

    try:   # structured outputs: always the JSON, never prose (Persian JSON is token-heavy, hence 900)
        res = llm.complete("router", messages, session_id=session_id, max_tokens=900, schema=ROUTER_SCHEMA)
        return _router_output(res.json())
    except LLMError as e:
        note(e)   # e.g. the schema itself was refused: one plain JSON-mode try, so a schema problem can't break the app
    except Exception as e:
        note(e)
        return rule_route(text)
    try:
        res = llm.complete("router", messages, session_id=session_id, max_tokens=900, json_mode=True)
        return _router_output(res.json())
    except LLMError as e:
        prose = plain_text(getattr(e, "text", "")).strip()
        note(e, prose)
        # the model talked instead of answering in JSON: that is its reply to small talk («سلام», «اسمت چیه؟»)
        return RouterOutput(intent="other", message=prose[:500]) if prose else rule_route(text)
    except Exception as e:
        note(e)
        return rule_route(text)


# --- conditions -----------------------------------------------------------------------------------------------

def _parse_arrive_at(v) -> datetime | None:
    """«HH:MM» (today, or tomorrow if already past) or an ISO datetime within the next 7 days (the «شنبه ۱۰ صبح»
    chip). Built on the demo clock, not the real date."""
    if not isinstance(v, str):
        return None
    v = to_latin_digits(v.strip())
    if m := _HHMM.match(v):
        h, mi = int(m.group(1)), int(m.group(2))
        if h < 24 and mi < 60:
            now = clock.now()
            dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            return dt if dt >= now - timedelta(minutes=1) else dt + timedelta(days=1)
        return None
    try:
        dt = H.as_tehran(datetime.fromisoformat(v))
    except ValueError:
        return None
    now = clock.now()
    return dt if now - timedelta(minutes=1) <= dt <= now + timedelta(days=7) else None


def apply_patch(state: SessionState, patch: dict) -> list[str]:
    """Apply only known, valid condition changes: a model can't set payment='free', a negative radius or
    budget_min > budget_max. `arrive_at` arrives as «HH:MM» (Tehran)."""
    changed = []
    allowed = set(Conditions.model_fields) - {"center"}
    for k, v in patch.items():
        if k == "area":   # «نزدیک ولیعصر»: the search moves there, placed by the dataset's own shops
            spot = places.locate(v) if isinstance(v, str) else None
            if spot:
                state.conditions = state.conditions.model_copy(update={"center": Location(
                    lat=spot[0], lng=spot[1], label=" ".join(v.split())[:40], source="area")})
                changed.append("center")
                if spot[2]:   # found only in shop addresses: the shopper must see where we put them
                    changed.append(f"center_by_address:{spot[2]}")
            continue
        if k not in allowed:
            continue
        if k in ("budget_min", "budget_max") and isinstance(v, (int, float)) and 0 < v < 10_000:
            v = int(v * 1_000_000) if v < 50 else int(v * 1_000)   # «۲ تومن» = 2M, «۵۰۰ تومن» = 500k (Tehran slang)
        if k == "arrive_at":
            if v is None:   # «الان راه می‌افتم»
                if state.conditions.arrive_at is not None:
                    state.conditions = state.conditions.model_copy(update={"arrive_at": None})
                    changed.append(k)
                continue
            v = _parse_arrive_at(v)
            if v is None:
                continue
        if getattr(state.conditions, k) == v:
            continue
        try:
            state.conditions = Conditions.model_validate({**state.conditions.model_dump(), k: v})
            changed.append(k)
        except ValidationError:
            pass
    return changed


def conditions_event(state: SessionState) -> dict:
    c = state.conditions
    return {"area": c.center.label, "center": {"lat": c.center.lat, "lng": c.center.lng},
            "radius_km": c.radius_km, "budget_max": c.budget_max,
            "budget_label": f"تا {format_price_short(c.budget_max)}" if c.budget_max else None,
            "payment": c.payment, "fast_delivery": c.fast_delivery, "open": c.open,
            "arrive_at": c.arrive_at.strftime("%H:%M") if c.arrive_at else None,
            "arrive_label": _arrive_label(c.arrive_at),
            "arrive_today": bool(c.arrive_at) and H.as_tehran(c.arrive_at).date() == clock.now().date(),
            "demo_time": clock.now().strftime("%H:%M") if clock.is_demo() else None,
            "located": c.center.source != "default"}   # False = the city-centre default, never chosen by the shopper


def _arrive_label(dt: datetime | None) -> str | None:
    """«رسیدن ۲۰:۳۰» today, «رسیدن شنبه ۱۰:۰۰» on another day."""
    if not dt:
        return None
    dt, now = H.as_tehran(dt), clock.now()
    t = to_fa_digits(dt.strftime("%H:%M"))
    days = (dt.date() - now.date()).days
    if days <= 0:
        return f"رسیدن {t}"
    day = "فردا" if days == 1 else shops_agent.WEEKDAYS[H.iran_weekday(dt.date())]
    return f"رسیدن {day} {t}"


def say(state: SessionState, em: Emitter, text: str) -> str:
    """Emit a chat message and remember it, so the next turn's router sees what the assistant said."""
    state.remember("assistant", text)
    return em.emit("message", {"text": text})


# --- direct tools (chip kind=direct_call; no LLM) -------------------------------------------------------------

class DirectTool(BaseModel):
    """Base for a direct tool's args. Unknown keys are rejected."""
    model_config = {"extra": "forbid"}


DIRECT_TOOLS: dict[str, tuple[type[DirectTool], Callable[..., Iterator[str]]]] = {}


def direct(name: str, args_model: type[DirectTool]):
    def deco(fn):
        DIRECT_TOOLS[name] = (args_model, fn)
        return fn
    return deco


class SetBudgetArgs(DirectTool):
    budget_max: int | None = Field(None, ge=1_000, le=100_000_000_000)


class NoArgs(DirectTool):
    pass


@direct("noop", NoArgs)
def _chip_noop(state: SessionState, em: Emitter, args: NoArgs) -> Iterator[str]:
    yield say(state, em, "باشه.")


class PreferBrandArgs(DirectTool):
    brand: str | None = Field(None, max_length=40)


class ShowShopsArgs(DirectTool):
    product_id: str = Field(max_length=64)


@direct("set_budget", SetBudgetArgs)
def _chip_set_budget(state: SessionState, em: Emitter, args: SetBudgetArgs) -> Iterator[str]:
    if args.budget_max is None:   # «فرقی نداره»: clear it everywhere, or the old need budget would come back
        state.conditions = state.conditions.model_copy(update={"budget_max": None, "budget_min": None})
        if state.need:
            state.need = state.need.model_copy(update={"budget_max": None, "budget_min": None})
    else:
        apply_patch(state, {"budget_max": args.budget_max})
    yield em.emit("conditions", conditions_event(state))
    yield from rerun_latest(state, em)


class SetConditionsArgs(DirectTool):
    """The conditions strip and the «همین رو بذار» relax chip. Only the keys actually sent are changed;
    radius_km=None means «کل تهران», budget_max=None means «فرقی نداره»."""
    radius_km: float | None = Field(None, ge=0.2, le=50)
    payment: Literal["any", "credit", "credit_in_person"] | None = None
    open: Literal["any", "now", "at_arrival"] | None = None
    fast_delivery: bool | None = None
    budget_max: int | None = Field(None, ge=1_000, le=100_000_000_000)
    arrive_at: str | None = Field(None, max_length=25)
    mode: Literal["walk", "car"] | None = None


@direct("set_conditions", SetConditionsArgs)
def _chip_set_conditions(state: SessionState, em: Emitter, args: SetConditionsArgs) -> Iterator[str]:
    changed: list[str] = []
    for k in args.model_fields_set:
        v = getattr(args, k)
        if v is None and k in ("radius_km", "budget_max", "arrive_at"):   # «کل تهران» / «فرقی نداره» / «الان»
            if getattr(state.conditions, k) is not None:
                state.conditions = state.conditions.model_copy(update={k: None})
                if k == "budget_max" and state.need:
                    state.need = state.need.model_copy(update={"budget_max": None})
                changed.append(k)
        elif v is not None:
            changed += apply_patch(state, {k: v})
    yield em.emit("conditions", conditions_event(state))
    if changed:
        state.just_changed = _condition_fields(changed)
        yield from rerun_latest(state, em)


class SetLocationArgs(DirectTool):
    """GPS («مکان من») or an area from /api/areas. Tehran only."""
    lat: float = Field(ge=35.5, le=35.9)
    lng: float = Field(ge=51.0, le=51.8)
    label: str = Field(max_length=40)
    source: Literal["gps", "area"] = "area"


@direct("set_location", SetLocationArgs)
def _chip_set_location(state: SessionState, em: Emitter, args: SetLocationArgs) -> Iterator[str]:
    label = args.label
    if args.source == "gps" and (name := neshan.place_name(args.lat, args.lng)):   # «مکان من · کاووسیه، میدان ونک»:
        label = f"مکان من · {name}"[:40]                                            # the shopper sees if the fix is right
    state.conditions = state.conditions.model_copy(update={"center": Location(
        lat=args.lat, lng=args.lng, label=label, source=args.source)})
    yield em.emit("conditions", conditions_event(state))
    yield from rerun_latest(state, em)


class CallShopArgs(DirectTool):
    shop_id: str = Field(max_length=64)
    product_id: str = Field(max_length=64)
    hold: bool = False


@direct("call_shop", CallShopArgs)
def _chip_call_shop(state: SessionState, em: Emitter, args: CallShopArgs) -> Iterator[str]:
    """Step 4: simulated call to a shown shop for the selected product (S6)."""
    if args.product_id != state.selected_product_id or not (
            args.shop_id in state.call_ok_ids or store.sells(args.shop_id, args.product_id)):   # the map lists them all
        yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
        return
    yield from call_shop(state, em, args.shop_id, hold=args.hold)


class GoHubArgs(DirectTool):
    """«مدل‌ها تو علاءالدین»: search right at a hub (a passage), within walking distance of it."""
    lat: float = Field(ge=35.5, le=35.9)
    lng: float = Field(ge=51.0, le=51.8)
    label: str = Field(max_length=60)


@direct("go_hub", GoHubArgs)
def _chip_go_hub(state: SessionState, em: Emitter, args: GoHubArgs) -> Iterator[str]:
    state.conditions = state.conditions.model_copy(update={
        "center": Location(lat=args.lat, lng=args.lng, label=args.label, source="area"), "radius_km": 0.5})
    state.just_changed = ["radius_km"]
    yield em.emit("conditions", conditions_event(state))
    if state.last_block_kind == "shops" and state.selected_product_id:
        yield from show_shops(state, em)
    elif state.need:
        yield from show_products(state, em)
    else:
        yield say(state, em, "بگو دنبال چی هستی تا همین‌جا برات پیدا کنم.")


def _excluded(state: SessionState) -> set[str]:
    """Shops already called or failed — for the selected product only (a charger failure says nothing about earbuds)."""
    pid = state.selected_product_id
    called = {c["shop_id"] for c in state.calls.values() if c.get("product_id") == pid}
    tried = {k.split("|", 1)[1] for k in state.tried_shop_ids if k.startswith(f"{pid}|")}
    return called | tried


def _select_product(state: SessionState, product_id: str | None) -> None:
    """Changing product clears everything that belonged to the old one (shops, call targets, plan)."""
    if product_id != state.selected_product_id:
        state.shops_shown, state.call_ok_ids, state.shop_ids = [], [], []
        if state.plan and state.plan.get("product_id") != product_id:
            state.plan = None
    state.selected_product_id = product_id


def _find_offer(state: SessionState, shop_id: str):
    """The shop's current ShopOffer for the selected product, planned against the current conditions."""
    res = shops_agent.find_shops(state.selected_product_id, state.conditions, keep=frozenset(state.just_changed))
    for o in res.items + res.more:
        if o.shop["shop_id"] == shop_id:
            return o, res
    product, offers, _ = shops_agent.load_offers(state.selected_product_id)
    now = clock.now()
    for o in offers:
        if o.shop["shop_id"] == shop_id:
            shops_agent._plan_visit(o, state.conditions, now)
            return o, res
    return None, res


def call_shop(state: SessionState, em: Emitter, shop_id: str, hold: bool = False) -> Iterator[str]:
    o, res = _find_offer(state, shop_id)
    if o is None:
        yield em.emit("error", {"text": "این فروشگاه دیگه این مدل رو نداره."})
        return
    now = clock.now()
    call_id = f"c{len(state.calls) + 1}"
    earlier = next((c for c in reversed(state.calls.values()) if c.get("shop_id") == shop_id
                    and c.get("product_id") == res.product["product_id"] and c.get("outcome") == "ok"), None)
    lv = live_torob.CURRENT   # cache only: the live price the shop card already showed, if any
    live_price = lv.cached_shop_price(res.product["product_id"], o.shop) if lv else None
    plan, result = call_engine.run_call(o, res.product, state.conditions.payment, call_id, want_hold=hold,
                                        llm=em.llm, session_id=em.session_id, earlier=earlier, live_price=live_price)
    s = o.shop
    yield em.emit("call_start", {
        "call_id": call_id, "shop_id": shop_id, "shop_name": s["name"], "title": f"تماس با {s['name']}",
        "simulated": True,   # every call is; the page shows it as the «شبیه‌سازی» tag
        "reasons": "می‌پرسم چون: " + "، ".join(plan.reasons),
        "real_shop_note": "با این فروشگاه واقعاً تماس گرفته نشد." if s["name_is_real"] else None})
    yield em.emit("status", {"text": "در حال تماس…", "done": False})
    for who, text in plan.lines:
        yield em.emit("call_line", {"call_id": call_id, "who": who, "text": text})
    label = call_engine.result_label(result)
    yield em.emit("call_result", {"call_id": call_id, "result": result.as_dict(), "label": label})
    stock = {"ok": "simulated_confirmed", "unavailable": "simulated_unavailable", "no_answer": "no_answer"}[result.outcome]
    yield em.emit("card_update", {"block_id": "shops", "shop_id": shop_id, "stock": stock, "label": label,
                                  "price_change": call_engine.price_change_text(result),
                                  "location": result.location, "hold_until": result.hold_until,
                                  "style": "simulated"})
    state.calls[call_id] = result.as_dict()
    yield from _after_contact(state, em, o, res, result, hold, offer_sms=True)


def _after_contact(state: SessionState, em: Emitter, o, res, result, hold: bool, offer_sms: bool = False) -> Iterator[str]:
    """What follows a call or a text: a plan when the shop has it, else an alternative (after no answer to a call,
    first the offer to text the same shop)."""
    s, shop_id = o.shop, o.shop["shop_id"]
    now = clock.now()
    if result.outcome != "ok":
        key = f"{state.selected_product_id}|{shop_id}"
        if key not in state.tried_shop_ids:
            state.tried_shop_ids.append(key)
        if offer_sms and result.outcome == "no_answer":
            sms = Chip(label="پیامک بده", kind="direct_call", tool="sms_shop",
                       args={"shop_id": shop_id, "product_id": res.product["product_id"]}).model_dump()
            text = f"{s['name']} جواب نداد. براش پیامک بفرستم؟ (شبیه‌سازی)"
            state.remember("assistant", text)
            yield em.emit("question", {"text": text, "chips": [sms]})
        yield from _offer_alternative(state, em, s, result.outcome)
        return
    key = f"{state.selected_product_id}|{shop_id}"
    if key in state.tried_shop_ids:   # it didn't answer the phone but did answer the text
        state.tried_shop_ids.remove(key)
    tried = _excluded(state) | {shop_id}
    backup = next((x for x in res.items + res.more if x.shop["shop_id"] not in tried and x.open_state != "no"), None)
    same_product_plan = state.plan and state.plan.get("product_id") == state.selected_product_id
    if not same_product_plan or state.plan.get("shop_id") == shop_id or state.plan.get("status") != "ok":
        state.plan = followup.plan_card(o, res.product, result, backup, now)
        yield em.emit("plan", {"replace": True, **state.plan})
    else:   # a second shop also confirmed: let the user move the plan, don't switch silently
        chip = Chip(label="برنامه رو بذار روی این", kind="direct_call", tool="set_plan",
                    args={"call_id": result.call_id}).model_dump()
        yield em.emit("question", {"text": f"{s['name']} هم داره. برنامه رو ببرم روی این؟",
                                   "chips": [chip, Chip(label="نه", kind="direct_call", tool="noop").model_dump()]})
        return
    if not hold and result.hold_until is None:
        chip = followup.call_chip(shop_id, res.product["product_id"], "بپرس نگهش دارن؟", hold=True)
        yield em.emit("question", {"text": "بپرسم تا رسیدنت نگهش دارن؟", "chips": [chip, Chip(
            label="نه", kind="direct_call", tool="noop").model_dump()]})


@direct("sms_shop", CallShopArgs)
def _chip_sms_shop(state: SessionState, em: Emitter, args: CallShopArgs) -> Iterator[str]:
    """The fallback when a call goes unanswered: one text to the same shop, and its reply (simulated, labelled)."""
    if args.shop_id not in state.call_ok_ids or args.product_id != state.selected_product_id:
        yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
        return
    o, res = _find_offer(state, args.shop_id)
    if o is None:
        yield em.emit("error", {"text": "این فروشگاه دیگه این مدل رو نداره."})
        return
    sms_id = f"m{len(state.calls) + 1}"
    lv = live_torob.CURRENT
    live_price = lv.cached_shop_price(res.product["product_id"], o.shop) if lv else None
    sent, reply, result = call_engine.run_sms(o, res.product, sms_id, live_price=live_price)
    s = o.shop
    yield em.emit("call_start", {"call_id": sms_id, "shop_id": s["shop_id"], "shop_name": s["name"], "channel": "sms",
                                 "title": f"پیامک به {s['name']}", "simulated": True,
                                 "reasons": "تلفن رو جواب نداد؛ همون سؤال‌ها رو پیامک می‌کنم.",
                                 "real_shop_note": "به این فروشگاه واقعاً پیامکی فرستاده نشد." if s["name_is_real"] else None})
    yield em.emit("call_line", {"call_id": sms_id, "who": "agent", "text": sent})
    if reply:
        yield em.emit("call_line", {"call_id": sms_id, "who": "shop", "text": reply})
    label = call_engine.sms_label(result)
    yield em.emit("call_result", {"call_id": sms_id, "result": result.as_dict(), "label": label})
    stock = {"ok": "simulated_confirmed", "unavailable": "simulated_unavailable", "no_answer": "no_answer"}[result.outcome]
    yield em.emit("card_update", {"block_id": "shops", "shop_id": s["shop_id"], "stock": stock, "label": label,
                                  "price_change": call_engine.price_change_text(result), "location": result.location,
                                  "hold_until": result.hold_until, "style": "simulated"})
    state.calls[sms_id] = result.as_dict()
    yield from _after_contact(state, em, o, res, result, hold=True)


class SetPlanArgs(DirectTool):
    call_id: str = Field(max_length=16)


@direct("set_plan", SetPlanArgs)
def _chip_set_plan(state: SessionState, em: Emitter, args: SetPlanArgs) -> Iterator[str]:
    """Move the plan to a shop whose call (for the selected product) came back confirmed."""
    c = state.calls.get(args.call_id)
    if not c or c.get("outcome") != "ok" or c.get("product_id") != state.selected_product_id:
        yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
        return
    o, res = _find_offer(state, c["shop_id"])
    if o is None:
        yield em.emit("error", {"text": "این فروشگاه دیگه این مدل رو نداره."})
        return
    tried = _excluded(state) | {c["shop_id"]}
    backup = next((x for x in res.items + res.more if x.shop["shop_id"] not in tried and x.open_state != "no"), None)
    state.plan = followup.plan_card(o, res.product, call_engine.CallResult(**c), backup, clock.now())
    yield em.emit("plan", {"replace": True, **state.plan})


def _offer_alternative(state: SessionState, em: Emitter, failed_shop: dict, reason: str) -> Iterator[str]:
    alt = followup.alternative(state.selected_product_id, state.conditions, failed_shop, reason,
                               _excluded(state), clock.now())
    if alt is None:
        sim = " (شبیه‌سازی)" if reason != "closed" else ""
        yield say(state, em, f"چون {failed_shop['name']} {followup.REASONS[reason]}{sim}، گزینه‌ی دیگه‌ای با این شرایط پیدا نکردم.")
        return
    if alt["kind"] == "shop" and alt["shop"]["shop_id"] not in state.call_ok_ids:
        state.call_ok_ids.append(alt["shop"]["shop_id"])
    if alt["kind"] == "product" and alt["product"]["product_id"] not in state.product_ids:
        state.product_ids.append(alt["product"]["product_id"])
    state.remember("assistant", alt["text"])
    yield em.emit("alternative", alt)


@direct("prefer_brand", PreferBrandArgs)
def _chip_prefer_brand(state: SessionState, em: Emitter, args: PreferBrandArgs) -> Iterator[str]:
    """Sets the brand preference only; keywords stay the user's own (no stacking «اپل سامسونگ»)."""
    if state.need:
        state.need = state.need.model_copy(update={"brand_pref": [args.brand] if args.brand else []})
        yield from show_products(state, em, ask=False)


@direct("show_shops", ShowShopsArgs)
def _chip_show_shops(state: SessionState, em: Emitter, args: ShowShopsArgs) -> Iterator[str]:
    if args.product_id not in state.product_ids:   # only products this session was actually shown
        yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
        return
    _select_product(state, args.product_id)
    yield from show_shops(state, em)


# --- handlers -------------------------------------------------------------------------------------------------

def _card_with_button(card: dict) -> dict:
    btn = Chip(label="فروشنده‌ها", kind="direct_call", tool="show_shops", args={"product_id": card["product_id"]})
    return {**card, "button": btn.model_dump()}


def _relevance_judge(state: SessionState, em: Emitter):
    """With a model: it checks which found products are the thing asked for, in the shopper's own words."""
    if em.llm is None or not getattr(em.llm, "enabled", False):
        return None
    said = [h["content"] for h in state.history if h.get("role") == "user"][-2:]
    want = " / ".join(said)
    if state.need:   # the router's reading too: «airpods pro 2» against Persian titles needs «ایرپاد پرو ۲» as well
        meant = " · ".join(x for x in (state.need.named_product, state.need.product_type, *state.need.keywords[:2]) if x)
        want = f"{want} (یعنی: {meant})" if want else meant
    dropped = [f["label"] for f in state.filter_suggest if f.get("dropped")]
    if dropped:   # the shopper tapped × on these: their own earlier words no longer require them
        want += " — کاربر این شرط‌ها را برداشت و دیگر لازم نیست: " + "، ".join(dropped)
    return lambda cands: relevance.keep(em.llm, want, cands, em.session_id, criteria=state.filters)


def ask_with_options(state: SessionState, em: Emitter, q) -> str:
    """The model's clarifying question as answer buttons. A tapped answer is sent as the user's reply, so the next
    turn reads it with the conversation; a waiting question also offers «فرقی نداره» so nobody gets stuck."""
    chips = [Chip(label=o, kind="say", value=o) for o in q.options]
    if q.wait:
        chips.append(Chip(label="فرقی نداره", kind="say", value="فرقی نداره؛ همین‌طوری چندتا مدل پیشنهاد بده"))
    state.remember("assistant", q.text)
    return em.emit("question", {"text": q.text, "chips": [c.model_dump() for c in chips]})


_EARLY = ThreadPoolExecutor(max_workers=4, thread_name_prefix="filters")


def start_filters_early(llm: LLMClient, state: SessionState, text: str, session_id: str):
    """Start building filters while the message is still being understood: a quick rule reading of the product gives
    the category; if the model's reading lands in the same category, its filters are ready (saves a model round trip)."""
    if not getattr(llm, "enabled", False):
        return None
    guess = rule_route(text, allow_ask=False)
    if guess.intent not in ("need", "named_product") or not guess.need:
        return None
    cats = tuple(search.candidates(guess.need)[1][:2])
    if not cats:
        return None
    said = [h["content"] for h in state.history if h.get("role") == "user"] + [text]
    current = list(state.filters)
    return cats, _EARLY.submit(filter_builder.build, llm, said, guess.need.product_type, cats, current, session_id)


def build_filters(state: SessionState, em: Emitter, early=None) -> None:
    if em.llm is None or not getattr(em.llm, "enabled", False) or not state.need:
        return
    _, cats = search.candidates(state.need, wide=True)
    cats = tuple(cats[:2])
    got = None
    if early and early[0][:1] == cats[:1]:   # the same category the early build used: take its answer
        try:
            got = early[1].result(timeout=15)
        except Exception:
            got = None
    if got is None:
        said = [h["content"] for h in state.history if h.get("role") == "user"]
        got = filter_builder.build(em.llm, said, state.need.product_type, cats, state.filters, em.session_id)
    if got is not None:
        state.filters, state.filter_suggest = got


def filters_event(state: SessionState) -> dict:
    chip = lambda f: {"id": f["id"], "label": f["label"], "must": f["must"],
                      "chip": Chip(label=f["label"], kind="direct_call", tool="toggle_filter", args={"id": f["id"]}).model_dump()}
    return {"active": [chip(f) for f in state.filters], "suggest": [chip(f) for f in state.filter_suggest]}


class ToggleFilterArgs(DirectTool):
    id: str = Field(max_length=8)


@direct("toggle_filter", ToggleFilterArgs)
def _chip_toggle_filter(state: SessionState, em: Emitter, args: ToggleFilterArgs) -> Iterator[str]:
    """× on an active filter turns it off (it stays offered with «+»); «+» on an offered one turns it on."""
    on = next((f for f in state.filters if f["id"] == args.id), None)
    off = next((f for f in state.filter_suggest if f["id"] == args.id), None)
    if on:
        state.filters = [f for f in state.filters if f["id"] != args.id]
        state.filter_suggest = [{**on, "dropped": True}, *state.filter_suggest][:6]
        if on["kind"] == "brand" and state.need:   # «سامسونگ ×»: the search itself widens too, not just the filter
            names = {normalize_fa(x).lower() for x in [on["value"], *state.need.brand_pref]}
            names |= {k for k, v in search.BRAND_ALIASES.items() if v in names} | {v for k, v in search.BRAND_ALIASES.items() if k in names}
            strip = lambda t: " ".join(w for w in str(t).split() if normalize_fa(w).lower() not in names)
            kws = [strip(k) for k in state.need.keywords]
            state.need = state.need.model_copy(update={
                "brand_pref": [], "keywords": [k for k in kws if k] or [state.need.product_type],
                "product_type": strip(state.need.product_type) or state.need.product_type,
                "named_product": None})
    elif off:
        state.filter_suggest = [f for f in state.filter_suggest if f["id"] != args.id]
        state.filters = [*state.filters, {k: v for k, v in off.items() if k != "dropped"}]
    else:
        yield em.emit("error", {"text": "این فیلتر دیگه نیست."})
        return
    if state.need:
        yield from show_products(state, em, ask=False)


WIDE_SPAN = 20   # judged matches whose prices run 20× from the cheap end to the dear end: ask before showing models


def show_products(state: SessionState, em: Emitter, ask: bool = True, intro: bool = False,
                  build: bool = False, early=None) -> Iterator[str]:
    """Step 1: the advisor's 2–3 picks (or the one named model), with counts in the status line (R4).
    `build`: a new request in the shopper's own words: first turn them into filters (shown at once, before the
    research that uses them)."""
    yield em.emit("conditions", conditions_event(state))   # the strip appears with the first results (R1)
    if build and em.llm is not None and getattr(em.llm, "enabled", False):
        yield em.emit("status", {"text": "دارم از حرفات فیلتر می‌سازم…", "done": False})
        build_filters(state, em, early)
        if state.filters or state.filter_suggest:
            yield em.emit("filters", filters_event(state))
    spec_on = any(f["kind"] == "spec" for f in state.filters)
    yield em.emit("status", {"text": "دارم مدل‌های این دسته رو یکی‌یکی با فیلترهات می‌سنجم…" if spec_on
                             else "دارم مدل‌ها رو مقایسه می‌کنم…", "done": False})
    res = advisor.pick_products(state.need, state.conditions, judge=_relevance_judge(state, em), filters=state.filters)
    if res.named:
        card = res.named.card()
        state.product_ids, state.last_block_kind = [card["product_id"]], "products"
        state.products_shown = [composer.product_fact(card, advisor.spec_pairs(res.named.c.specs))]
        _select_product(state, card["product_id"])
        n = res.named.area.sellers
        found = f"{to_fa_digits(n)} فروشنده {res.named.where}" if n else "نزدیکت فروشنده‌ای ثبت نشده"
        yield em.emit("status", {"text": f"همین مدل رو پیدا کردم، {found}", "done": True})
        yield em.emit("product_cards", {"block_id": "products", "replace": True, "items": [_card_with_button(card)],
                                        "compare_rows": [], "notes": res.notes})
        yield from show_shops(state, em)   # R7: a named model skips straight to its sellers
        return
    n = state.need
    vague = not (state.conditions.budget_max or n.use_case or n.brand_pref or n.named_product or n.must_have
                 or n.price_pref == "cheap" or state.filters)
    q = res.question
    if ask and vague and res.n_matching is not None and res.price_span >= WIDE_SPAN and q and q["text"] not in state.asked:
        # «میخوام گوشی بخرم»: the matches run from a 2M feature phone to a 180M flagship; three of them at random would
        # be a guess. The budget buttons come first (with «فرقی نداره»), the models after the answer.
        state.asked.append(q["text"])
        state.remember("assistant", q["text"])
        yield em.emit("status", {"text": "مدل‌هاش از خیلی ساده تا خیلی گرونه", "done": True})
        yield em.emit("question", q)
        return
    if not build and (state.filters or state.filter_suggest):   # a rerun (filter tapped, budget…): the chips again
        yield em.emit("filters", filters_event(state))
    cards = [p.card() for p in res.picks]
    _select_product(state, None)   # a new set of products: no shop or plan from the previous one is callable
    state.product_ids = [c["product_id"] for c in cards]
    state.products_shown = [composer.product_fact(c, advisor.spec_pairs(p.c.specs)) for c, p in zip(cards, res.picks)]
    state.last_block_kind = "products"
    matching = f" → {to_fa_digits(res.n_matching)} تا همونیه که خواستی" if res.n_matching is not None else ""
    status = (f"{to_fa_digits(res.n_candidates)} مدل پیدا شد{matching} → {to_fa_digits(res.n_nearby)} تا نزدیکت فروشنده دارن"
              f" → {to_fa_digits(len(cards))} تا رو مقایسه کردم") if cards else "چیزی پیدا نشد"
    yield em.emit("status", {"text": status, "done": True})
    if cards:   # with none, the note is said once below as the assistant's message, not also as an empty block
        yield em.emit("product_cards", {"block_id": "products", "replace": True,
                                        "items": [_card_with_button(c) for c in cards],
                                        "compare_rows": res.compare_rows, "notes": res.notes,
                                        "hub": _hub_line(res.picks[0].c.category)})
    if intro and len(cards) >= 2:   # generative UI: a comparison built for this shopper's own words
        said = " / ".join(h["content"] for h in state.history if h["role"] == "user")[-300:]
        yield from compose_ui(state, em, said, intro=True)
    if not cards:
        yield say(state, em, res.notes[0] if res.notes else "چیزی پیدا نکردم؛ یه جور دیگه بگو؟")
    elif not res.in_area and state.conditions.radius_km is not None:
        yield from _coverage_offer(state, em)
    elif res.question and (ask or res.question.get("fact")) and res.question["text"] not in state.asked:   # once only
        state.asked.append(res.question["text"])
        state.remember("assistant", res.question["text"])
        yield em.emit("question", res.question)


def _hub_line(category: str) -> dict | None:
    """«بورس این دسته: پاساژ علاالدین (۴۴ فروشنده)» under the product cards, with what the hub map needs."""
    h = places.hubs((category,))
    if not h["buildings"] or h["spread"]:
        return None
    b = h["buildings"][0]
    return {"text": f"بورسش: {b['kind']} {b['name']} · {to_fa_digits(b['shops'])} فروشنده",
            "categories": [category], "subject": category}


def show_hub(state: SessionState, em: Emitter, need: Need | None) -> Iterator[str]:
    """«بورس … کجاست؟»: the buildings and neighbourhood where that trade gathers, measured on the dataset, with a map
    and a button per hub that searches right there."""
    need = need or state.need
    if need is None:
        yield say(state, em, "بورسِ چی رو می‌خوای؟ اسم کالا یا صنف رو بگو.")
        return
    state.need = need
    # The relevance judge reads the shopper's last words as «what they want». «بورس گوشی کجاست؟» asks for a place, so
    # a later «مدل‌ها تو علاالدین» would have every phone judged «not a bourse»: keep only the item in the history.
    if state.history and state.history[-1]["role"] == "user":
        state.history[-1] = {"role": "user", "content": need.product_type or " ".join(need.keywords[:2]) or state.history[-1]["content"]}
    yield em.emit("status", {"text": "دارم می‌گردم ببینم فروشنده‌هاش بیشتر کجا جمع‌ان…", "done": False})
    judge = _relevance_judge(state, em)
    cands, cats = search.candidates(need, wide=judge is not None)
    if judge and cands:   # a trade is named loosely («لوازم برقی»): its categories are those of the products that ARE it
        j = judge(cands)
        if j and j.kept:
            cats = [c for c, _ in Counter(c.category for c in j.kept).most_common(2)]
    if not cats:
        yield em.emit("status", {"text": "دسته‌اش رو پیدا نکردم", "done": True})
        yield say(state, em, "این کالا رو تو داده‌ی ترب پیدا نکردم؛ یه جور دیگه بگو؟")
        return
    cats = tuple(cats[:2])
    h = places.hubs(cats)
    subject = need.product_type or (need.keywords[0] if need.keywords else cats[0])
    text = places.hub_sentence(subject, h)
    yield em.emit("status", {"text": f"{to_fa_digits(h['total'])} فروشنده‌ی حضوری تو دسته‌ی «{'، '.join(cats)}» → "
                                     f"شمردم کجا جمع‌ان", "done": True})
    chips = [Chip(label=f"مدل‌ها تو {b['name']}", kind="direct_call", tool="go_hub",
                  args={"lat": b["lat"], "lng": b["lon"], "label": f"{b['kind']} {b['name']}"}).model_dump()
             for b in h["buildings"][:2]]
    chips.append(Chip(label="مدل‌ها رو نشون بده", kind="say", value=f"{subject} پیشنهاد بده").model_dump())
    state.remember("assistant", text)
    yield em.emit("hub", {"subject": subject, "text": text, "categories": list(cats), "total": h["total"],
                          "buildings": h["buildings"], "areas": h["areas"], "spread": h["spread"], "chips": chips})


def show_shops(state: SessionState, em: Emitter) -> Iterator[str]:
    """Steps 2–3: shops that registered the selected product, compared on identical rows (R6), with counts (R4),
    the junk line (R5), category-only shops (D14) and at most one relaxed condition, stated (R9)."""
    if not state.selected_product_id:
        yield say(state, em, "اول یه مدل انتخاب کن تا فروشنده‌هاش رو پیدا کنم.")
        return
    yield em.emit("conditions", conditions_event(state))
    yield em.emit("status", {"text": "دارم فروشنده‌های نزدیک رو مقایسه می‌کنم…", "done": False})
    res = shops_agent.find_shops(state.selected_product_id, state.conditions, keep=frozenset(state.just_changed))
    now = clock.now()
    cards = [shops_agent.card(o, res.product, state.conditions, now) for o in res.items]
    more = [shops_agent.card(o, res.product, state.conditions, now) for o in res.more[:20]]
    state.call_ok_ids = [c["shop_id"] for c in cards + more]   # shown as having it (S6 calls only these)
    state.shop_ids = state.call_ok_ids + [s["shop_id"] for s in res.category_only]
    state.shops_shown = [{
        "shop_id": o.shop["shop_id"], "name": o.shop["name"], "rank": i + 1, "price_min": o.price_min,
        "price_age": shops_agent.price_line(o, now)["age"], "minutes": o.minutes, "mode": o.mode, "open_state": o.open_state,
        "closes": o.closes.strftime("%H:%M") if o.closes else None,
        "closes_iso": o.closes.isoformat() if o.closes else None,   # compare as datetimes: 00:00 = midnight
        "credit": [p for p, _ in o.payments],
        "where": shops_agent.where_line(o.shop), "lat": o.shop["lat"], "lon": o.shop["lon"]}
        for i, o in enumerate(res.items)]
    state.last_block_kind = "shops"
    c = res.counts
    where = "نزدیکت" if state.conditions.radius_km else "در تهران"
    steps = [f"{to_fa_digits(c['found'])} فروشنده {where} ثبت کرده‌اند"]
    if c["terms_mismatch"]:
        steps.append(f"{to_fa_digits(c['terms_mismatch'])} تا با شرط‌هات جور نبود")
    if c["closed_at_arrival"]:
        steps.append(f"{to_fa_digits(c['closed_at_arrival'])} تا موقع رسیدن بسته‌ان")
    if c["suspicious_hidden"]:
        steps.append(f"{to_fa_digits(c['suspicious_hidden'])} قیمت مشکوک کنار رفت")
    steps.append("مرتب بر اساس قیمت، تازگی و فاصله")
    yield em.emit("status", {"text": " → ".join(steps), "done": True})
    junk = (f"{to_fa_digits(c['suspicious_hidden'])} قیمت مشکوک کنار گذاشته شد" if c["suspicious_hidden"] else None)
    yield em.emit("shop_cards", {
        "block_id": "shops", "replace": True,
        "product": {"product_id": res.product["product_id"], "title": res.product["title"]},
        "items": cards, "more": more, "more_count": len(res.more),
        "category_only": res.category_only, "notes": res.notes, "junk_line": junk,
        "relaxed": res.relaxed, "demo_time": now.strftime("%H:%M") if clock.is_demo() else None,
        "disclaimer": "نام و شماره‌ی فروشگاه‌ها در دیتاست ساختگی‌اند؛ تماس‌ها شبیه‌سازی است.",
    })
    if c["found"] == 0 and not cards:
        yield from _coverage_offer(state, em)
    elif not cards:
        yield say(state, em, res.notes[0] if res.notes else "نزدیکت فروشنده‌ای برای این مدل ثبت نشده.")
    # «open» relaxed means everything is closed at arrival: that is exactly when the next-morning offer helps
    elif (not state.conditions.arrive_at and not state.declined_morning and (not res.relaxed or res.relaxed["field"] == "open")
          and c["found"] - c["terms_mismatch"] >= 2
          and c["closed_at_arrival"] > 0.5 * (c["found"] - c["terms_mismatch"])):
        yield from _closed_now_offer(state, em, now)
    if state.plan and state.plan.get("product_id") == state.selected_product_id:
        yield from _recheck_plan(state, em)


def _next_shopping_morning(now: datetime) -> datetime:
    """Today 10:00 if it's still before 10 and not Friday; else the next non-Friday day at 10:00
    (most shops in the data are closed on Fridays)."""
    if now.hour < 10 and H.iran_weekday(now.date()) != 6:
        return now.replace(hour=10, minute=0, second=0, microsecond=0)
    day = now + timedelta(days=1)
    while H.iran_weekday(day.date()) == 6:
        day += timedelta(days=1)
    return day.replace(hour=10, minute=0, second=0, microsecond=0)


def _closed_now_offer(state: SessionState, em: Emitter, now: datetime) -> Iterator[str]:
    """R1: most shops are closed when you'd get there → offer the next shopping morning instead of a dead end."""
    morning = _next_shopping_morning(now)
    gap = (morning.date() - now.date()).days
    day = "امروز" if gap == 0 else "فردا" if gap == 1 else shops_agent.WEEKDAYS[H.iran_weekday(morning.date())]
    why = "جمعه‌ست و " if H.iran_weekday(now.date()) == 6 else ""
    text = f"{why}الان بیشتر مغازه‌ها تا برسی بسته‌ان — برای {day} ۱۰ صبح نشون بدم؟"
    args = {"arrive_at": morning.isoformat(timespec="minutes")}
    if state.conditions.open == "now":   # «باز الان» would still test the current time
        args["open"] = "at_arrival"
    chips = [Chip(label=f"{day} ۱۰ صبح", kind="direct_call", tool="set_conditions", args=args).model_dump(),
             Chip(label="نه، همین الان", kind="direct_call", tool="decline_morning").model_dump()]
    state.remember("assistant", text)
    yield em.emit("question", {"text": text, "chips": chips})


def _coverage_offer(state: SessionState, em: Emitter) -> Iterator[str]:
    """R1/Scene 10: the dataset covers ~10% of Torob's Tehran shops, mostly in the central area (Ferdowsi, Sheikh Hadi,
    Baharestan). Offer only chips that change something: the dense area if we're far from it, a bigger radius."""
    c = state.conditions
    lat, lng = DEFAULT_CENTER
    far = haversine_m(c.center.lat, c.center.lng, lat, lng) > 3_000
    chips = []
    if far:
        chips.append(Chip(label="جمهوری / فردوسی", kind="direct_call", tool="set_location",
                          args={"lat": lat, "lng": lng, "label": DEFAULT_AREA, "source": "area"}).model_dump())
    if c.radius_km is not None:
        if c.radius_km < 3:
            chips.append(Chip(label="تا ۳ کیلومتر", kind="direct_call", tool="set_conditions",
                              args={"radius_km": 3}).model_dump())
        chips.append(Chip(label="کل تهران", kind="direct_call", tool="set_conditions",
                          args={"radius_km": None}).model_dump())
    if not chips:
        return
    text = ("داده‌ی ترب این‌جا فروشگاه زیادی نداره (بیشترش منطقه‌ی مرکزی، اطراف جمهوری و فردوسیه). کجا رو نشون بدم؟"
            if far else "توی این شعاع فروشنده‌ای ثبت نشده. شعاع رو بیشتر کنم؟")
    state.remember("assistant", text)
    yield em.emit("question", {"text": text, "chips": chips})


class NoMorningArgs(DirectTool):
    pass


@direct("decline_morning", NoMorningArgs)
def _chip_decline_morning(state: SessionState, em: Emitter, args: NoMorningArgs) -> Iterator[str]:
    state.declined_morning = True   # don't ask again on every refresh
    yield say(state, em, "باشه، همین الان رو نشون می‌دم.")


def _recheck_plan(state: SessionState, em: Emitter) -> Iterator[str]:
    """Conditions changed (e.g. «ساعت ۸:۳۰ میام»): is the plan's shop still open when you'd arrive? (R2, Scene 8)"""
    o, _ = _find_offer(state, state.plan["shop_id"])
    if o is None:
        return
    now = clock.now()
    state.plan["dataset"]["visit"] = shops_agent.visit_line(o, now)
    state.plan["arrival"] = o.arrival.strftime("%H:%M") if o.arrival else None
    state.plan["open_at_arrival"] = o.open_state
    # The hold was agreed for a time; say so when it no longer covers the arrival (Scene 8).
    call = next((c for c in state.calls.values() if c["shop_id"] == o.shop["shop_id"]
                 and c.get("product_id") == state.selected_product_id and c.get("hold_until")), None)
    state.plan["simulated"]["hold_short"] = bool(call and o.arrival and call["hold_until"] < o.arrival.strftime("%H:%M"))
    _, res = _find_offer(state, o.shop["shop_id"])
    tried = _excluded(state) | {o.shop["shop_id"]}
    backup = next((x for x in res.items + res.more if x.shop["shop_id"] not in tried and x.open_state != "no"), None)
    state.plan["backup"] = followup._mini(backup, now) if backup else None
    if o.open_state == "no":
        state.plan["status"] = "closed_at_arrival"
        alt = followup.alternative(state.selected_product_id, state.conditions, o.shop, "closed",
                                   _excluded(state) | {o.shop["shop_id"]}, now, hold=True)
        state.plan["switch_to"] = alt
        if alt and alt["kind"] == "shop" and alt["shop"]["shop_id"] not in state.call_ok_ids:
            state.call_ok_ids.append(alt["shop"]["shop_id"])
    else:
        state.plan["status"], state.plan["switch_to"] = "ok", None
    yield em.emit("plan", {"replace": True, **state.plan})


def _condition_fields(changed: list[str]) -> list[str]:
    """Condition keys as the shop finder's relaxation names them (budget_min and budget_max are one «budget»)."""
    return sorted({"budget_max" if k in ("budget_min", "budget_max") else k for k in changed if ":" not in k})


def rerun_latest(state: SessionState, em: Emitter) -> Iterator[str]:
    """After a condition change, re-run the block the user is looking at (R15)."""
    if state.last_block_kind == "shops" and state.selected_product_id:
        yield from show_shops(state, em)
    elif state.need:
        yield from show_products(state, em)


def _turn(llm: LLMClient, state: SessionState, em: Emitter, text: str | None, chip: Chip | None) -> Iterator[str]:
    if chip and chip.kind == "direct_call":
        entry = DIRECT_TOOLS.get(chip.tool or "")
        if entry is None:
            yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
            return
        args_model, fn = entry
        try:
            args = args_model.model_validate(chip.args)
        except ValidationError:
            yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
            return
        yield from fn(state, em, args)
        return
    text = (chip.value if chip else text or "").strip()
    if not text:
        yield em.emit("error", {"text": "یه چیزی بنویس تا شروع کنیم."})
        return
    early = start_filters_early(llm, state, text, em.session_id)   # runs alongside the model's reading of the message
    out = understand(llm, state, text, em.session_id)   # history = previous turns; this message is added below
    state.remember("user", text)
    patch = dict(out.conditions_patch)
    if out.need and out.need.budget_max and "budget_max" not in patch:
        patch["budget_max"] = out.need.budget_max
    if out.need and out.need.budget_min and "budget_min" not in patch:   # «بالای ۱۵۰ میلیون»: a floor, not a ceiling
        patch["budget_min"] = out.need.budget_min
    if changed := apply_patch(state, patch):
        state.just_changed = _condition_fields(changed)
        yield em.emit("conditions", conditions_event(state))
    if by_address := next((c.split(":", 1)[1] for c in changed if c.startswith("center_by_address:")), None):
        yield say(state, em, f"«{patch['area'][:40]}» رو به‌عنوان محله تو داده‌ی ترب ندارم؛ چند فروشگاه این اسم تو "
                             f"آدرسشونه، اطراف «{by_address}». از همون‌جا نشونت می‌دم؛ اگه جای دیگه‌ای هستی بگو یا "
                             f"از نوار بالا مکان رو بزن.")
    if isinstance(patch.get("area"), str) and "center" not in changed:   # a place the dataset has no shops around
        yield say(state, em, f"«{patch['area'][:40]}» رو تو داده‌ی ترب پیدا نکردم؛ از «{state.conditions.center.label}» "
                             f"نشونت می‌دم. می‌تونی از نوار بالا محدوده رو عوض کنی.")
    if out.intent in ("need", "named_product") and out.need:
        if not state.need or normalize_fa(out.need.product_type) != normalize_fa(state.need.product_type):
            state.asked = []   # a different product may ask its own questions again
        state.need = out.need
        q = out.question if out.intent == "need" else None
        if out.message and not q:
            yield say(state, em, out.message)
        if q and q.wait:   # too vague: one question with answer buttons, models after the answer
            yield ask_with_options(state, em, q)
            return
        # the model's question replaces the advisor's; while it is open, no comparison (the answer changes the picks)
        asked = sum(e == "question" for e, _ in em.events)
        yield from show_products(state, em, ask=not q, intro=not q, build=True, early=early)
        if q and sum(e == "question" for e, _ in em.events) == asked:   # one question per turn (e.g. not after «کجا رو نشون بدم؟»)
            yield ask_with_options(state, em, q)
    elif out.intent == "refine":
        if state.need and state.last_block_kind in ("products", "shops"):   # the cards must reflect the new conditions
            if out.message:   # «قسطی دارن؟» → say what changed, so a question doesn't look ignored
                yield say(state, em, out.message)
            yield from rerun_latest(state, em)
        else:
            yield say(state, em, out.message or "شرایط رو عوض کردم.")
    elif out.intent == "show_shops":
        yield from _open_shops(state, em, f"{out.target} {text}")
    elif out.intent == "call_shop":
        if not state.shops_shown:
            if state.products_shown and state.last_block_kind == "products":   # «اولی رو بپرس»: its sellers first
                yield from _open_shops(state, em, f"{out.target} {text}")
                if state.shops_shown:
                    yield from _which_shop(state, em)
                return
            yield say(state, em, "اول فروشنده‌ها رو پیدا کنیم؛ بعد برات زنگ می‌زنم.")
            return
        shop = _resolve_shop(state, f"{out.target} {text}")
        if shop is None:
            yield from _which_shop(state, em)
        else:   # the user asked («بپرس») — that is the permission rule 5 needs
            yield from call_shop(state, em, shop["shop_id"], hold="نگه" in normalize_fa(text))
    elif out.intent == "hub":
        yield from show_hub(state, em, out.need)
    elif out.intent == "ask_results":
        if state.shops_shown or (state.products_shown and state.last_block_kind == "products"):
            yield from answer_results(state, em, text)
        else:   # «ارزون‌ترین پاوربانک» with nothing shown is a search, not a question about results
            redo = rule_route(text, allow_ask=False)
            if redo.need:
                state.need = redo.need
                yield from show_products(state, em)
            else:
                yield say(state, em, "بگو دنبال چه چیزی هستی تا برات پیدا کنم.")
    else:
        yield say(state, em, out.message or "بگو دنبال چه چیزی هستی تا برات پیدا کنم.")


def _resolve_shop(state: SessionState, target: str) -> dict | None:
    """«اولی/دومی/…» (whole word, within range) or the shop whose name matches best; None when unsure.
    With a single shown shop, «زنگ بزن» means that one. Never guesses the first shop."""
    shown = state.shops_shown
    if not shown:
        return None
    t = normalize_fa(target)
    m = _ORDINAL.search(t)
    if m:
        i = _ORDINAL_INDEX[m.group(1)]
        return shown[i] if i < len(shown) else None
    words = set(t.split())
    scored = []
    for s in shown:
        name = normalize_fa(s["name"])
        if name and name in t:
            return s                                   # full name: certain
        parts = [w for w in name.split() if len(w) > 2]
        scored.append((sum(w in words for w in parts) / max(1, len(parts)), s))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return shown[0] if len(shown) == 1 else None


def _resolve_product(state: SessionState, target: str) -> str | None:
    """The shown product card the shopper means: «اولی/دومی» or the best name match; the only card if there is one."""
    shown = state.products_shown
    if not shown:
        return None
    t = normalize_fa(target)
    if m := _ORDINAL.search(t):
        i = _ORDINAL_INDEX[m.group(1)]
        return shown[i]["product_id"] if i < len(shown) else None
    words = set(t.split())
    scored = sorted(((sum(w in words for w in normalize_fa(p["title"]).split() if len(w) > 2), p) for p in shown),
                    key=lambda x: -x[0])
    if scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]["product_id"]
    return shown[0]["product_id"] if len(shown) == 1 else None


def _open_shops(state: SessionState, em: Emitter, target: str) -> Iterator[str]:
    """Sellers of the product card the shopper points at, or a question with one button per card."""
    pid = _resolve_product(state, target)
    if pid and pid in state.product_ids:
        _select_product(state, pid)
        yield from show_shops(state, em)
        return
    if not state.products_shown:
        yield say(state, em, "اول بگو دنبال چی هستی تا مدل‌ها و فروشنده‌هاش رو پیدا کنم.")
        return
    asked = "فروشنده‌های کدوم مدل؟"
    near = [p for p in state.products_shown if p.get("nearest_minutes") is not None]
    if near and state.history[-2:-1] == [{"role": "assistant", "content": asked}]:   # asked already: don't ask twice
        p = min(near, key=lambda p: p["nearest_minutes"])
        yield say(state, em, f"فروشنده‌های «{p['title'][:50]}» رو می‌آرم؛ نزدیک‌ترین فروشنده‌اش حدود "
                             f"{to_fa_digits(p['nearest_minutes'])} دقیقه "
                             f"{'پیاده' if p.get('nearest_by') == 'walk' else 'با ماشین'}ه (تخمینی).")
        _select_product(state, p["product_id"])
        yield from show_shops(state, em)
        return
    def label(p: dict) -> str:   # the distance is what tells the cards apart for someone about to walk there
        m = p.get("nearest_minutes")
        return p["title"][:32] + (f" · {to_fa_digits(m)} دقیقه" if m is not None else "")
    chips = [Chip(label=label(p), kind="direct_call", tool="show_shops", args={"product_id": p["product_id"]}).model_dump()
             for p in state.products_shown]
    state.remember("assistant", asked)
    yield em.emit("question", {"text": asked, "chips": chips})


def _which_shop(state: SessionState, em: Emitter) -> Iterator[str]:
    chips = [Chip(label=s["name"], kind="direct_call", tool="call_shop",
                  args={"shop_id": s["shop_id"], "product_id": state.selected_product_id}).model_dump()
             for s in state.shops_shown]
    state.remember("assistant", "به کدومشون زنگ بزنم؟")
    yield em.emit("question", {"text": "به کدومشون زنگ بزنم؟", "chips": chips})


def _ui_chip(kind: str, x: dict, product_id: str | None) -> dict | None:
    """Buttons inside composed blocks: the same validated direct calls the cards use."""
    if kind == "show_shops":
        return Chip(label=f"فروشنده‌های «{x['title'][:40]}»", kind="direct_call", tool="show_shops",
                    args={"product_id": x["product_id"]}).model_dump()
    if kind == "call_shop":
        return Chip(label=f"از «{x['name'][:40]}» بپرس", kind="direct_call", tool="call_shop",
                    args={"shop_id": x["shop_id"], "product_id": product_id}).model_dump() if product_id else None
    return Chip(label=x["text"], kind="say", value=x["text"]).model_dump()


def _board(state: SessionState) -> composer.Board:
    """The cards on screen for compose: the shop list once there is one (with the product it is for), else products."""
    on_shops = state.last_block_kind == "shops" and bool(state.shops_shown)
    products = ([p for p in state.products_shown if p["product_id"] == state.selected_product_id] if on_shops
                else state.products_shown)
    shops = state.shops_shown if on_shops else []
    live, lv = {}, live_torob.CURRENT
    if lv and shops and state.selected_product_id:   # cache only: what the shop cards already showed
        for s in shops:
            if "lat" in s and (p := lv.cached_shop_price(state.selected_product_id, s)):
                live[s["shop_id"]] = p
    calls = {k: c for k, c in state.calls.items() if c.get("product_id") == state.selected_product_id}
    return composer.Board(products, shops, calls, live, place=place_facts(state))


def compose_ui(state: SessionState, em: Emitter, question: str, intro: bool = False):
    """Generative UI: the model picks the blocks (compare, bars, pick, checklist, next steps); the code fills them.
    Emits nothing and returns False when the model is off or its answer is unusable."""
    if em.llm is None or not getattr(em.llm, "enabled", False):
        return False
    ui = composer.compose(em.llm, _board(state), question, em.session_id, intro=intro,
                          selected_product_id=state.selected_product_id, chip_factory=_ui_chip)
    if ui is None:
        return False
    state.remember("assistant", ui["text"] or "(جدول مقایسه)")
    yield em.emit("ui", {"mode": "intro" if intro else "answer", **ui})
    return True


def answer_results(state: SessionState, em: Emitter, text: str) -> Iterator[str]:
    """Questions about the cards on screen (shops, or products before any shop list). With a model, the answer is
    composed as UI blocks whose numbers come only from the cards' facts; without one, rules for the common questions."""
    shown = state.shops_shown
    if not shown and not state.products_shown:
        yield say(state, em, "هنوز چیزی نشون ندادم؛ اول بگو چی لازم داری.")
        return
    if (yield from compose_ui(state, em, text)):
        return
    if not shown:
        best = state.products_shown[0]
        yield say(state, em, f"پیشنهاد اولم «{best['title']}»ه ({best['label']}). برای دیدن فروشنده‌هاش دکمه‌ی زیر کارتش رو بزن.")
        return
    yield say(state, em, rule_answer(shown, text))


def rule_answer(shown: list[dict], text: str) -> str:
    t = normalize_fa(text)
    name = lambda s: f"«{s['name']}»"
    if re.search(r"زودتر|دیرتر|می ?بنده|تا کی|بسته|باز", t):
        known = [s for s in shown if s["closes"]]
        unknown = [s for s in shown if not s["closes"]]
        if not known:
            return "ساعت بسته شدن هیچ‌کدوم ثبت نشده؛ می‌تونم بپرسم."
        key = lambda s: s.get("closes_iso") or s["closes"]
        early, late = min(known, key=key), max(known, key=key)
        parts = [f"{name(early)} زودتر می‌بنده ({to_fa_digits(early['closes'])})"]
        if late is not early:
            parts.append(f"{name(late)} تا {to_fa_digits(late['closes'])} بازه")
        if unknown:
            parts.append("ساعت " + "، ".join(name(s) for s in unknown) + " ثبت نشده — بپرسم؟")
            return "؛ ".join(parts)
        return "؛ ".join(parts) + "."
    if re.search(r"ارزون|ارزان|قیمت", t):
        priced = [s for s in shown if s["price_min"]]
        if not priced:
            return "قیمت هیچ‌کدوم ثبت نشده؛ می‌تونم بپرسم."
        c = min(priced, key=lambda s: s["price_min"])
        return f"کمترین قیمت ثبت‌شده مال {name(c)}ه: {format_toman(c['price_min'])} ({c['price_age']}). موجودیش تأیید نشده."
    if re.search(r"نزدیک|دور|پیاده|دقیقه", t):
        n = min(shown, key=lambda s: s["minutes"])
        return f"{name(n)} از همه نزدیک‌تره، حدود {to_fa_digits(n['minutes'])} دقیقه (تخمینی)."
    if re.search(r"قسط|اقساط|ترب ?پی|اعتبار", t):
        cr = [s for s in shown if s["credit"]]
        if not cr:
            return "هیچ‌کدوم از این‌ها شرایط قسطی ثبت نکرده."
        return "قسطی: " + "؛ ".join(f"{name(s)} ({'، '.join(s['credit'])})" for s in cr) + "."
    first = shown[0]
    return f"بهترین گزینه طبق قیمت، تازگی و فاصله {name(first)}ه ({first['where']}). سؤال خاصی درباره‌ی این‌ها داری؟"


def run_turn(llm: LLMClient, state: SessionState, em: Emitter, *, text: str | None = None,
             chip: Chip | None = None) -> Iterator[str]:
    """One user turn → SSE strings, always ending with `done` (unless the client disconnected)."""
    em.llm = llm
    try:
        yield from _turn(llm, state, em, text, chip)
    except Exception as e:   # never leave the client hanging; the detail goes to the event log only
        yield em.emit("error", {"text": "یه مشکلی پیش اومد؛ دوباره امتحان کن.", "detail": type(e).__name__})
    yield em.emit("done", {})
