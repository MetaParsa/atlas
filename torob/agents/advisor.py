"""Step 1 advisor (spec §6.3 + §0.2 A1, R7): need → 2–3 product picks, compared honestly.

Everything here is code: counts, prices, labels and the «چرا» line come from the data. Labels are assigned *after*
the picks are chosen, from facts about the shown cards, so a label can never be false. A spec row appears only when
≥ 2 cards have it and the values differ; a card with < 2 spec lines shows none. A named model («گوشی سامسونگ S25 FE»)
is matched directly so the router can skip to its sellers. Notes describe the cards actually shown.
"""

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.text import format_price_short, normalize_fa, to_fa_digits
from torob import search, store
from torob.logic.geo import bbox, haversine_m
from torob.models import Chip, Conditions, Need

SELLER_FLOOR = 3          # A1: a pick needs ≥ 3 sellers nearby, relaxed to 2, then 1, saying so
OVER_BUDGET_SLACK = 0.10  # up to 10% over budget is shown («۷٪ بالاتر از بودجه، ولی …»); more is not shown as a fit
FRESH_DAYS = 30
_SKIP_SPEC_KEYS = {"نام محصول", "برند", "شرکت سازنده", "سازنده", "تاریخ معرفی", "مدل", "نام", "کد محصول"}
_UNKNOWN_VALUES = {"نامعلوم", "ثبت نشده", "-", "ندارد اطلاعات"}
LABELS = {"best_value": "بهترین ارزش", "cheapest": "ارزان‌ترین", "most_available": "بیشترین فروشنده",
          "closest_match": "نزدیک‌ترین به درخواستت", "other": "گزینه‌ی دیگر", "named": "همین مدل"}


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
    in_area: bool            # sellers counted within the chosen radius
    whole_city: bool = False # the radius is «کل تهران», so «نزدیکت» would be wrong
    over_budget_pct: int | None = None
    same_title_ids: list[str] = field(default_factory=list)
    spec_lines: list[str] = field(default_factory=list)
    why: str = ""
    copy_suspect: bool = False   # the model thinks it's a copy («طرح») of a brand-name product

    @property
    def area_priced(self) -> bool:
        return self.in_area and bool(self.area.prices)

    @property
    def price(self) -> int:
        """Typical registered price used for comparing and budget: nearby median, else Tehran median."""
        return (self.area.median if self.area_priced else None) or self.c.price_median or 0

    @property
    def low(self) -> int | None:
        return self.area.prices[0] if self.area_priced else self.c.price_min

    @property
    def where(self) -> str:
        return "در تهران" if self.whole_city or not self.in_area else "نزدیکت"

    def card(self) -> dict:
        priced = self.area_priced
        return {
            "product_id": self.c.product_id, "title": self.c.title, "brand": self.c.brand, "image": self.c.image,
            "category": self.c.category, "role": self.role, "role_label": LABELS[self.role],
            "price_min": self.area.prices[0] if priced else self.c.price_min,
            "price_max": self.area.prices[-1] if priced else self.c.price_max,
            "price_scope": "area" if priced and not self.whole_city else "tehran",
            "sellers_in_area": self.area.sellers, "sellers_total": self.c.sellers_total,
            "fresh_in_area": self.area.fresh, "over_budget_pct": self.over_budget_pct,
            "spec_lines": self.spec_lines, "same_title_variants": len(self.same_title_ids), "why": self.why,
            "copy_suspect": self.copy_suspect,
        }


