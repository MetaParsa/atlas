"""Product search for the advisor: query variants, category inference, candidates (spec §0.2 R12).

FTS5 (unicode61, normalised Persian) does the matching; this module decides *which* queries and *which*
categories. The category comes from where the hits land, favouring a category whose name starts with the request's
head word; an LLM's `Need.category_hints` may only narrow that choice, never add a category the hits don't support
(«شارژر آیفون» → «شارژر گوشی», not Lightning cables; «گوشی» → phones, not phone batteries).
"""

import re
import sqlite3
from dataclasses import dataclass

from core.text import normalize_fa, to_latin_digits
from torob import store
from torob.models import Need

# Everyday words → how products are titled in the data. Only widens a query; never replaces the user's words.
SYNONYMS = {
    "هندزفری": ["هدفون", "ایرباد"], "ایرپاد": ["هندزفری", "هدفون بیسیم"], "ایرباد": ["هندزفری", "هدفون"],
    "هدفون": ["هندزفری"], "کتونی": ["کتانی"], "کتانی": ["کتونی"], "گوشی": ["موبایل"], "موبایل": ["گوشی"],
    "آیفون": ["اپل"], "ایفون": ["اپل"], "اپل": ["آیفون"], "تایپ سی": ["type c"], "type c": ["تایپ سی"],
    "شارژر": ["آداپتور"], "پاور بانک": ["پاوربانک"], "پاوربانک": ["شارژر همراه"], "اسپیکر": ["بلندگو"],
    "دوربین مداربسته": ["دوربین مدار بسته"], "مداربسته": ["مدار بسته"], "لپتاپ": ["لپ تاپ"],
}
_SYN = {normalize_fa(k): [normalize_fa(v) for v in vs] for k, vs in SYNONYMS.items()}   # «آیفون» → «ایفون»
_GENERIC = set("برای با و یا که این اون یه یک مدل نوع خوب ارزون ارزان اصل اورجینال باشگاه ورزش خونه خانه کار".split())
# Accessories are titled and categorised after the thing they fit («کاور سیلیکونی هایلو X1 Neo», «کیف و کاور هدفون و
# هندزفری»), so a search for earbuds also hits their cases. They stay out unless the shopper asked for one.
ACCESSORY_WORDS = {normalize_fa(w) for w in "کیف کاور قاب گلس محافظ بند کیس استند پایه نگهدارنده".split()}
# Category names that are accessories *for* something; «کیف و کوله», «قطعات کامپیوتر», «لوازم التحریر» are products.
ACCESSORY_CATEGORIES = tuple(normalize_fa(p) for p in ("کیف و کاور", "کاور", "قاب", "گلس", "محافظ صفحه", "بند", "لوازم جانبی"))


def wants_accessory(need: Need) -> bool:
    """The shopper's own words (not the model's category hints) name an accessory: «کاور ایرپاد», «قاب آیفون»."""
    text = normalize_fa(" ".join(filter(None, [need.named_product, need.product_type, *need.keywords])))
    return bool(set(to_latin_digits(text).split()) & ACCESSORY_WORDS)


def is_accessory(title: str) -> bool:
    """A product title that starts with an accessory word: «کاور سیلیکونی هایلو X1 Neo»."""
    words = to_latin_digits(normalize_fa(title)).split()
    return bool(words) and words[0] in ACCESSORY_WORDS


def is_accessory_category(category: str) -> bool:
    """«کیف و کاور هدفون و هندزفری», «سایر لوازم جانبی هدفون و هندزفری» — accessories for the thing searched."""
    return normalize_fa(category).removeprefix(normalize_fa("سایر ")).startswith(ACCESSORY_CATEGORIES)


@dataclass
class Candidate:
    product_id: str
    title: str
    title_norm: str
    brand: str | None
    category: str
    image: str | None
    specs: str
    spec_count: int
    sellers_total: int
    price_min: int | None
    price_median: int | None
    price_max: int | None
    fresh_offers_30d: int
    rank: int              # 0 = best text match


