"""Step machine (spec §0.2 R10): one JSON LLM call per free-text turn, then code decides what runs.

Chips with kind=direct_call skip the LLM entirely. `rule_route` parses simple Persian requests without any model;
it is FakeLLM's script in development and the fallback when a real model returns unusable JSON.
"""

import re
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic import ValidationError

from core.agent_loop import Emitter
from core.llm import LLMClient, LLMError
from core.text import format_million, normalize_fa, to_fa_digits, to_latin_digits
from torob import store
from torob.models import Chip, Conditions, Need, RouterOutput, SessionState

ROUTER_PROMPT = (Path(__file__).parent / "prompts" / "router.md").read_text(encoding="utf-8")

_BUDGET = re.compile(r"(?:تا|زیر|حداکثر|کمتر از)\s*([\d.,]+)\s*(میلیون|م\b|هزار|تومن|تومان)?")
_CREDIT = re.compile(r"قسط|اقساط|اعتباری|ترب ?پی|اسنپ ?پی|دیجی ?پی")
_CALL = re.compile(r"بپرس|زنگ بزن|تماس بگیر")
_STOP = set("یه یک می خوام میخوام خواستم برای با از که رو را و تا زیر میلیون تومن تومان هزار دارم لازم دنبال ای هم "
            "خوب یدونه یکی چی چیه هست باشه بشه قسطی اقساطی".split())


def _budget(text: str) -> int | None:
    m = _BUDGET.search(to_latin_digits(text))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    unit = m.group(2) or ""
    return int(n * 1_000_000) if unit.startswith("م") else int(n * 1_000) if unit == "هزار" else int(n) if n > 10_000 else int(n * 1_000_000)


def rule_route(text: str) -> RouterOutput:
    """No-model understanding of the common cases. Deliberately simple; never invents numbers."""
    t = normalize_fa(text)
    patch: dict = {}
    budget = _budget(t)
    if budget:
        patch["budget_max"] = budget
    if _CREDIT.search(t):
        patch["payment"] = "credit"
    if _CALL.search(t):
        return RouterOutput(intent="call_shop", conditions_patch=patch)
    words = [w for w in re.sub(r"[^\w\s]", " ", _BUDGET.sub(" ", to_latin_digits(t))).split()
             if w not in _STOP and not w.isdigit()]
    if not words:
        return RouterOutput(intent="refine" if patch else "other", conditions_patch=patch)
    need = Need(product_type=" ".join(words[:3]), keywords=[" ".join(words)], budget_max=budget)
    return RouterOutput(intent="need", need=need, conditions_patch=patch)


def understand(llm: LLMClient, state: SessionState, text: str, session_id: str) -> RouterOutput:
    messages = [{"role": "system", "content": ROUTER_PROMPT}, *state.history[-6:], {"role": "user", "content": text}]
    try:
        res = llm.complete("router", messages, session_id=session_id, max_tokens=300, json_mode=True)
        return RouterOutput.model_validate(res.json())
    except (LLMError, ValidationError, ValueError):
        return rule_route(text)   # the app keeps working when a model misbehaves or the budget is spent


# --- handlers (S1 skeleton; S2–S6 replace the bodies) ------------------------------------------------------

def _apply_patch(state: SessionState, patch: dict) -> list[str]:
    """Apply only known, valid condition changes (a model can't set payment='free' or a negative radius)."""
    changed = []
    allowed = set(Conditions.model_fields) - {"center", "arrive_at"}
    for k, v in patch.items():
        if k not in allowed or getattr(state.conditions, k) == v:
            continue
        try:
            state.conditions = Conditions.model_validate({**state.conditions.model_dump(), k: v})
            changed.append(k)
        except ValidationError:
            pass
    return changed


def _conditions_event(state: SessionState) -> dict:
    c = state.conditions
    return {"area": c.center.label, "radius_km": c.radius_km, "budget_max": c.budget_max,
            "budget_label": f"تا {format_million(c.budget_max)}" if c.budget_max else None,
            "payment": c.payment, "fast_delivery": c.fast_delivery, "open": c.open}


def show_products(state: SessionState, em: Emitter, query: str) -> Iterator[str]:
    """S1 stub: plain FTS search so the flow is testable end to end. S2 swaps in the advisor (roles, seller floor)."""
    yield em.emit("status", {"text": "دارم مدل‌ها رو مقایسه می‌کنم…", "done": False})
    rows = store.search_products(_dataset(), query, limit=3)
    state.product_ids = [r["product_id"] for r in rows]
    state.last_block_kind = "products"
    yield em.emit("status", {"text": f"{to_fa_digits(len(rows))} مدل پیدا شد", "done": True})
    yield em.emit("product_cards", {"block_id": "products", "replace": True, "items": [
        {"product_id": r["product_id"], "title": r["title"], "sellers_total": r["sellers_total"],
         "price_min": r["price_min"], "price_max": r["price_max"]} for r in rows]})
    if not rows:
        yield em.emit("message", {"text": "توی داده‌ی ترب چیزی با این مشخصات پیدا نکردم. یه جور دیگه بگو؟"})


DIRECT_TOOLS: dict[str, Callable[..., Iterator[str]]] = {}


def direct(name: str):
    def deco(fn):
        DIRECT_TOOLS[name] = fn
        return fn
    return deco


@direct("show_products")
def _chip_show_products(state: SessionState, em: Emitter, query: str = "") -> Iterator[str]:
    yield from show_products(state, em, query)


_dataset_con = None


def _dataset():
    global _dataset_con
    if _dataset_con is None:
        _dataset_con = store.connect()
    return _dataset_con


def _turn(llm: LLMClient, state: SessionState, em: Emitter, text: str | None, chip: Chip | None) -> Iterator[str]:
    if chip and chip.kind == "direct_call":
        fn = DIRECT_TOOLS.get(chip.tool or "")
        if fn is None:
            yield em.emit("error", {"text": "این گزینه دیگه در دسترس نیست."})
            return
        yield from fn(state, em, **chip.args)
        return
    text = (chip.value if chip else text or "").strip()
    if not text:
        yield em.emit("error", {"text": "یه چیزی بنویس تا شروع کنیم."})
        return
    state.remember("user", text)
    out = understand(llm, state, text, em.session_id)
    if _apply_patch(state, out.conditions_patch):
        yield em.emit("conditions", _conditions_event(state))
    if out.intent in ("need", "named_product") and out.need:
        state.need = out.need
        if out.message:
            yield em.emit("message", {"text": out.message})
        query = out.need.named_product or " ".join(out.need.keywords) or out.need.product_type
        yield from show_products(state, em, query)
    elif out.intent == "refine":
        yield em.emit("message", {"text": out.message or "شرایط رو عوض کردم."})
    else:
        # call_shop / ask_results land in S3–S6
        yield em.emit("message", {"text": out.message or "بگو دنبال چه چیزی هستی تا برات پیدا کنم."})


def run_turn(llm: LLMClient, state: SessionState, em: Emitter, *, text: str | None = None,
             chip: Chip | None = None) -> Iterator[str]:
    """One user turn → SSE strings, always ending with `done` (unless the client disconnected)."""
    try:
        yield from _turn(llm, state, em, text, chip)
    except Exception as e:   # never leave the client hanging; the detail goes to the event log only
        yield em.emit("error", {"text": "یه مشکلی پیش اومد؛ دوباره امتحان کن.", "detail": type(e).__name__})
    yield em.emit("done", {})
