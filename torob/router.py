"""Step machine (spec §0.2 R10): one JSON LLM call per free-text turn, then code decides what runs.

Chips with kind=direct_call skip the LLM entirely; their args are validated per tool. `rule_route` parses simple
Persian requests without any model; it is FakeLLM's script in development and the fallback when a real model
returns unusable JSON or the budget is spent.
"""

import re
from collections.abc import Callable, Iterator
from typing import Literal
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from core.agent_loop import Emitter, Tool, tool_loop
from core.llm import LLMClient
from core.text import format_million, format_price_short, format_toman, normalize_fa, to_fa_digits, to_latin_digits
from torob import store
from torob import clock
from torob.agents import advisor
from torob.agents import call as call_engine
from torob.agents import followup
from torob.agents import shops as shops_agent
from torob.models import Chip, Conditions, Location, Need, RouterOutput, SessionState

ROUTER_PROMPT = (Path(__file__).parent / "prompts" / "router.md").read_text(encoding="utf-8")

_NUM = r"(\d+(?:[.,]\d+)?)"
_BUDGET = re.compile(rf"(?:تا|زیر|حداکثر|کمتر از|نهایتا|نهایت)\s*{_NUM}\s*(میلیون|م(?![ء-ی])|هزار|تومن|تومان)?")
_TIME_WORDS = re.compile(r"^\s*(?:شب|صبح|عصر|ظهر|بعد ?از ?ظهر|ساعت|دقیقه|روز)")
_CREDIT = re.compile(r"قسط|اقساط|اعتباری|ترب ?پی|اسنپ ?پی|دیجی ?پی")
_CALL = re.compile(r"بپرس|زنگ بزن|تماس بگیر")
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
    if _CALL.search(t):
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
    need = Need(product_type=" ".join(words[:3]), keywords=[" ".join(words)], budget_max=budget)
    return RouterOutput(intent="need", need=need, conditions_patch=patch)


def understand(llm: LLMClient, state: SessionState, text: str, session_id: str) -> RouterOutput:
    """History (previous turns only) + this message → RouterOutput. Any failure → rule_route."""
    messages = [{"role": "system", "content": ROUTER_PROMPT}, *state.history[-6:], {"role": "user", "content": text}]
    try:
        res = llm.complete("router", messages, session_id=session_id, max_tokens=300, json_mode=True)
        return RouterOutput.model_validate(res.json())
    except Exception:   # LLM error, budget spent, bad JSON, schema mismatch: the app keeps working on rules
        return rule_route(text)


# --- conditions -----------------------------------------------------------------------------------------------

def _parse_arrive_at(v) -> datetime | None:
    if isinstance(v, str) and (m := _HHMM.match(to_latin_digits(v.strip()))):
        h, mi = int(m.group(1)), int(m.group(2))
        if h < 24 and mi < 60:
            return clock.now().replace(hour=h, minute=mi, second=0, microsecond=0)   # demo clock, not the real date
    return None


