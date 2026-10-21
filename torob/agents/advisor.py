"""Step 1 advisor (spec §6.3 + §0.2 A1, R7): need → 2–3 product picks, compared honestly.

Everything here is code: counts, prices, labels and the «چرا» line come from the data. Labels are assigned *after*
the picks are chosen, from facts about the shown cards («ارزان‌ترین» = cheapest of these cards), so a label can never
be false. A spec row appears only when ≥ 2 cards have it and the values differ; a card with < 2 spec lines shows none.
A named model («گوشی سامسونگ S25 FE») is matched directly so the router can skip to its sellers.
"""

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.text import format_million, format_price_short, normalize_fa, to_fa_digits
from torob import search, store
from torob.logic.geo import bbox, haversine_m
from torob.models import Chip, Conditions, Need

SELLER_FLOOR = 3          # A1: a pick needs ≥ 3 sellers nearby, relaxed to 2, then 1, saying so
OVER_BUDGET_SLACK = 0.10  # within 10% over budget is shown as «… ٪ بالاتر از بودجه، ولی …»
FRESH_DAYS = 30
_SKIP_SPEC_KEYS = {"نام محصول", "برند", "شرکت سازنده", "سازنده", "تاریخ معرفی", "مدل", "نام", "کد محصول"}
_UNKNOWN_VALUES = {"نامعلوم", "ثبت نشده", "-", "ندارد اطلاعات"}
LABELS = {"best_value": "بهترین ارزش", "cheapest": "ارزان‌ترین", "most_available": "بیشترین فروشنده",
          "other": "گزینه‌ی دیگر", "named": "همین مدل"}


@dataclass
class AreaStat:
    sellers: int = 0
    prices: list[int] = field(default_factory=list)   # sane registered prices of nearby offers
    fresh: int = 0                                     # nearby sane offers whose price changed in the last 30 days

    @property
    def median(self) -> int | None:
        return int(statistics.median(self.prices)) if self.prices else None


@dataclass
class Pick:
    c: search.Candidate
    role: str
    area: AreaStat
    in_area: bool
    over_budget_pct: int | None = None
    same_title_ids: list[str] = field(default_factory=list)
    spec_lines: list[str] = field(default_factory=list)
    why: str = ""

    @property
    def price(self) -> int:
        return (self.area.median if self.in_area else None) or self.c.price_median or 0

    def card(self) -> dict:
        area = self.in_area and bool(self.area.prices)
        return {
            "product_id": self.c.product_id, "title": self.c.title, "brand": self.c.brand, "image": self.c.image,
            "category": self.c.category, "role": self.role, "role_label": LABELS[self.role],
            "price_min": self.area.prices[0] if area else self.c.price_min,
            "price_max": self.area.prices[-1] if area else self.c.price_max,
            "price_scope": "area" if area else "tehran",   # «قیمت ثبت‌شده نزدیکت» vs «در کل تهران»
            "sellers_in_area": self.area.sellers, "sellers_total": self.c.sellers_total,
            "fresh_in_area": self.area.fresh, "over_budget_pct": self.over_budget_pct,
            "spec_lines": self.spec_lines, "same_title_variants": len(self.same_title_ids), "why": self.why,
        }


@dataclass
class AdvisorResult:
    picks: list[Pick]
    categories: list[str]
    notes: list[str]                     # honest caveats shown as one line («فقط دو فروشنده‌ی نزدیک…»)
    compare_rows: list[dict]             # [{key, values}] only for specs ≥ 2 cards share and that differ
    question: dict | None                # {text, chips}
    floor_used: int
    in_area: bool
    named: Pick | None = None            # the user named this exact model → router skips to shops
    n_candidates: int = 0                # for the status line: «۴۸ مدل پیدا شد → ۱۲ تا نزدیکت فروشنده دارن»
    n_nearby: int = 0


def _fresh_cutoff() -> str:
    exported = store.meta(store.get()).get("exported_at", "2026-10-14T00:00:00+03:30")
    return (datetime.fromisoformat(exported) - timedelta(days=FRESH_DAYS)).isoformat()


