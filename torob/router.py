"""Step machine (spec §0.2 R10): one JSON LLM call per free-text turn, then code decides what runs.

Chips with kind=direct_call skip the LLM entirely; their args are validated per tool. `rule_route` parses simple
Persian requests without any model; it is FakeLLM's script in development and the fallback when a real model
returns unusable JSON or the budget is spent.
"""

import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from core.agent_loop import Emitter
from core.llm import LLMClient
from core.text import TEHRAN, format_million, normalize_fa, to_fa_digits, to_latin_digits
from torob import store
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
    words = [w for w in re.sub(r"[^\w\s]", " ", _BUDGET.sub(" ", t)).split() if w not in _STOP and not w.isdigit()]
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
            return datetime.now(TEHRAN).replace(hour=h, minute=mi, second=0, microsecond=0)
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


class ShowProductsArgs(DirectTool):
    query: str = ""


@direct("show_products", ShowProductsArgs)
def _chip_show_products(state: SessionState, em: Emitter, args: ShowProductsArgs) -> Iterator[str]:
    yield from show_products(state, em, args.query[:200])


# --- handlers (S1 skeleton; S2–S6 replace the bodies) ---------------------------------------------------------

def show_products(state: SessionState, em: Emitter, query: str) -> Iterator[str]:
    """S1 stub: plain FTS search so the flow is testable end to end. S2 swaps in the advisor (roles, seller floor)."""
    yield em.emit("status", {"text": "دارم مدل‌ها رو مقایسه می‌کنم…", "done": False})
    rows = store.search_products(store.get(), query, limit=3)
    state.product_ids = [r["product_id"] for r in rows]
    state.last_block_kind = "products"
    yield em.emit("status", {"text": f"{to_fa_digits(len(rows))} مدل پیدا شد", "done": True})
    yield em.emit("product_cards", {"block_id": "products", "replace": True, "items": [
        {"product_id": r["product_id"], "title": r["title"], "sellers_total": r["sellers_total"],
         "price_min": r["price_min"], "price_max": r["price_max"]} for r in rows]})
    if not rows:
        yield say(state, em, "توی داده‌ی ترب چیزی با این مشخصات پیدا نکردم. یه جور دیگه بگو؟")


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
        query = out.need.named_product or " ".join(out.need.keywords) or out.need.product_type
        yield from show_products(state, em, query)
    elif out.intent == "refine":
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