def queries_for(need: Need) -> list[str]:
    """The user's own keywords first, then synonym-widened variants (deduped, max 4)."""
    base = [normalize_fa(k) for k in (need.keywords or [need.product_type]) if k and k.strip()]
    if need.named_product:
        base.insert(0, normalize_fa(need.named_product))
    for brand in need.brand_pref[:1]:   # a chosen brand also widens the search, so its products are found
        if brand:
            base = [f"{base[0]} {normalize_fa(brand)}"] + base if base else [normalize_fa(brand)]
    out: list[str] = []
    for q in base:
        out.append(q)
        padded = f" {q} "
        for word, alts in _SYN.items():
            if f" {word} " in padded:   # whole words only («اپل» must not hit «اپلیکیشن»)
                out.extend(padded.replace(f" {word} ", f" {alt} ").strip() for alt in alts)
    seen, uniq = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:4]


def head_word(need: Need) -> str:
    """First meaningful word of the request, e.g. «شارژر» in «شارژر اصل آیفون»."""
    text = normalize_fa(need.named_product or need.product_type or " ".join(need.keywords))
    for w in to_latin_digits(text).split():
        if w not in _GENERIC and not w.isdigit() and len(w) > 1:
            return w
    return ""


def hint_categories(con: sqlite3.Connection, hints: list[str], accessories: bool = True) -> list[str]:
    """LLM category hints → real category names (exact, then 'contains'), most offers first. With
    `accessories=False`, «کیف و کاور …» / «لوازم جانبی …» categories are skipped (the hint «هندزفری» matches both)."""
    found: list[str] = []
    for h in hints[:3]:
        hn = normalize_fa(h)
        if not hn:
            continue
        rows = con.execute("SELECT category FROM categories WHERE category_norm = ? OR category_norm LIKE ? "
                           "ORDER BY (category_norm = ?) DESC, offer_count DESC LIMIT 4",
                           (hn, f"%{hn}%", hn)).fetchall()
        found += [r[0] for r in rows if r[0] not in found and (accessories or not is_accessory_category(r[0]))][:2]
    return found[:2]


def infer_categories(hits: list[dict], head: str, max_cats: int = 2) -> list[str]:
    """Categories that dominate the top text matches; a category whose name starts with the head word gets a
    strong boost. Returns 1–2 categories covering most of the evidence."""
    score: dict[str, float] = {}
    for i, h in enumerate(hits):
        score[h["category"]] = score.get(h["category"], 0) + 1 / (1 + i / 10)   # earlier hits count more
    if head:
        for cat in score:
            if normalize_fa(cat).startswith(head):
                score[cat] *= 3
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    if not ranked:
        return []
    total = sum(score.values())
    cats = [ranked[0][0]]
    head_hit = bool(head) and normalize_fa(cats[0]).startswith(head)   # «گوشی» → «گوشی موبایل»: don't add cases
    if (not head_hit and len(ranked) > 1 and len(cats) < max_cats and ranked[1][1] >= 0.35 * ranked[0][1]
            and ranked[0][1] / total < 0.8):
        cats.append(ranked[1][0])
    return cats


_MODEL_TOKEN = re.compile(r"^(?=.*\d)(?=.*[a-z])[a-z0-9\-]{2,}$|^[a-z]{2,}\d+[a-z0-9]*$")
# «4G», «12V», «25A», «1TB», «20000mah» describe a spec, not a model.
_UNIT_TOKEN = re.compile(r"^\d+(?:[.,]\d+)?(?:gb|tb|mb|g|mah|ma|w|v|a|mp|hz|khz|mhz|k|m|cm|mm|kw|kva|va|l|inch|in)$")
# Variant words that make a different model: «S25» ≠ «S25 FE» ≠ «S25 Ultra».
_VARIANT_CANON = {"fe": "fe", "ultra": "ultra", "اولترا": "ultra", "الترا": "ultra", "plus": "plus", "پلاس": "plus",
                  "pro": "pro", "پرو": "pro", "max": "max", "مکس": "max", "lite": "lite", "لایت": "lite",
                  "mini": "mini", "مینی": "mini", "neo": "neo", "نیو": "neo", "edge": "edge", "prime": "prime",
                  "nc": "nc", "se": "se"}
VARIANT_WORDS = set(_VARIANT_CANON)


def variants(tokens: set[str]) -> set[str]:
    """Canonical variant words in a token set: «اولترا» and «Ultra» are the same variant."""
    return {_VARIANT_CANON[t] for t in tokens if t in _VARIANT_CANON}
