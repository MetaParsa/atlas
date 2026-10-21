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

from core.agent_loop import Emitter
from core.llm import LLMClient
from core.text import TEHRAN, format_million, normalize_fa, to_fa_digits, to_latin_digits
from torob import store
from torob import clock
from torob.agents import advisor
from torob.agents import shops as shops_agent
from torob.models import Chip, Conditions, Need, RouterOutput, SessionState

ROUTER_PROMPT = (Path(__file__).parent / "prompts" / "router.md").read_text(encoding="utf-8")

_NUM = r"(\d+(?:[.,]\d+)?)"
_BUDGET = re.compile(rf"(?:تا|زیر|حداکثر|کمتر از|نهایتا|نهایت)\s*{_NUM}\s*(میلیون|م(?![ء-ی])|هزار|تومن|تومان)?")
_TIME_WORDS = re.compile(r"^\s*(?:شب|صبح|عصر|ظهر|بعد ?از ?ظهر|ساعت|دقیقه|روز)")
_CREDIT = re.compile(r"قسط|اقساط|اعتباری|ترب ?پی|اسنپ ?پی|دیجی ?پی")
_CALL = re.compile(r"بپرس|زنگ بزن|تماس بگیر")
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


def rule_route(text: str) -> RouterOutput:
    """No-model understanding of the common cases. Deliberately simple; never invents numbers."""
    t = to_latin_digits(normalize_fa(text))
    patch: dict = {}
    budget = _budget(t)
    if budget:
        patch["budget_max"] = budget
    if _CREDIT.search(t):
        patch["payment"] = "credit"
    if _CALL.search(t):
        return RouterOutput(intent="call_shop", conditions_patch=patch)
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
            "budget_label": f"تا {format_million(c.budget_max)}" if c.budget_max else None,
            "payment": c.payment, "fast_delivery": c.fast_delivery, "open": c.open,
            "arrive_at": c.arrive_at.strftime("%H:%M") if c.arrive_at else None}


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


class CallShopArgs(DirectTool):
    shop_id: str = Field(max_length=64)
    product_id: str = Field(max_length=64)


@direct("call_shop", CallShopArgs)
def _chip_call_shop(state: SessionState, em: Emitter, args: CallShopArgs) -> Iterator[str]:
    """S6 runs the simulated call; only shops this session was shown, for the selected product."""
    if args.shop_id not in state.call_ok_ids or args.product_id != state.selected_product_id:
        yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
        return
    yield em.emit("call_start", {"call_id": "pending", "shop_id": args.shop_id, "pending": "S6"})


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
    state.selected_product_id = args.product_id
    yield from show_shops(state, em)


# --- handlers -------------------------------------------------------------------------------------------------

def _card_with_button(card: dict) -> dict:
    btn = Chip(label="فروشنده‌ها", kind="direct_call", tool="show_shops", args={"product_id": card["product_id"]})
    return {**card, "button": btn.model_dump()}


def show_products(state: SessionState, em: Emitter, ask: bool = True) -> Iterator[str]:
    """Step 1: the advisor's 2–3 picks (or the one named model), with counts in the status line (R4)."""
    yield em.emit("status", {"text": "دارم مدل‌ها رو مقایسه می‌کنم…", "done": False})
    res = advisor.pick_products(state.need, state.conditions)
    if res.named:
        card = res.named.card()
        state.product_ids, state.selected_product_id, state.last_block_kind = [card["product_id"]], card["product_id"], "products"
        n = res.named.area.sellers
        found = f"{to_fa_digits(n)} فروشنده {res.named.where}" if n else "نزدیکت فروشنده‌ای ثبت نشده"
        yield em.emit("status", {"text": f"همین مدل رو پیدا کردم · {found}", "done": True})
        yield em.emit("product_cards", {"block_id": "products", "replace": True, "items": [_card_with_button(card)],
                                        "compare_rows": [], "notes": res.notes})
        yield from show_shops(state, em)   # R7: a named model skips straight to its sellers
        return
    cards = [p.card() for p in res.picks]
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
    yield em.emit("status", {"text": "دارم فروشنده‌های نزدیک رو مقایسه می‌کنم…", "done": False})
    res = shops_agent.find_shops(state.selected_product_id, state.conditions)
    now = clock.now()
    cards = [shops_agent.card(o, res.product, state.conditions, now) for o in res.items]
    more = [shops_agent.card(o, res.product, state.conditions, now) for o in res.more[:20]]
    state.call_ok_ids = [c["shop_id"] for c in cards + more]   # shown as having it (S6 calls only these)
    state.shop_ids = state.call_ok_ids + [s["shop_id"] for s in res.category_only]
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
    else:
        # call_shop / ask_results land in S3–S6
        yield say(state, em, out.message or "بگو دنبال چه چیزی هستی تا برات پیدا کنم.")


def run_turn(llm: LLMClient, state: SessionState, em: Emitter, *, text: str | None = None,
             chip: Chip | None = None) -> Iterator[str]:
    """One user turn → SSE strings, always ending with `done` (unless the client disconnected)."""
    try:
        yield from _turn(llm, state, em, text, chip)
    except Exception as e:   # never leave the client hanging; the detail goes to the event log only
        yield em.emit("error", {"text": "یه مشکلی پیش اومد؛ دوباره امتحان کن.", "detail": type(e).__name__})
    yield em.emit("done", {})