def area_stats(product_ids: list[str], cond: Conditions) -> dict[str, AreaStat]:
    """Per product, within the radius: distinct sellers, sane registered prices, freshly updated offers."""
    if not product_ids:
        return {}
    con = store.get()
    lat, lon = cond.center.lat, cond.center.lng
    sql = (f"SELECT o.product_id, o.shop_id, o.price_toman, o.price_flag, o.price_changed_at, s.lat, s.lon "
           f"FROM offers o JOIN shops s USING (shop_id) WHERE o.product_id IN ({','.join('?' * len(product_ids))})")
    args: list = list(product_ids)
    if cond.radius_km:
        la1, la2, lo1, lo2 = bbox(lat, lon, cond.radius_km)
        sql += " AND s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?"
        args += [la1, la2, lo1, lo2]
    cutoff = _fresh_cutoff()
    shops: dict[str, set] = {}
    out: dict[str, AreaStat] = {}
    for pid, shop, price, flag, changed, slat, slon in con.execute(sql, args).fetchall():
        if cond.radius_km and haversine_m(lat, lon, slat, slon) > cond.radius_km * 1000:
            continue
        st = out.setdefault(pid, AreaStat())
        shops.setdefault(pid, set()).add(shop)
        if flag == "ok":
            st.prices.append(price)
            if changed and datetime.fromisoformat(changed).isoformat() >= cutoff:
                st.fresh += 1
    for pid, st in out.items():
        st.sellers = len(shops[pid])
        st.prices.sort()
    return out


def spec_pairs(specs_json: str) -> list[tuple[str, str]]:
    try:
        items = json.loads(specs_json or "[]")
    except ValueError:
        return []
    out = []
    for it in items:
        k, v = (it.get("key") or "").strip(), (it.get("value") or "").strip()
        if k and v and k not in _SKIP_SPEC_KEYS and v not in _UNKNOWN_VALUES and len(v) <= 40:
            out.append((k, v))
    return out


def _named(need: Need, reps: list[search.Candidate], stats: dict[str, AreaStat], cond: Conditions) -> Pick | None:
    """The user's text contains a model code and a candidate's title has all of those codes."""
    text = " ".join(filter(None, [need.named_product, need.product_type, *need.keywords]))
    codes = search.model_tokens(text)
    if not codes:
        return None
    matches = [c for c in reps if all(code in search.title_tokens(c.title_norm) for code in codes)]
    if not matches:
        return None
    best = max(matches, key=lambda c: (stats.get(c.product_id, AreaStat()).sellers, c.sellers_total, -c.rank))
    st = stats.get(best.product_id, AreaStat())
    return Pick(best, "named", st, in_area=st.sellers > 0)


