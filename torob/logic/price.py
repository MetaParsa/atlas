"""Price sanity rules (spec §5.3 rule 6 + amendment A6) and warranty wording.

Flags are computed once at ETL time. A flagged price is never shown in a price range, never used as the
cheapest price and never ranked first; the UI says «قیمت مشکوک کنار گذاشته شد».
"""

import json
import re

from core.text import normalize_fa, to_fa_digits

MIN_SANE = 1_000               # under this, toman prices are typos or placeholders (cheap parts cost a few thousand)
MAX_SANE = 10_000_000_000      # over 10 billion toman: junk (the export has values up to 7×10¹⁷)
PRODUCT_RATIO = 3              # vs the product's median, when it has ≥ 3 priced offers
MIN_OFFERS_FOR_PRODUCT_RULE = 3
# Narrow rules for products with < 3 priced offers (decided 2026-10-22, recorded in spec §0.2 R12):
# a plain category-median ratio was tried and dropped (categories mix per-metre cable and 100 m rolls, so it
# flagged ~9.5k real prices). Only the extremes are flagged: a few thousand toman in a category whose typical
# price is in the millions (e.g. an inverter at 3,000), or > 1 billion in a category typically under 10 million.
LOW_ABS, LOW_CAT_MEDIAN = 10_000, 1_000_000
HIGH_ABS, HIGH_CAT_MEDIAN = 1_000_000_000, 10_000_000


def flag_price(price: int | None, product_median: float | None, product_n: int,
               category_median: float | None = None) -> str:
    """ok | missing | suspicious_low | suspicious_high. The ETL calls this for every offer (single source of truth)."""
    if price is None:
        return "missing"
    if price < MIN_SANE:
        return "suspicious_low"
    if price > MAX_SANE:
        return "suspicious_high"
    if product_n >= MIN_OFFERS_FOR_PRODUCT_RULE and product_median:
        if price > PRODUCT_RATIO * product_median:
            return "suspicious_high"
        if price < product_median / PRODUCT_RATIO:
            return "suspicious_low"
    elif category_median:
        if price < LOW_ABS and category_median > LOW_CAT_MEDIAN:
            return "suspicious_low"
        if price > HIGH_ABS and category_median < HIGH_CAT_MEDIAN:
            return "suspicious_high"
    return "ok"


_PER_METRE = re.compile(r"قیمت\s*(?:هر\s*)?متر(?:ی)?(?![ء-ی])|هر\s*متر(?![ء-ی])|\(\s*متری\s*\)")


def is_per_metre(seller_title: str | None) -> bool:
    """Only explicit per-metre pricing («قیمت متری», «قیمت هر متر») — not «۱۰۰ متری» rolls or «۳.۵ میلی‌متری» jacks."""
    return bool(seller_title) and bool(_PER_METRE.search(normalize_fa(seller_title)))


def warranty_text(raw: str | None) -> str | None:
    """{"provider":"شرکتی","period_months":18,...} -> «۱۸ ماه شرکتی»; provider or description as written otherwise."""
    if not raw:
        return None
    w = json.loads(raw)
    provider, months, desc = w.get("provider"), w.get("period_months"), w.get("description")
    if months and provider:
        return f"{to_fa_digits(months)} ماه {provider}"
    if months:
        return f"{to_fa_digits(months)} ماه گارانتی"
    return provider or desc or None
