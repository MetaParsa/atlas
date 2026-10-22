"""Which found products are actually what the shopper asked for — judged by the model, not by word lists.

Full-text search finds everything that *mentions* the request: «کاور سیلیکونی هایلو X1 Neo» mentions earbuds, a phone
battery mentions the phone. One small call shows the model the shopper's own words and the top candidates (title and
category) and keeps only the ones that are the thing itself. Without a model, or if the answer is unusable, the
search's own rules stand (None = no judgement).
"""

from core.llm import LLMClient

MAX_ITEMS = 30
PROMPT = """A shopper in Tehran asked for: {want}

Products found in the shop catalogue (index | title | category):
{rows}

Return the indices of the products that ARE what the shopper asked for. Leave out accessories, spare parts, refills,
cases, covers, chargers, cables and other kinds of product, unless the shopper asked for that kind of item. Keep every
product that fits, whatever its brand or price; do not rank. Reply as JSON: {{"keep": [indices]}}"""


def keep(llm: LLMClient, want: str, cands: list, session_id: str) -> list | None:
    """The candidates the model says match `want` (order kept), or None when there is no usable judgement."""
    items = cands[:MAX_ITEMS]
    if not items or not want.strip():
        return None
    rows = "\n".join(f"{i} | {c.title} | {c.category}" for i, c in enumerate(items))
    try:
        res = llm.complete("judge", [{"role": "user", "content": PROMPT.format(want=want.strip(), rows=rows)}],
                           session_id=session_id, max_tokens=300, json_mode=True)
        idx = {int(i) for i in res.json().get("keep", []) if str(i).lstrip("-").isdigit()}
    except Exception:   # LLM off, budget spent, bad JSON: the rule-based result stands
        return None
    kept = [c for i, c in enumerate(items) if i in idx]
    return kept or None   # «nothing fits» is more likely a bad answer than a true one; don't empty the results
