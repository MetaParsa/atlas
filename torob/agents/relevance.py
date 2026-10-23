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
RESEARCH_ITEMS, BATCH = 120, 20   # with filters to check, the judge reads this many, in small parallel batches
PROMPT = """A shopper in Tehran asked: {want}

Products found in the shop catalogue (index | title | category | typical price in toman | key specs). Prices are current Tehran
prices (late 2026, after years of inflation), so judge prices against the other listings here, not from memory:
{rows}

For EACH product decide whether a sensible salesperson would show it for this request:
- no: a different kind of product, or an accessory / spare part / refill / case / charger / cable (unless the shopper
  asked for that kind of item);
- no: it is not an identifiable product a shop sells as one item: a vague bundle or "collection of accessories", a
  generic title with no brand, model or concrete product type, a service, course, gift card or promo listing;
- no: it clearly misses an explicit requirement: a named brand or model or generation, wireless vs wired, a size or
  capacity, or a stated form ("small, for travel" rules out large home or party speakers);
- yes otherwise. Do not filter on the budget (the app does that with nearby prices), but do use price as evidence
  of what a product is (a speaker costing ten times the portable ones is a large home or party speaker). Soft wishes
  ("good", "cheap", "for the gym") never make it a no.
- no, with fake=true: the title says it is the product but the listing clearly isn't: its price is a small fraction
  (under about a fifth) of the other listings of the same kind here, i.e. priced like a spare part, case or board
  (a «گوشی موبایل» at 4.5 million next to phones at 40–180 million is a mislabeled part or a wrong listing).
Also mark copy=true when it looks like a copy or imitation of a brand-name product: the title says طرح / کپی / فیک,
its price is under about half of other listings of the same model in this list, it costs less than a used one or a
single spare part (one earbud, a case) of the same model in this list, or a premium brand's item (Apple, Samsung
flagship, JBL, Sony, Bose) is priced like the no-name products in this list. When the shopper asks for the
genuine thing («اصل», «اورجینال»), a copy is show=false.

Also give each product "fit" from 0 to 3: how well it serves what this shopper wants it FOR and their soft wishes
("for gaming", "for the gym", "good camera", "for my old father", "long battery"), judged from its title, specs and
price as an honest salesperson would. For gaming phones that means a strong chip, plenty of RAM (8 GB+), a high-refresh
screen and a big battery; a budget entry phone is fit 0 or 1 for gaming even if it is a phone. When the shopper gave no
purpose or wish, every product is fit 1.

Finally suggest "budget_steps": three budgets in toman, ascending (entry, middle, generous), that a typical shopper
with this request would pick between, judged from the prices of the products you marked show=true and from who it is
for (an everyday item or a gift doesn't need to reach the most expensive listings).

{criteria}Reply as compact JSON, one short row per product, covering every index:
{{"rows": [[index, show 1|0, copy 1|0, fake 1|0, fit 0-3{pass_shape}], ...], "budget_steps": [a, b, c]}}"""

CRITERIA = """The shopper's filters, in this order. For EACH product answer each one from its title and specs: T (yes),
F (no) or ? (the title and specs don't say; never guess a spec that isn't written), as one string in filter order:
{lines}

"""


@dataclass
class Judgement:
    kept: list
    copies: set[str] = field(default_factory=set)   # product_ids that look like copies
    budget_steps: list[int] = field(default_factory=list)   # the model's budget buttons (the advisor clamps them)
    fit: dict[str, int] = field(default_factory=dict)        # product_id -> 0..3, how well it serves the stated purpose
    fakes: list[str] = field(default_factory=list)            # titles that claim to be the item but aren't (hidden, told)
    passes: dict[str, dict] = field(default_factory=dict)     # product_id -> {filter id: True | False | None}


