"""AI-built filters: the shopper's words become concrete, removable filters over the category's real data.

One job per call. The model reads what the shopper said and a profile of the category (which spec fields its
products really have, their common values, the brands, the price range) and returns:
- `active`: filters the shopper asked for, or clearly implied (each a short Persian chip label, a kind and a one-line
  test); `must` = the product has to pass it, otherwise it only ranks higher;
- `suggest`: up to four more filters a careful salesperson would offer for this category.
Brand filters are applied in code over the whole category; spec filters are checked product by product by the
relevance judge (agents/relevance.py), which reads titles and specs. Prices stay with the budget strip.
"""

import json
import re
from collections import Counter
from functools import lru_cache

from core.llm import LLMClient
from torob import store

MAX_ACTIVE, MAX_SUGGEST = 5, 4

PROMPT = """You turn a Tehran shopper's request into concrete product filters for a shop catalogue.

The shopper said (oldest first): {said}
Product they want: {product}
Filters already on (keep the ones still wanted; the shopper may have just changed their mind): {current}

What this category's products really have (spec field → common values), brands and price range:
{profile}

Return JSON:
{{"active": [{{"label": "...", "kind": "brand" | "spec", "value": "...", "test": "...", "must": true | false}}],
  "suggest": [same shape, "must": true]}}

Rules:
- active: only what the shopper asked for or clearly implied by a stated purpose. «سامسونگ» → kind brand,
  value = the brand as the catalogue writes it. «گوشی برای بازی» → spec filters such as RAM 8 GB or more, a gaming-
  class chip, a high refresh rate (must=false: they rank, they don't exclude). «حتما 5G» → must=true.
- Do NOT make filters for price or budget (the app has its own budget buttons), nor for "original/genuine".
- label: Persian, 2–4 words, how a shop assistant would say it («رم ۸ گیگ به بالا», «5G», «دوربین ۵۰ مگاپیکسل+»,
  «سامسونگ»). Use Persian digits.
- test: one short English sentence a reader of the product title and specs can answer true/false
  ("RAM is at least 8 GB", "supports 5G", "brand is Samsung").
- suggest: 2–4 filters that really split this category's products (from the profile above), useful for this shopper,
  not already active. Empty when nothing useful.
- At most {max_active} active filters. Plain JSON, no prose."""


@lru_cache(maxsize=64)
def profile(categories: tuple[str, ...]) -> str:
    """The category as data: the 18 most common spec fields with their 4 most common values, the top brands and
    the 10th / 50th / 90th percentile prices. Cached per category set."""
    if not categories:
        return ""
    con = store.connect()   # its own connection: this runs on a background thread next to the request's queries
    try:
        q = ",".join("?" * len(categories))
        rows = con.execute(f"SELECT specs, brand, price_median FROM products WHERE category IN ({q}) AND redacted = 0",
                           categories).fetchall()
    finally:
        con.close()
    keys, vals, brands, prices = Counter(), {}, Counter(), []
    for specs, brand, price in rows:
        if brand:
            brands[brand] += 1
        if price:
            prices.append(price)
        try:
            items = json.loads(specs or "[]")
        except ValueError:
            continue
        for it in items:
            k, v = (it.get("key") or "").strip(), (it.get("value") or "").strip(" ,")
            if k and v and len(v) <= 40:
                keys[k] += 1
                vals.setdefault(k, Counter())[v] += 1
    lines = [f"- {k}: {' | '.join(v for v, _ in vals[k].most_common(4))}" for k, _ in keys.most_common(18)]
    prices.sort()
    pct = lambda f: prices[int(f * (len(prices) - 1))] if prices else 0
    lines.append("brands: " + "، ".join(b for b, _ in brands.most_common(15)))
    lines.append(f"prices (toman): 10% {pct(.1):,} · median {pct(.5):,} · 90% {pct(.9):,} · {len(rows)} products")
    return "\n".join(lines)


def _clean(items, n: int) -> list[dict]:
    out = []
    for f in items or []:
        if not isinstance(f, dict):
            continue
        kind = f.get("kind")
        label = " ".join(str(f.get("label") or "").split())[:40]
        test = " ".join(str(f.get("test") or "").split())[:160]
        value = " ".join(str(f.get("value") or "").split())[:40]
        if kind not in ("brand", "spec") or not label or not test or (kind == "brand" and not value):
            continue
        if any(x["label"] == label for x in out):
            continue
        out.append({"label": label, "kind": kind, "value": value, "test": test, "must": bool(f.get("must", kind == "brand"))})
        if len(out) >= n:
            break
    return out


def build(llm: LLMClient, said: list[str], product: str, categories: tuple[str, ...], current: list[dict],
          session_id: str) -> tuple[list[dict], list[dict]] | None:
    """(active, suggest), each with stable ids «f1…», or None when the model is off or its answer is unusable."""
    if not getattr(llm, "enabled", False) or not categories:
        return None
    cur = "; ".join(f"{f['label']} ({'must' if f['must'] else 'nice'})" for f in current) or "none"
    msg = PROMPT.format(said=" / ".join(s[:200] for s in said[-4:]), product=product or "?", current=cur,
                        profile=profile(categories), max_active=MAX_ACTIVE)
    try:
        res = llm.complete("judge", [{"role": "user", "content": msg}], session_id=session_id, max_tokens=900, json_mode=True)
        reply = res.json()
    except Exception:
        return None
    active, suggest = _clean(reply.get("active"), MAX_ACTIVE), _clean(reply.get("suggest"), MAX_SUGGEST)
    suggest = [f for f in suggest if all(f["label"] != a["label"] for a in active)]
    for i, f in enumerate(active + suggest, 1):
        f["id"] = f"f{i}"
    return active, suggest


def brand_ok(candidate, brands: list[str]) -> bool:
    """Brand filters run in code over the whole category: the listing's brand, else its title, names one of them."""
    from core.text import normalize_fa
    from torob.search import BRAND_ALIASES
    want = set()
    for b in brands:
        nb = normalize_fa(b)
        want |= {nb, *(v for k, v in BRAND_ALIASES.items() if k == nb.lower()), *(k for k, v in BRAND_ALIASES.items() if v == nb)}
    hay = normalize_fa(f"{candidate.brand or ''} {candidate.title}").lower()
    return any(w and re.search(rf"(^|\s){re.escape(w.lower())}(\s|$)", hay) for w in want)