def apply_patch(state: SessionState, patch: dict) -> list[str]:
    """Apply only known, valid condition changes: a model can't set payment='free', a negative radius or
    budget_min > budget_max. `arrive_at` arrives as «HH:MM» (Tehran)."""
    changed = []
    allowed = set(Conditions.model_fields) - {"center"}
    for k, v in patch.items():
        if k not in allowed:
            continue
        if k == "arrive_at":
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
    return {"area": c.center.label, "radius_km": c.radius_km, "budget_max": c.budget_max,
            "budget_label": f"تا {format_price_short(c.budget_max)}" if c.budget_max else None,
            "payment": c.payment, "fast_delivery": c.fast_delivery, "open": c.open,
            "arrive_at": c.arrive_at.strftime("%H:%M") if c.arrive_at else None,
            "demo_time": clock.now().strftime("%H:%M") if clock.is_demo() else None}


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
    arrive_at: str | None = Field(None, max_length=5)
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
    if args.shop_id not in state.call_ok_ids or args.product_id != state.selected_product_id:
        yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
        return
    yield from call_shop(state, em, args.shop_id, hold=args.hold)


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
    res = shops_agent.find_shops(state.selected_product_id, state.conditions)
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
    plan, result = call_engine.run_call(o, res.product, state.conditions.payment, call_id, want_hold=hold,
                                        llm=em.llm, session_id=em.session_id)
    s = o.shop
    yield em.emit("call_start", {
        "call_id": call_id, "shop_id": shop_id, "shop_name": s["name"], "title": f"تماس با {s['name']}، شبیه‌سازی",
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


def show_products(state: SessionState, em: Emitter, ask: bool = True) -> Iterator[str]:
    """Step 1: the advisor's 2–3 picks (or the one named model), with counts in the status line (R4)."""
    yield em.emit("conditions", conditions_event(state))   # the strip appears with the first results (R1)
    yield em.emit("status", {"text": "دارم مدل‌ها رو مقایسه می‌کنم…", "done": False})
    res = advisor.pick_products(state.need, state.conditions)
    if res.named:
        card = res.named.card()
        state.product_ids, state.last_block_kind = [card["product_id"]], "products"
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
    state.last_block_kind = "products"
    status = (f"{to_fa_digits(res.n_candidates)} مدل پیدا شد → {to_fa_digits(res.n_nearby)} تا نزدیکت فروشنده دارن"
              f" → {to_fa_digits(len(cards))} تا رو مقایسه کردم") if cards else "چیزی پیدا نشد"
    yield em.emit("status", {"text": status, "done": True})
    yield em.emit("product_cards", {"block_id": "products", "replace": True, "items": [_card_with_button(c) for c in cards],
                                    "compare_rows": res.compare_rows, "notes": res.notes})
    if not cards:
        yield say(state, em, res.notes[0] if res.notes else "چیزی پیدا نکردم؛ یه جور دیگه بگو؟")
    elif ask and res.question:
        state.remember("assistant", res.question["text"])
        yield em.emit("question", res.question)


def show_shops(state: SessionState, em: Emitter) -> Iterator[str]:
    """Steps 2–3: shops that registered the selected product, compared on identical rows (R6), with counts (R4),
    the junk line (R5), category-only shops (D14) and at most one relaxed condition, stated (R9)."""
    if not state.selected_product_id:
        yield say(state, em, "اول یه مدل انتخاب کن تا فروشنده‌هاش رو پیدا کنم.")
        return
    yield em.emit("conditions", conditions_event(state))
    yield em.emit("status", {"text": "دارم فروشنده‌های نزدیک رو مقایسه می‌کنم…", "done": False})
    res = shops_agent.find_shops(state.selected_product_id, state.conditions)
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
        "where": shops_agent.where_line(o.shop)} for i, o in enumerate(res.items)]
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
    if not cards:
        yield say(state, em, res.notes[0] if res.notes else "نزدیکت فروشنده‌ای برای این مدل ثبت نشده.")
    if state.plan and state.plan.get("product_id") == state.selected_product_id:
        yield from _recheck_plan(state, em)


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
    if apply_patch(state, patch):
        yield em.emit("conditions", conditions_event(state))
    if out.intent in ("need", "named_product") and out.need:
        state.need = out.need
        if out.message:
            yield say(state, em, out.message)
        yield from show_products(state, em)
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
    elif out.intent == "ask_results":
        if state.shops_shown:
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


def answer_results(state: SessionState, em: Emitter, text: str) -> Iterator[str]:
    """Questions about the shown shops. Numbers come only from their facts; with an LLM, a short tool loop over the
    same facts; without one, rules for the common questions."""
    shown = state.shops_shown
    if not shown:
        yield say(state, em, "هنوز فروشگاهی نشون ندادم؛ اول بگو چی لازم داری.")
        return
    if em.llm is not None and getattr(em.llm, "enabled", False):
        tool = Tool("shown_shops", "facts about the shops currently shown to the user", {"type": "object", "properties": {}},
                    lambda: {"shops": shown})
        msgs = [{"role": "system", "content": "به فارسی محاوره‌ای و کوتاه جواب بده. فقط از خروجی ابزار shown_shops عدد و "
                                              "ساعت نقل کن؛ چیزی که نیست بگو «ثبت نشده». قیمت ثبت‌شده را موجودی تأییدشده ندان."},
                {"role": "user", "content": text}]
        for chunk in tool_loop(em.llm, msgs, [tool], em, max_steps=3, max_tokens=300):
            yield chunk
        state.remember("assistant", em.events[-1][1].get("text", ""))
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