def _verdicts(reply: dict, n: int, criteria: list[dict]) -> dict[int, dict]:
    """Compact rows ([i, show, copy, fake, fit, "TF?"]) or the older {"items": [{"i", "show", …}]} shape → per index."""
    out = {}
    for r in reply.get("rows") or []:
        if not isinstance(r, list) or not r or not str(r[0]).isdigit() or int(r[0]) >= n:
            continue
        g = lambda k: r[k] if len(r) > k else None
        marks = str(g(5) or "")
        out[int(r[0])] = {"show": g(1) in (1, True, "1"), "copy": g(2) in (1, True, "1"), "fake": g(3) in (1, True, "1"),
                          "fit": g(4) if isinstance(g(4), (int, float)) else None,
                          "pass": {f["id"]: {"T": True, "F": False}.get(marks[k].upper()) if k < len(marks) else None
                                   for k, f in enumerate(criteria)}}
    for x in reply.get("items") or []:
        if isinstance(x, dict) and str(x.get("i", "")).isdigit() and int(x["i"]) < n:
            p = x.get("pass") if isinstance(x.get("pass"), dict) else {}
            out[int(x["i"])] = {**x, "pass": {f["id"]: p.get(f["id"]) if isinstance(p.get(f["id"]), bool) else None for f in criteria}}
    return out


def keep(llm: LLMClient, want: str, cands: list, session_id: str, criteria: list[dict] | None = None) -> Judgement | None:
    """The candidates the model says match `want` (order kept) and the likely copies, or None (no usable judgement).
    An empty `kept` is a real answer when the model gave a verdict on (nearly) every candidate: none of them fits.
    `criteria` (spec filters from agents/filters.py): every product is also checked against each, and the judge then
    reads up to 120 products in parallel batches, so the whole category is researched, not just the top text hits."""
    criteria = [c for c in criteria or [] if c.get("kind") == "spec"]
    items = cands[:RESEARCH_ITEMS if criteria else MAX_ITEMS]
    if not items or not want.strip():
        return None
    from concurrent.futures import ThreadPoolExecutor

    from torob.agents.advisor import spec_pairs   # (late: advisor imports this module)
    specs = lambda c: "; ".join(f"{k}: {v}" for k, v in spec_pairs(getattr(c, "specs", ""))[:8 if criteria else 5])[:260 if criteria else 160]
    crit = (CRITERIA.format(lines="\n".join(f"- {c['id']}: {c['test']}" for c in criteria)) if criteria else "")
    shape = f', "{"T" * len(criteria)}" (one of T/F/? per filter)' if criteria else ""

    def one(batch: list) -> tuple[dict, list[int]] | None:
        rows = "\n".join(f"{i} | {c.title} | {c.category} | {c.price_median or '?'} | {specs(c)}" for i, c in enumerate(batch))
        try:
            res = llm.complete("judge", [{"role": "user", "content": PROMPT.format(want=want.strip(), rows=rows, criteria=crit,
                                                                                   pass_shape=shape)}],
                               session_id=session_id, max_tokens=1500 + 30 * len(batch) * len(criteria), json_mode=True)
            reply = res.json()
            v = _verdicts(reply, len(batch), criteria)
            steps = [int(x) for x in (reply.get("budget_steps") or []) if isinstance(x, (int, float)) and x > 0][:3]
        except Exception:   # LLM off, budget spent, bad JSON: this batch has no verdict
            return None
        return v, steps

    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        answers = list(pool.map(one, batches))
    if answers[0] is None:   # the first batch holds the best text matches: without it there is no judgement
        return None
    verdicts, steps = {}, answers[0][1]
    for b, (batch, ans) in enumerate(zip(batches, answers)):
        if ans:
            for i, v in ans[0].items():
                verdicts[b * BATCH + i] = v
    judged = [i for i in range(len(items)) if (i // BATCH) < len(answers) and answers[i // BATCH]]
    kept = [c for i, c in enumerate(items) if verdicts.get(i, {}).get("show") is True]
    if not kept and len(verdicts) < 0.8 * len(judged):   # an empty answer that skipped items is a bad answer, not a «no»
        return None
    copies = {c.product_id for i, c in enumerate(items) if verdicts.get(i, {}).get("show") is True
              and verdicts[i].get("copy") is True}
    fit, passes = {}, {}
    for i, c in enumerate(items):
        vi = verdicts.get(i, {})
        if vi.get("show") is True and isinstance(vi.get("fit"), (int, float)):
            fit[c.product_id] = max(0, min(3, int(vi["fit"])))
        if criteria and vi.get("show") is True:
            passes[c.product_id] = vi.get("pass") or {f["id"]: None for f in criteria}
    fakes = [c.title for i, c in enumerate(items) if verdicts.get(i, {}).get("fake") is True]
    return Judgement(kept, copies, steps, fit, fakes, passes)
