"""Which found products are actually what the shopper asked for — judged by the model, not by word lists.

Full-text search finds everything that *mentions* the request: «کاور سیلیکونی هایلو X1 Neo» mentions earbuds, a phone
battery mentions the phone. One small call shows the model the shopper's own words (and budget) and the top candidates
(title, category, typical price) and asks what a sensible salesperson would show: the right kind of product, meeting
the explicit requirements, with soft wishes («خوب», «برای باشگاه») left to the ranking. It also flags likely copies
(«طرح», or a price far below the genuine product). Without a model, or with an unusable answer, the search's own rules
stand (None = no judgement).
"""

from dataclasses import dataclass, field

from core.llm import LLMClient

MAX_ITEMS = 40   # the advisor ranks at most 40 anyway
PROMPT = """A shopper in Tehran asked: {want}

Products found in the shop catalogue (index | title | category | typical price in toman):
{rows}

1. "keep": the indices a sensible salesperson would show for this request.
   - Drop other kinds of product, and accessories, spare parts, refills, cases, chargers or cables, unless the shopper
     asked for that kind of item.
   - Drop products that clearly miss an explicit requirement: a named brand or model, a size or capacity, a stated form
     ("small, for travel" rules out a large home speaker), or the budget (far above it, or so far below it that it is
     clearly a different class, e.g. a 2-million basic phone for a 20-million budget).
   - Do NOT drop for soft wishes such as "good", "cheap", "for the gym" or a use case. Keep every product of the right
     kind that meets the explicit requirements, whatever its brand. Do not rank.
2. "copies": among the kept indices, the ones that look like a copy or imitation of a brand-name product (the title says
   طرح, کپی, های کپی or فیک, or the price is far below what the genuine product costs in Iran).

Reply as JSON: {{"keep": [indices], "copies": [indices]}}"""


@dataclass
class Judgement:
    kept: list
    copies: set[str] = field(default_factory=set)   # product_ids that look like copies


def _ids(values) -> set[int]:
    return {int(i) for i in values or [] if str(i).lstrip("-").isdigit()}


def keep(llm: LLMClient, want: str, cands: list, session_id: str) -> Judgement | None:
    """The candidates the model says match `want` (order kept) and the likely copies, or None (no usable judgement)."""
    items = cands[:MAX_ITEMS]
    if not items or not want.strip():
        return None
    rows = "\n".join(f"{i} | {c.title} | {c.category} | {c.price_median or '?'}" for i, c in enumerate(items))
    try:
        res = llm.complete("judge", [{"role": "user", "content": PROMPT.format(want=want.strip(), rows=rows)}],
                           session_id=session_id, max_tokens=400, json_mode=True)
        out = res.json()
        keep_idx, copy_idx = _ids(out.get("keep")), _ids(out.get("copies"))
    except Exception:   # LLM off, budget spent, bad JSON: the rule-based result stands
        return None
    kept = [c for i, c in enumerate(items) if i in keep_idx]
    if not kept:        # «nothing fits» is more likely a bad answer than a true one; don't empty the results
        return None
    return Judgement(kept, {c.product_id for i, c in enumerate(items) if i in copy_idx and i in keep_idx})
