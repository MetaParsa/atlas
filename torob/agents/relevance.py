"""Which found products are actually what the shopper asked for — judged by the model, not by word lists.

Full-text search finds everything that *mentions* the request: «کاور سیلیکونی هایلو X1 Neo» mentions earbuds, a phone
battery mentions the phone. One small call shows the model the shopper's own words and the top candidates (title,
category, typical price) and asks, product by product, what a sensible salesperson would show: the right kind of
product, meeting the explicit requirements, with soft wishes («خوب», «برای باشگاه») left to the ranking and the budget
left to the advisor (it filters with nearby prices and a 10% slack; a second, model-made budget cut conflicted). It also
flags likely copies («طرح», or a price far below the genuine product). Without a model, or with an unusable answer,
the search's own rules stand (None = no judgement).

A verdict per product, not one list of indices: on hard searches (wireless vs wired mouse, «AirPods Pro 2» vs Pro 3,
a budget class) the list form kept 18 wrong products of 33, the per-product form none (live test, 2026-10-22).
"""

from dataclasses import dataclass, field

from core.llm import LLMClient

MAX_ITEMS = 40   # the advisor ranks at most 40 anyway
PROMPT = """A shopper in Tehran asked: {want}

Products found in the shop catalogue (index | title | category | typical price in toman). Prices are current Tehran
prices (late 2026, after years of inflation), so judge prices against the other listings here, not from memory:
{rows}

For EACH product decide whether a sensible salesperson would show it for this request:
- no: a different kind of product, or an accessory / spare part / refill / case / charger / cable (unless the shopper
  asked for that kind of item);
- no: it clearly misses an explicit requirement: a named brand or model or generation, wireless vs wired, a size or
  capacity, or a stated form ("small, for travel" rules out large home or party speakers);
- yes otherwise. Price and budget are NOT your concern (the app filters them with nearby prices). Soft wishes
  ("good", "cheap", "for the gym") never make it a no.
Also mark copy=true when it looks like a copy or imitation of a brand-name product: the title says طرح / کپی / فیک,
or its price is a small fraction of other listings of the same model in this list.

Reply as JSON: {{"items": [{{"i": index, "show": true|false, "copy": true|false}}, ...]}} covering every index."""


@dataclass
class Judgement:
    kept: list
    copies: set[str] = field(default_factory=set)   # product_ids that look like copies


def keep(llm: LLMClient, want: str, cands: list, session_id: str) -> Judgement | None:
    """The candidates the model says match `want` (order kept) and the likely copies, or None (no usable judgement)."""
    items = cands[:MAX_ITEMS]
    if not items or not want.strip():
        return None
    rows = "\n".join(f"{i} | {c.title} | {c.category} | {c.price_median or '?'}" for i, c in enumerate(items))
    try:
        res = llm.complete("judge", [{"role": "user", "content": PROMPT.format(want=want.strip(), rows=rows)}],
                           session_id=session_id, max_tokens=1500, json_mode=True)
        verdicts = {int(v["i"]): v for v in res.json().get("items", []) if str(v.get("i", "")).isdigit()}
    except Exception:   # LLM off, budget spent, bad JSON: the rule-based result stands
        return None
    kept = [c for i, c in enumerate(items) if verdicts.get(i, {}).get("show") is True]
    if not kept:        # «nothing fits» is more likely a bad answer than a true one; don't empty the results
        return None
    copies = {c.product_id for i, c in enumerate(items) if verdicts.get(i, {}).get("show") is True
              and verdicts[i].get("copy") is True}
    return Judgement(kept, copies)