@dataclass
class AdvisorResult:
    picks: list[Pick]
    categories: list[str]
    notes: list[str]
    compare_rows: list[dict]
    question: dict | None
    floor_used: int
    in_area: bool
    named: Pick | None = None
    n_candidates: int = 0
    n_nearby: int = 0
    n_matching: int | None = None   # how many the model judged to be the thing asked for (None = no model)


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
    """The user's text has a model code, and a title has all those codes and no variant word they didn't say."""
    text = " ".join(filter(None, [need.named_product, need.product_type, *need.keywords]))
    codes = search.model_tokens(text)
    if not codes:
        return None
    asked = search.title_tokens(normalize_fa(text))
    # A named brand must be in the title too: the data tags e.g. a «یونیوا S25» with brand «سامسونگ».
    brands = search.brands_in(text, {normalize_fa(c.brand) for c in reps if c.brand})
    want = search.variants(asked)
    matches = [c for c in reps if all(code in (tt := search.title_tokens(c.title_norm)) for code in codes)
               and search.variants(tt) == want            # «S25» ≠ «S25 FE»; «S25 اولترا» = «S25 Ultra»
               and (not brands or any(b in c.title_norm for b in brands))]
    if not matches:
        return None
    best = max(matches, key=lambda c: (stats.get(c.product_id, AreaStat()).sellers, c.sellers_total, -c.rank))
    st = stats.get(best.product_id, AreaStat())
    return Pick(best, "named", st, in_area=st.sellers > 0, whole_city=cond.radius_km is None)


def _dedupe(cands: list[search.Candidate]) -> tuple[list[search.Candidate], dict[str, list[str]]]:
    """Same-title products: keep the one most sellers list, remember the rest («۳ نسخه با همین اسم»)."""
    groups: dict[str, list[search.Candidate]] = {}
    for c in sorted(cands, key=lambda c: c.rank):
        groups.setdefault(c.title_norm, []).append(c)
    reps, same = [], {}
    for g in groups.values():
        rep = max(g, key=lambda c: c.sellers_total)
        rep.rank = min(c.rank for c in g)
        reps.append(rep)
        same[rep.product_id] = [c.product_id for c in g if c is not rep]
    reps.sort(key=lambda c: c.rank)
    return reps, same


def _brand_filter(need: Need, reps: list[search.Candidate], notes: list[str]) -> list[search.Candidate]:
    """A chosen brand (chip) or a brand named in the text («آیفون» = اپل) narrows the picks; say so if it can't."""
    known = {normalize_fa(c.brand) for c in reps if c.brand}
    wanted = {normalize_fa(b) for b in need.brand_pref if b}
    if not wanted:
        text = " ".join(filter(None, [need.named_product, need.product_type, *need.keywords]))
        wanted = search.brands_in(text, known)
    if not wanted:
        return reps
    branded = [c for c in reps if c.brand and normalize_fa(c.brand) in wanted]
    if need.brand_pref and len(branded) < 2:
        names = "، ".join(need.brand_pref)
        notes.append(f"از برند {names} چیز زیادی ثبت نشده" + ("؛ همین یکی رو آوردم." if branded else "؛ بقیه رو آوردم."))
    return branded if branded and (need.brand_pref or len(branded) >= 2) else reps


