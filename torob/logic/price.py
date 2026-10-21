"""Price sanity rules (spec §5.3 rule 6 + amendment A6) and warranty wording.

Flags are computed once at ETL time. A flagged price is never shown in a price range, never used as the
cheapest price and never ranked first; the UI says «قیمت مشکوک کنار گذاشته شد».
"""

import json

from core.text import to_fa_digits

MIN_SANE = 1_000               # under this, toman prices are typos or placeholders (cheap parts cost a few thousand)
MAX_SANE = 10_000_000_000      # over 10 billion toman: junk (the export has values up to 7×10¹⁷)
PRODUCT_RATIO = 3              # vs the product's median, when it has ≥ 3 priced offers
MIN_OFFERS_FOR_PRODUCT_RULE = 3
# A category-median rule was tried and dropped: categories mix e.g. per-metre cable and 100 m rolls,
# so it flagged ~9.5k legitimate prices.


def flag_price(price: int | None, product_median: float | None, product_n: int) -> str:
    """ok | missing | suspicious_low | suspicious_high."""
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
    return "ok"


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
