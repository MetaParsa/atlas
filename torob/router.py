"""Step machine (spec §0.2 R10): one JSON LLM call per free-text turn, then code decides what runs.

Chips with kind=direct_call skip the LLM entirely; their args are validated per tool. `rule_route` parses simple
Persian requests without any model; it is FakeLLM's script in development and the fallback when a real model
returns unusable JSON or the budget is spent.
"""

import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import Literal
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from core.agent_loop import Emitter
from core.llm import LLMClient
from core.text import format_million, format_price_short, format_toman, normalize_fa, to_fa_digits, to_latin_digits
from torob import search
from torob import store
from torob import clock
from torob.agents import advisor
from torob.agents import call as call_engine
from torob.agents import compose as composer
from torob.agents import places
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
    """«تا ۳ میلیون» → 3M, «تا ۵۰۰ هزار» → 500k, «زیر ۳۰۰ تومن» → 300k (colloquial تومن = thousand),
    «تا ۲۵۰۰۰۰۰ تومان» → 2.5M. A bare small number counts as millions; «تا ۸ شب» is a time, not a budget."""
    for m in _BUDGET.finditer(t):
        n, unit = float(m.group(1).replace(",", "")), m.group(2) or ""
        if not unit and _TIME_WORDS.match(t[m.end():]):
            continue
        if unit.startswith("م"):
            return int(n * 1_000_000)
        if unit == "هزار":
            return int(n * 1_000)
        if unit in ("تومن", "تومان"):
            return int(n) if n >= 10_000 else int(n * 1_000)
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


def understand(llm: LLMClient, state: SessionState, text: str, session_id: str) -> RouterOutput:
    """History (previous turns only) + this message → RouterOutput. Any failure → rule_route (and is recorded)."""
    messages = [{"role": "system", "content": ROUTER_PROMPT}, *state.history[-6:], {"role": "user", "content": text}]
    res = None
    try:
        res = llm.complete("router", messages, session_id=session_id, max_tokens=900, json_mode=True)   # Persian JSON is token-heavy
        return RouterOutput.model_validate(res.json())
    except Exception as e:   # LLM error, budget spent, bad JSON, schema mismatch: the app keeps working on rules
        if getattr(llm, "enabled", False):
            ROUTER_FALLBACKS.append({"at": time.strftime("%H:%M:%S"), "error": f"{type(e).__name__}: {str(e)[:200]}",
                                     "reply": (res.text[:300] if res is not None else "")})
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
        if k not in allowed:
            continue
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
            "demo_time": clock.now().strftime("%H:%M") if clock.is_demo() else None}


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
    state.conditions = state.conditions.model_copy(update={"center": Location(
        lat=args.lat, lng=args.lng, label=args.label, source=args.source)})
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
    if result.outcome != "ok":
        key = f"{state.selected_product_id}|{shop_id}"
        if key not in state.tried_shop_ids:
            state.tried_shop_ids.append(key)
        yield from _offer_alternative(state, em, s, result.outcome)
        return
    tried = _excluded(state) | {shop_id}
    backup = next((x for x in res.items + res.more if x.shop["shop_id"] not in tried and x.open_state != "no"), None)
    same_product_plan = state.plan and state.plan.get("product_id") == state.selected_product_id
    if not same_product_plan or state.plan.get("shop_id") == shop_id or state.plan.get("status") != "ok":
        state.plan = followup.plan_card(o, res.product, result, backup, now)
        yield em.emit("plan", {"replace": True, **state.plan})
    else:   # a second shop also confirmed: let the user move the plan, don't switch silently
        chip = Chip(label="برنامه رو بذار روی این", kind="direct_call", tool="set_plan",
                    args={"call_id": call_id}).model_dump()
        yield em.emit("question", {"text": f"{s['name']} هم داره. برنامه رو ببرم روی این؟",
                                   "chips": [chip, Chip(label="نه", kind="direct_call", tool="noop").model_dump()]})
        return
    if not hold and result.hold_until is None:
        chip = followup.call_chip(shop_id, res.product["product_id"], "بپرس نگهش دارن؟", hold=True)
        yield em.emit("question", {"text": "بپرسم تا رسیدنت نگهش دارن؟", "chips": [chip, Chip(
            label="نه", kind="direct_call", tool="noop").model_dump()]})


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
    want = " / ".join(said) or (state.need.named_product or state.need.product_type if state.need else "")
    return lambda cands: relevance.keep(em.llm, want, cands, em.session_id)


def ask_with_options(state: SessionState, em: Emitter, q) -> str:
    """The model's clarifying question as answer buttons. A tapped answer is sent as the user's reply, so the next
    turn reads it with the conversation; a waiting question also offers «فرقی نداره» so nobody gets stuck."""
    chips = [Chip(label=o, kind="say", value=o) for o in q.options]
    if q.wait:
        chips.append(Chip(label="فرقی نداره", kind="say", value="فرقی نداره؛ همین‌طوری چندتا مدل پیشنهاد بده"))
    state.remember("assistant", q.text)
    return em.emit("question", {"text": q.text, "chips": [c.model_dump() for c in chips]})


def show_products(state: SessionState, em: Emitter, ask: bool = True, intro: bool = False) -> Iterator[str]:
    """Step 1: the advisor's 2–3 picks (or the one named model), with counts in the status line (R4)."""
    yield em.emit("conditions", conditions_event(state))   # the strip appears with the first results (R1)
    yield em.emit("status", {"text": "دارم مدل‌ها رو مقایسه می‌کنم…", "done": False})
    res = advisor.pick_products(state.need, state.conditions, judge=_relevance_judge(state, em))
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
    elif ask and res.question:
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
    _, cats = search.candidates(need)
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
        "price_age": shops_agent.price_line(o, now)["age"], "minutes": o.minutes, "open_state": o.open_state,
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
    elif (not state.conditions.arrive_at and not state.declined_morning and not res.relaxed
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
    return sorted({"budget_max" if k in ("budget_min", "budget_max") else k for k in changed})


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
    out = understand(llm, state, text, em.session_id)   # history = previous turns; this message is added below
    state.remember("user", text)
    patch = dict(out.conditions_patch)
    if out.need and out.need.budget_max and "budget_max" not in patch:
        patch["budget_max"] = out.need.budget_max
    if changed := apply_patch(state, patch):
        state.just_changed = _condition_fields(changed)
        yield em.emit("conditions", conditions_event(state))
    if out.intent in ("need", "named_product") and out.need:
        state.need = out.need
        q = out.question if out.intent == "need" else None
        if out.message and not q:
            yield say(state, em, out.message)
        if q and q.wait:   # too vague: one question with answer buttons, models after the answer
            yield ask_with_options(state, em, q)
            return
        # the model's question replaces the advisor's; while it is open, no comparison (the answer changes the picks)
        yield from show_products(state, em, ask=not q, intro=not q)
        if q:
            yield ask_with_options(state, em, q)
    elif out.intent == "refine":
        if state.need and state.last_block_kind in ("products", "shops"):   # the cards must reflect the new conditions
            yield from rerun_latest(state, em)
        else:
            yield say(state, em, out.message or "شرایط رو عوض کردم.")
    elif out.intent == "call_shop":
        if not state.shops_shown:
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
    return composer.Board(products, shops, calls, live)


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