def pick_products(need: Need, cond: Conditions, judge=None) -> AdvisorResult:
    """`judge(candidates) -> kept | None`: the model's relevance check (agents/relevance.py), when there is a model."""
    cands, cats = search.candidates(need, wide=judge is not None)   # a judge sees a wider net (the model's categories too)
    notes: list[str] = []
    if not cands:
        return AdvisorResult([], cats, ["توی داده‌ی ترب چیزی با این مشخصات پیدا نکردم."], [], None, 0, False)

    reps, same_title = _dedupe(cands)
    reps = _brand_filter(need, reps, notes)
    n_found, n_matching, copies, steps = len(reps), None, set(), []
    if judge:
        j = judge(reps)
        if j is None:     # no usable judgement: back to the narrow, rule-picked categories
            reps = [c for c in reps if c.category in cats] or reps
        elif not j.kept:  # the model looked at each one and none is the thing asked for: say so, don't show rejects
            return AdvisorResult([], cats, ["توی داده‌ی ترب چیزی که دقیقاً همین باشه پیدا نکردم؛ یه جور دیگه بگو یا عکسش رو بفرست."],
                                 [], None, 0, False, n_candidates=n_found, n_matching=0)
        else:             # only the products that are the thing asked for compete
            reps, copies, n_matching, steps = j.kept, j.copies, len(j.kept), j.budget_steps
    stats = area_stats([c.product_id for c in reps[:150]], cond)
    named = _named(need, reps, stats, cond)
    budget = cond.budget_max          # the strip is the single source of truth (the router copies need.budget_max)
    whole_city = cond.radius_km is None

    def typical(c: search.Candidate) -> int:
        st = stats.get(c.product_id)
        return (st.median if st else None) or c.price_median or 0

    # A1 seller floor on availability alone: ≥ 3 nearby, relax to 2, then 1; nobody nearby → all Tehran.
    in_area, floor_used, pool = True, SELLER_FLOOR, []
    for floor in (SELLER_FLOOR, 2, 1):
        pool = [c for c in reps if stats.get(c.product_id, AreaStat()).sellers >= floor]
        if len(pool) >= 2 or (floor == 1 and pool):
            floor_used = floor
            break
    if not pool:
        in_area, pool = False, reps
        notes.append("نزدیک تو فروشنده‌ای برای این ثبت نشده؛ این‌ها از کل تهرانه.")

    price = {c.product_id: (typical(c) if in_area else c.price_median or 0) for c in pool}
    question = None
    if budget:
        fits = [c for c in pool if price[c.product_id] <= budget * (1 + OVER_BUDGET_SLACK)]
        if fits:
            pool = fits
        else:   # nothing fits: show the cheapest real options and offer a realistic budget, never «۵۹۴۰۰٪ بالاتر»
            pool = sorted(pool, key=lambda c: price[c.product_id])[:2]
            cheapest = price[pool[0].product_id]
            notes.append(f"چیزی زیر {format_price_short(budget)} پیدا نشد؛ ارزان‌ترین {'نزدیکت' if in_area else 'در تهران'} "
                         f"حدود {format_price_short(cheapest)} است.")
            target = _round_budget(cheapest)
            question = {"text": f"بودجه رو تا {format_price_short(target)} ببرم بالا؟", "chips": [
                Chip(label=f"آره، تا {format_price_short(target)}", kind="direct_call", tool="set_budget",
                     args={"budget_max": target}).model_dump(),
                Chip(label="نه", kind="direct_call", tool="noop", args={}).model_dump()]}

    pool = pool[:40]   # keep relevance: only the best text matches compete
    avail = {c.product_id: stats.get(c.product_id, AreaStat()).sellers for c in pool}
    n = len(pool)
    rel = {c.product_id: 1 - i / max(1, n - 1) for i, c in enumerate(pool)}
    max_avail = max(avail.values()) or 1
    med_price = sorted(price[c.product_id] for c in pool)[n // 2] or 1

    def value_score(c: search.Candidate) -> float:
        cheapness = min(1.0, med_price / max(1, price[c.product_id]))
        return 0.5 * rel[c.product_id] + 0.3 * math.log1p(avail[c.product_id]) / math.log1p(max_avail) + 0.2 * cheapness

    chosen: list[search.Candidate] = []
    for key in (lambda c: -value_score(c), lambda c: (price[c.product_id], c.rank), lambda c: (-avail[c.product_id], c.rank)):
        for c in sorted(pool, key=key):
            if all(not search.near_duplicate(c.title_norm, x.title_norm) for x in chosen):
                chosen.append(c)
                break
    picks = [Pick(c, "other", stats.get(c.product_id, AreaStat()), in_area, whole_city,
                  same_title_ids=same_title.get(c.product_id, [])) for c in chosen]
    _label(picks)
    for p in [*picks, *([named] if named else [])]:
        if budget and p.price > budget:
            pct = round((p.price / budget - 1) * 100)
            p.over_budget_pct = max(1, pct) if pct <= OVER_BUDGET_SLACK * 100 else None
        pairs = spec_pairs(p.c.specs)
        p.spec_lines = [f"{k}: {v}" for k, v in pairs[:3]] if len(pairs) >= 2 else []   # R7: ≥ 2 lines or none
    if named:
        named.same_title_ids = same_title.get(named.c.product_id, [])
    for p in [*picks, *([named] if named else [])]:
        p.copy_suspect = p.c.product_id in copies
    for p in [*picks, *([named] if named else [])]:
        p.why = why_line(p, budget)

    if in_area and picks and not named and min(p.area.sellers for p in picks) < SELLER_FLOOR:   # about the cards shown
        m = min(p.area.sellers for p in picks)
        notes.append("بعضی از این‌ها فقط یک فروشنده‌ی نزدیک دارن." if m == 1 else
                     "بعضی از این‌ها فقط دو فروشنده‌ی نزدیک دارن.")
    if question is None and not named:
        question = _question(need, cond, price, picks, steps)
    return AdvisorResult(picks, cats, notes, _compare_rows(picks), None if named else question,
                         floor_used, in_area, named, n_candidates=n_found, n_matching=n_matching,
                         n_nearby=sum(1 for c in reps if stats.get(c.product_id, AreaStat()).sellers > 0))


def _label(picks: list[Pick]) -> None:
    """Truthful labels from the shown cards (ties → no label):
    «ارزان‌ترین» = lowest typical price of these cards; «بیشترین فروشنده» = most sellers;
    «بهترین ارزش» only if the first pick is at or below the cards' median price and has at least their median sellers,
    else «نزدیک‌ترین به درخواستت» (it is the best text match)."""
    if not picks:
        return
    prices = [p.price for p in picks]
    sellers = [p.area.sellers if p.in_area else p.c.sellers_total for p in picks]
    first = picks[0]
    good_value = prices[0] <= statistics.median(prices) and sellers[0] >= statistics.median(sellers)
    first.role = "best_value" if good_value else "closest_match"
    for i, p in enumerate(picks[1:], start=1):
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
        if len({normalize_fa(v).replace(" ", "") for v in vals if v}) >= 2:
            rows.append({"key": k, "values": [v or "ثبت نشده" for v in vals]})
        if len(rows) == 3:
            break
    return rows


def why_line(p: Pick, budget: int | None = None) -> str:
    """One Persian line from the card's own facts (no LLM, no opinions, nothing invented)."""
    sellers = p.area.sellers if p.in_area else p.c.sellers_total
    parts = []
    if p.over_budget_pct:
        parts.append(f"قیمت معمولش {to_fa_digits(p.over_budget_pct)}٪ بالاتر از بودجه، ولی")
    if p.role == "most_available":
        parts.append(f"بیشترین فروشنده {p.where} ({to_fa_digits(sellers)} تا)")
    else:
        if p.role == "cheapest":
            parts.append("قیمت معمولش از بقیه‌ی این‌ها کمتره،")
        parts.append(f"{to_fa_digits(sellers)} فروشنده {p.where}")
    if p.low:
        scope = "" if p.area_priced or not p.in_area else " (در کل تهران)"
        parts.append(f"، قیمت ثبت‌شده از {format_price_short(p.low)}{scope}")
    if p.in_area and p.area.fresh:
        parts.append(f"، {to_fa_digits(p.area.fresh)} قیمت این ماه به‌روز شده")
    return " ".join(parts).replace(" ،", "،")


def budget_tiers(prices: list[int], suggested: list[int] = ()) -> list[int]:
    """Three budget buttons for these products. The model's suggestion (it knows who the item is for) when it falls
    inside the real prices; else the lower part of the price spread, so a few luxury listings don't set the buttons."""
    vals = sorted(v for v in prices if v)
    if not vals:
        return []
    lo, hi = vals[0], vals[min(len(vals) - 1, int(len(vals) * 0.9))]
    ok = sorted({_round_budget(s) for s in suggested if lo * 0.8 <= s <= hi * 1.1})
    if len(ok) >= 2:
        return ok[:3]
    pick = lambda q: vals[min(len(vals) - 1, int(len(vals) * q))]
    return sorted({_round_budget(pick(0.2)), _round_budget(pick(0.45)), _round_budget(pick(0.7))})


def _question(need: Need, cond: Conditions, price: dict, picks: list[Pick], steps: list[int] = ()) -> dict | None:
    """At most one splitting question, as direct_call chips (no re-parsing). Budget buttons: the model's suggestion,
    clamped to the matching products' real prices (budget_tiers)."""
    if not cond.budget_max and len(price) >= 3:
        vals = sorted(v for v in price.values() if v)
        if vals and vals[-1] / vals[0] >= 3:
            tiers = budget_tiers(vals, steps)
            chips = [Chip(label=f"تا {format_price_short(t)}", kind="direct_call", tool="set_budget",
                          args={"budget_max": t}) for t in tiers if t > 0]
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