def pick_products(need: Need, cond: Conditions) -> AdvisorResult:
    cands, cats = search.candidates(need)
    notes: list[str] = []
    if not cands:
        return AdvisorResult([], cats, ["توی داده‌ی ترب چیزی با این مشخصات پیدا نکردم."], [], None, 0, False)

    # Same-title products: keep the one most sellers list, remember the rest («۳ نسخه با همین اسم»).
    groups: dict[str, list[search.Candidate]] = {}
    for c in sorted(cands, key=lambda c: c.rank):
        groups.setdefault(c.title_norm, []).append(c)
    reps, same_title = [], {}
    for g in groups.values():
        rep = max(g, key=lambda c: c.sellers_total)
        rep.rank = min(c.rank for c in g)
        reps.append(rep)
        same_title[rep.product_id] = [c.product_id for c in g if c is not rep]
    reps.sort(key=lambda c: c.rank)

    # The user named a brand («آیفون» = اپل): keep that brand when it has at least two candidates.
    text = " ".join(filter(None, [need.named_product, need.product_type, *need.keywords, *need.brand_pref]))
    named_brands = search.brands_in(text, {normalize_fa(c.brand) for c in reps if c.brand})
    if named_brands:
        branded = [c for c in reps if c.brand and normalize_fa(c.brand) in named_brands]
        if len(branded) >= 2:
            reps = branded

    stats = area_stats([c.product_id for c in reps[:150]], cond)
    named = _named(need, reps, stats, cond)
    budget = cond.budget_max or need.budget_max

    def area_price(c: search.Candidate) -> int:
        st = stats.get(c.product_id)
        return (st.median if st else None) or c.price_median or 0

    # A1 seller floor: ≥ 3 nearby, relax to 2, then 1; with nobody nearby, fall back to all Tehran and say so.
    pool, floor_used, in_area = [], SELLER_FLOOR, True
    for floor in (SELLER_FLOOR, 2, 1):
        pool = [c for c in reps if stats.get(c.product_id, AreaStat()).sellers >= floor]
        if budget:
            pool = [c for c in pool if area_price(c) <= budget * (1 + OVER_BUDGET_SLACK)]
        if len(pool) >= 2 or (floor == 1 and pool):
            floor_used = floor
            break
    if not pool:
        in_area = False
        pool = [c for c in reps if not budget or (c.price_median or 0) <= budget * (1 + OVER_BUDGET_SLACK)] or reps
        notes.append("نزدیک تو فروشنده‌ای برای این ثبت نشده؛ این‌ها از کل تهرانه.")
    elif floor_used < SELLER_FLOOR:
        notes.append("فقط یک فروشنده‌ی نزدیک ثبت کرده." if floor_used == 1 else "فقط دو فروشنده‌ی نزدیک ثبت کرده‌اند.")
    price = {c.product_id: (area_price(c) if in_area else c.price_median or 0) for c in pool}
    if budget and not any(p <= budget for p in price.values()):
        notes.append(f"چیزی زیر {format_price_short(budget)} پیدا نشد؛ نزدیک‌ترین‌ها به بودجه رو آوردم.")

    pool = pool[:40]   # keep relevance: only the best text matches compete
    avail = {c.product_id: stats.get(c.product_id, AreaStat()).sellers for c in pool}
    n = len(pool)
    rel = {c.product_id: 1 - i / max(1, n - 1) for i, c in enumerate(pool)}
    max_avail = max(avail.values()) or 1
    med_price = sorted(price.values())[len(price) // 2] or 1

    def value_score(c: search.Candidate) -> float:
        cheapness = min(1.0, med_price / max(1, price[c.product_id]))
        return 0.5 * rel[c.product_id] + 0.3 * math.log1p(avail[c.product_id]) / math.log1p(max_avail) + 0.2 * cheapness

    chosen: list[search.Candidate] = []
    for key in (lambda c: -value_score(c), lambda c: (price[c.product_id], c.rank), lambda c: (-avail[c.product_id], c.rank)):
        for c in sorted(pool, key=key):
            if all(not search.near_duplicate(c.title_norm, x.title_norm) for x in chosen):
                chosen.append(c)
                break
    picks = [Pick(c, "other", stats.get(c.product_id, AreaStat()), in_area, same_title_ids=same_title.get(c.product_id, []))
             for c in chosen]
    _label(picks)
    for p in picks:
        if budget and p.price > budget:
            p.over_budget_pct = max(1, round((p.price / budget - 1) * 100))
        pairs = spec_pairs(p.c.specs)
        p.spec_lines = [f"{k}: {v}" for k, v in pairs[:3]] if len(pairs) >= 2 else []   # R7: ≥ 2 lines or none
        p.why = why_line(p)
    if named:
        named.same_title_ids = same_title.get(named.c.product_id, [])
        named.why = why_line(named)
    return AdvisorResult(picks, cats, notes, _compare_rows(picks), None if named else _question(need, cond, price, picks),
                         floor_used, in_area, named, n_candidates=len(reps),
                         n_nearby=sum(1 for c in reps if stats.get(c.product_id, AreaStat()).sellers > 0))


def _label(picks: list[Pick]) -> None:
    """Truthful labels from the shown cards: the first pick is the value choice; «ارزان‌ترین» and «بیشترین فروشنده»
    go only to the card that really is cheapest / most available among these cards (ties → no label)."""
    if not picks:
        return
    picks[0].role = "best_value"
    rest = picks[1:]
    prices = [p.price for p in picks]
    sellers = [p.area.sellers if p.in_area else p.c.sellers_total for p in picks]
    for p in rest:
        i = picks.index(p)
        if prices[i] == min(prices) and prices.count(min(prices)) == 1:
            p.role = "cheapest"
        elif sellers[i] == max(sellers) and sellers.count(max(sellers)) == 1:
            p.role = "most_available"


def _compare_rows(picks: list[Pick]) -> list[dict]:
    """Spec rows that ≥ 2 cards have and whose values actually differ (others show «ثبت نشده»)."""
    if len(picks) < 2:
        return []
    per = [dict(spec_pairs(p.c.specs)) for p in picks]
    counts: dict[str, int] = {}
    for d in per:
        for k in d:
            counts[k] = counts.get(k, 0) + 1
    rows = []
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n < 2:
            continue
        vals = [d.get(k) for d in per]
        distinct = {normalize_fa(v).replace(" ", "") for v in vals if v}
        if len(distinct) >= 2:
            rows.append({"key": k, "values": [v or "ثبت نشده" for v in vals]})
        if len(rows) == 3:
            break
    return rows


def why_line(p: Pick) -> str:
    """One Persian line from the card's own facts (no LLM, no opinions, nothing invented)."""
    where = "نزدیکت" if p.in_area else "در کل تهران"
    sellers = p.area.sellers if p.in_area else p.c.sellers_total
    low = p.area.prices[0] if p.in_area and p.area.prices else p.c.price_min
    parts = []
    if p.over_budget_pct:
        parts.append(f"{to_fa_digits(p.over_budget_pct)}٪ بالاتر از بودجه، ولی")
    if p.role == "most_available":
        parts.append(f"بیشترین فروشنده {where} ({to_fa_digits(sellers)} تا)")
    else:
        if p.role == "cheapest":
            parts.append("ارزان‌ترین قیمت ثبت‌شده بین این‌ها ·")
        parts.append(f"{to_fa_digits(sellers)} فروشنده {where}")
    if low:
        parts.append(f"· قیمت ثبت‌شده از {format_price_short(low)}")
    if p.in_area and p.area.fresh:
        parts.append(f"· {to_fa_digits(p.area.fresh)} قیمت این ماه به‌روز شده")
    return " ".join(parts)


def _question(need: Need, cond: Conditions, price: dict, picks: list[Pick]) -> dict | None:
    """At most one splitting question, as direct_call chips (no LLM, no re-parsing)."""
    if not (cond.budget_max or need.budget_max) and len(price) >= 3:
        vals = sorted(v for v in price.values() if v)
        if vals and vals[-1] / vals[0] >= 3:
            tiers = sorted({_round_budget(vals[len(vals) // 4]), _round_budget(vals[len(vals) // 2]),
                            _round_budget(vals[3 * len(vals) // 4])})
            chips = [Chip(label=f"تا {format_price_short(t)}", kind="direct_call", tool="set_budget", args={"budget_max": t})
                     for t in tiers if t > 0]
            chips.append(Chip(label="فرقی نداره", kind="direct_call", tool="set_budget", args={"budget_max": None}))
            return {"text": "بودجه‌ت حدوداً چقدره؟", "chips": [c.model_dump() for c in chips]}
    brands = list(dict.fromkeys(p.c.brand for p in picks if p.c.brand))
    if not need.brand_pref and len(brands) >= 2:
        chips = [Chip(label=b, kind="direct_call", tool="prefer_brand", args={"brand": b}) for b in brands]
        chips.append(Chip(label="فرقی نداره", kind="direct_call", tool="prefer_brand", args={"brand": None}))
        return {"text": "برند خاصی مدنظرته؟", "chips": [c.model_dump() for c in chips]}
    return None


def _round_budget(v: int) -> int:
    """Round up to a friendly number: 1.37M → 1.5M, 380k → 400k."""
    step = 500_000 if v >= 1_000_000 else 50_000
    return int(math.ceil(v / step) * step)