BRAND_ALIASES = {"ایفون": "اپل", "iphone": "اپل", "apple": "اپل", "samsung": "سامسونگ", "xiaomi": "شیائومی",
                 "anker": "انکر"}


def model_tokens(text: str) -> list[str]:
    """Tokens that look like a model code: «S25», «A3949», «R50i», «EP-TA800»."""
    return [t for t in to_latin_digits(normalize_fa(text)).split() if _MODEL_TOKEN.match(t) and not _UNIT_TOKEN.match(t)]


def brands_in(text: str, known: set[str]) -> set[str]:
    """Brands the user named, matched against brands present in the candidates («آیفون» counts as «اپل»)."""
    toks = set(to_latin_digits(normalize_fa(text)).split())
    found = {b for b in known if b and b in toks}
    found |= {BRAND_ALIASES[t] for t in toks if t in BRAND_ALIASES and BRAND_ALIASES[t] in known}
    return found


def candidates(need: Need, limit_per_query: int = 60, wide: bool = False) -> tuple[list[Candidate], list[str]]:
    """Ranked candidate products (sane price only, no redacted titles) and the categories used.
    `wide` (a model will judge every candidate): also search the model's own category hints. Text hits alone can
    land in the wrong place («کفش دویدن» → women's college shoes) and the judge can't keep what was never fetched."""
    con = store.get()
    qs = queries_for(need)
    raw: list[dict] = []
    for q in qs:
        raw += store.search_products(con, q, limit=limit_per_query)
    acc = wants_accessory(need)
    if not acc:   # cases and covers don't get to vote for the category of an earbuds search
        raw = [h for h in raw if not is_accessory_category(h["category"])] or raw
    # The text hits decide the category; the model's hints may only narrow that choice. A hint alone matches by name
    # and lands on parts and accessories («گوشی موبایل» → «باتری گوشی موبایل», «ساعت هوشمند» → «شارژر ساعت هوشمند»).
    inferred = infer_categories(raw, head_word(need))
    hinted = hint_categories(con, need.category_hints, accessories=acc)
    cats = [c for c in inferred if c in hinted] or inferred or hinted
    fetch = list(dict.fromkeys(cats + hinted))[:4] if wide else cats   # `cats` stays the narrow, rule-picked set
    # Reciprocal rank fusion: a product found by several of the shopper's phrasings («ایرپاد پرو 2», «اپل ایرپادز پرو 2»)
    # outranks one that a single phrasing ranks high. Keeping only each product's best rank let four lists' top-10s
    # push the genuine AirPods Pro 2 (10th–19th in the Persian queries) past the 40 the relevance judge reads.
    fused: dict[str, float] = {}
    for q in qs:
        for c in fetch:
            for i, r in enumerate(store.search_products(con, q, limit=limit_per_query, category=c)):
                fused[r["product_id"]] = fused.get(r["product_id"], 0.0) + 1 / (10 + i)
    if not fused:
        return [], cats
    order = {pid: n for n, pid in enumerate(sorted(fused, key=lambda pid: -fused[pid]))}
    by_id = {pid: (n, None) for pid, n in order.items()}
    ids = list(by_id)
    rows = con.execute(
        f"SELECT product_id, title, title_norm, brand, category, image, specs, spec_count, sellers_total, price_min, "
        f"price_median, price_max, fresh_offers_30d FROM products WHERE product_id IN ({','.join('?' * len(ids))}) "
        f"AND price_median IS NOT NULL", ids).fetchall()
    out = [Candidate(**dict(r), rank=by_id[r["product_id"]][0]) for r in rows]
    if not acc:   # «کاور سیلیکونی …» in the earbuds category is still a case (kept only if nothing else matched)
        out = [c for c in out if not is_accessory(c.title_norm)] or out
    out.sort(key=lambda c: c.rank)
    return out, cats


def title_tokens(title_norm: str) -> set[str]:
    return {t for t in re.split(r"[\s\-_/|()،,]+", to_latin_digits(title_norm)) if len(t) > 1}


def near_duplicate(a: str, b: str, threshold: float = 0.85) -> bool:
    """Same or near-same title (token Jaccard) — e.g. the three «شارژر اپل توان 20 وات سه پین» products."""
    if a == b:
        return True
    ta, tb = title_tokens(a), title_tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= threshold
