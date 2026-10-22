"""Steps 2–3 (spec §6.4, §8, §0.2 R6, R8, R9, R13, A9, A10): shops that registered a product, compared.

All numbers are code. Three things are never merged (brief): the registered price, how old it is, and stock — which
the data never confirms («موجودی: تأیید نشده»; only a call in S6 can change it, and then it is labelled simulated).
Unknown hours stay unknown. Travel time is an estimate. When the conditions leave nothing, exactly one condition is
relaxed and the card says which; the user's strip is not changed unless they tap the chip.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

from core.text import TEHRAN, format_price_short, format_toman, price_age_label, to_fa_digits
from torob import clock, store
from torob.logic import hours as H
from torob.logic.geo import bbox, estimate_minutes, haversine_m
from torob.models import Chip, Conditions

WALK_MAX_M = 2_000          # beyond this, estimate by car (R13)
TOP_N = 3
CATEGORY_ONLY_MAX = 10
WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
W = {"price": 0.40, "fresh": 0.25, "travel": 0.15, "slack": 0.10, "complete": 0.10}   # R13
BADGE_LABELS = {"open_known": "تا برسی بازه", "cheapest": "ارزان‌ترین", "nearest": "نزدیک‌ترین", "open_latest": "دیرتر باز",
                "freshest_price": "قیمت تازه‌تر", "credit": "قسطی"}


@dataclass
class ShopOffer:
    shop: dict                      # shops row
    price_min: int | None           # sane registered prices only (variants grouped)
    price_max: int | None
    changed_at: str | None          # newest change among this shop's sane offers (or any offer if none sane)
    flags: set[str]
    seller_title: str
    warranty: str | None
    per_metre: bool
    seller_note: str | None = None    # the seller's own description of the product (12% of offers)
    meters: float = 0
    minutes: int = 0
    mode: str = "walk"
    arrival: datetime | None = None
    open_state: str = "unknown"     # yes | tight | no | unknown (at arrival)
    closes: datetime | None = None
    next_open: datetime | None = None
    payments: list[tuple[str, str]] = field(default_factory=list)
    same_building_others: int = 0
    score: float = 0
    badges: list[str] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        return self.price_min is not None


@dataclass
class ShopsResult:
    product: dict
    items: list[ShopOffer]
    more: list[ShopOffer]
    category_only: list[dict]
    counts: dict                    # found, closed_at_arrival, suspicious_hidden, filtered_out
    notes: list[str]
    relaxed: dict | None            # {"field", "label", "chip"} when one condition had to be relaxed


def _payments(con: sqlite3.Connection, shop_ids: list[str]) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    if shop_ids:
        q = f"SELECT shop_id, provider, channel FROM shop_payments WHERE shop_id IN ({','.join('?' * len(shop_ids))})"
        for sid, prov, ch in con.execute(q, shop_ids):
            out.setdefault(sid, []).append((prov, ch))
    return out


def load_offers(product_id: str) -> tuple[dict, list[ShopOffer], set[str]]:
    """All shops that registered the product, variants grouped per shop.
    Returns (product, offers, ids of shops whose only prices are suspicious — set aside, still sellers)."""
    con = store.get()
    prod = con.execute("SELECT product_id, title, category, brand, image FROM products WHERE product_id = ?",
                       (product_id,)).fetchone()
    if prod is None:
        raise KeyError(product_id)
    rows = con.execute(
        "SELECT o.shop_id, o.price_toman, o.price_changed_at, o.price_flag, o.seller_title, o.warranty, o.per_metre, "
        "o.description "
        "FROM offers o WHERE o.product_id = ?", (product_id,)).fetchall()
    by_shop: dict[str, list] = {}
    for r in rows:
        by_shop.setdefault(r["shop_id"], []).append(r)
    shops = {r["shop_id"]: dict(r) for r in con.execute(
        f"SELECT * FROM shops WHERE shop_id IN ({','.join('?' * len(by_shop))})", list(by_shop))} if by_shop else {}
    pays = _payments(con, list(by_shop))
    offers, suspicious_only = [], set()
    for sid, rs in by_shop.items():
        sane = [r for r in rs if r["price_flag"] == "ok"]
        flags = {r["price_flag"] for r in rs}
        if not sane and flags <= {"suspicious_low", "suspicious_high"}:
            suspicious_only.add(sid)       # R5: set aside, counted in the grey line
            continue
        ref = sane or rs
        changed = max((r["price_changed_at"] for r in ref if r["price_changed_at"]), default=None)
        offers.append(ShopOffer(
            shop=shops[sid], price_min=min(r["price_toman"] for r in sane) if sane else None,
            price_max=max(r["price_toman"] for r in sane) if sane else None, changed_at=changed, flags=flags,
            seller_title=ref[0]["seller_title"], warranty=next((r["warranty"] for r in rs if r["warranty"]), None),
            per_metre=any(r["per_metre"] for r in rs), payments=pays.get(sid, []),
            seller_note=next((" ".join(r["description"].split()) for r in rs if (r["description"] or "").strip()), None)))
    return dict(prod), offers, suspicious_only


def _plan_visit(o: ShopOffer, cond: Conditions, now: datetime) -> None:
    s = o.shop
    o.meters = haversine_m(cond.center.lat, cond.center.lng, s["lat"], s["lon"])
    o.mode = "walk" if o.meters <= WALK_MAX_M and cond.mode == "walk" else "car"
    o.minutes = estimate_minutes(o.meters, o.mode)
    if cond.arrive_at:   # the user said when they'll be there; a time already past today means tomorrow
        arrive = H.as_tehran(cond.arrive_at)
        o.arrival = arrive if arrive > now else arrive + timedelta(days=1)
    else:
        o.arrival = now + timedelta(minutes=o.minutes)
    week = H.from_json(s["hours"])
    o.open_state, o.closes = H.open_at_arrival(week, o.arrival)
    if o.open_state == "no":
        o.next_open = H.next_open(week, o.arrival)


def _credit_ok(o: ShopOffer, payment: str) -> bool:
    if payment == "any":
        return True
    if payment == "credit":
        return bool(o.payments)
    return any(ch == "in_person" for _, ch in o.payments)


def _in_radius(o: ShopOffer, c: Conditions) -> bool:
    return not c.radius_km or o.meters <= c.radius_km * 1000


def _passes_terms(o: ShopOffer, c: Conditions) -> bool:
    """Purchase terms only (credit, express delivery, budget); radius and opening are checked separately."""
    if not _credit_ok(o, c.payment):
        return False
    if c.fast_delivery and not o.shop["fast_delivery"]:
        return False
    if c.budget_max and (not o.priced or o.price_min > c.budget_max * 1.10):   # no price = can't claim it fits
        return False
    return True


def _passes_open(o: ShopOffer, c: Conditions, now: datetime) -> bool:
    if c.open == "now" and H.status_at(H.from_json(o.shop["hours"]), now)[0] == "closed":
        return False
    return not (c.open == "at_arrival" and o.open_state == "no")


def _passes(o: ShopOffer, c: Conditions, now: datetime) -> bool:
    return _in_radius(o, c) and _passes_terms(o, c) and _passes_open(o, c, now)


def _freshness(changed_at: str | None, now: datetime) -> float:
    if not changed_at:
        return 0.0
    days = (now - datetime.fromisoformat(changed_at)).days
    return 1.0 if days <= 7 else 0.6 if days <= 30 else 0.3 if days <= 90 else 0.0


def _score(items: list[ShopOffer], now: datetime) -> None:
    cheapest = min((o.price_min for o in items if o.priced), default=None)
    for o in items:
        price = (cheapest / o.price_min) if (cheapest and o.priced) else 0.0
        travel = max(0.0, 1 - o.minutes / 60)
        slack = {"yes": 1.0, "tight": 0.5, "unknown": 0.3, "no": 0.0}[o.open_state]
        s = o.shop
        complete = 0.5 * bool(s["hours_known"]) + 0.25 * bool(o.warranty) + 0.25 * bool(s["floor"] or s["unit"])
        o.score = (W["price"] * price + W["fresh"] * _freshness(o.changed_at, now) + W["travel"] * travel
                   + W["slack"] * slack + W["complete"] * complete)
        if o.open_state == "no":
            o.score -= 1.0
        if not o.priced:
            o.score -= 0.5          # «قیمت ثبت نشده» never ranks first


def _badges(compared: list[ShopOffer], shown: list[ShopOffer]) -> None:
    """A badge is a fact about the whole compared set (every shop that passed), given to its unique winner only
    if that winner is one of the shown cards (ties → no badge)."""
    items = compared

    def unique_best(key, reverse=False, cond=lambda o: True):
        pool = [o for o in items if cond(o)]
        if len(pool) < 2:
            return None
        vals = sorted(pool, key=key, reverse=reverse)
        return vals[0] if key(vals[0]) != key(vals[1]) else None

    picks = {
        "cheapest": unique_best(lambda o: o.price_min, cond=lambda o: o.priced),
        "nearest": unique_best(lambda o: o.minutes),
        "open_latest": unique_best(lambda o: o.closes or datetime.min.replace(tzinfo=TEHRAN), reverse=True,
                                   cond=lambda o: o.closes is not None),
        "freshest_price": unique_best(lambda o: o.changed_at or "", reverse=True, cond=lambda o: o.changed_at),
    }
    shown_ids = {id(o) for o in shown}
    for badge, o in picks.items():
        if o and id(o) in shown_ids:
            o.badges.append(badge)
    for o in shown:
        if o.payments:
            o.badges.append("credit")
    # Known-open shops rank above unknown-hours ones (A10); when that's why a card is ahead, say it first.
    if any(o.open_state == "unknown" for o in shown):
        for o in shown:
            if o.open_state in ("yes", "tight"):
                o.badges.insert(0, "open_known")


@lru_cache(maxsize=512)
def shop_categories_for(product_category: str) -> tuple[str, ...]:
    """Shops declare broad activity categories («لوازم جانبی موبایل و تبلت»), products have narrow ones
    («شارژر گوشی»). Learn the link from the data: the broad categories most sellers of this narrow category declare."""
    con = store.get()
    rows = con.execute("""
      WITH sellers AS (SELECT DISTINCT o.shop_id FROM offers o JOIN products p USING (product_id) WHERE p.category = ?)
      SELECT j.value, count(*) n, (SELECT count(*) FROM sellers) total
      FROM shops s JOIN sellers USING (shop_id), json_each(s.categories) j GROUP BY 1 ORDER BY 2 DESC LIMIT 3""",
                       (product_category,)).fetchall()
    return tuple(r[0] for r in rows if r[2] and r[1] / r[2] >= 0.3)


def category_only(product: dict, cond: Conditions, seller_ids: set[str]) -> list[dict]:
    """Nearby shops whose declared activity matches, but that did not register this product: «موجودی نامعلوم»."""
    cats = shop_categories_for(product["category"])
    if not cats:
        return []
    con = store.get()
    lat, lon = cond.center.lat, cond.center.lng
    sql = "SELECT shop_id, name, building, floor, unit, plaque, address, lat, lon, categories FROM shops WHERE 1=1"
    args: list = []
    if cond.radius_km:
        la1, la2, lo1, lo2 = bbox(lat, lon, cond.radius_km)
        sql += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        args += [la1, la2, lo1, lo2]
    sql += " AND (" + " OR ".join("categories LIKE ?" for _ in cats) + ")"
    args += [f'%"{c}"%' for c in cats]
    out = []
    for r in con.execute(sql, args):
        if r["shop_id"] in seller_ids:
            continue
        m = haversine_m(lat, lon, r["lat"], r["lon"])
        if cond.radius_km and m > cond.radius_km * 1000:
            continue
        out.append({"shop_id": r["shop_id"], "name": r["name"], "meters": round(m),
                    "minutes": estimate_minutes(m, "walk" if m <= WALK_MAX_M else "car"),
                    "where": where_line(dict(r)), "stock_label": "این مدل رو ثبت نکرده — موجودی نامعلوم"})
    return sorted(out, key=lambda x: x["meters"])[:CATEGORY_ONLY_MAX]


def _same_building(offers: list[ShopOffer]) -> None:
    groups: dict[str, int] = {}
    for o in offers:
        if o.shop["building_gid"]:
            groups[o.shop["building_gid"]] = groups.get(o.shop["building_gid"], 0) + 1
    for o in offers:
        gid = o.shop["building_gid"]
        o.same_building_others = groups.get(gid, 1) - 1 if gid else 0


def _relax_options(c: Conditions, keep: frozenset[str] = frozenset()) -> list[tuple[str, dict, str]]:
    """Candidate single-condition relaxations, in order, with a human label. Distance gives way first (a farther shop
    that meets every term beats a nearby one that doesn't: «only instalments» stays instalments); conditions the user
    has just set (`keep`) give way last."""
    opts = []
    if c.radius_km:
        for r in (c.radius_km * 2, c.radius_km * 4):
            if r <= 50:
                opts.append(("radius_km", {"radius_km": r}, f"تا {km_label(r)}"))
        opts.append(("radius_km", {"radius_km": None}, "کل تهران"))
    if c.open != "any":
        opts.append(("open", {"open": "any"}, "بدون شرط باز بودن"))
    if c.budget_max:
        opts.append(("budget_max", {"budget_max": None}, "بدون محدودیت بودجه"))
    if c.fast_delivery:
        opts.append(("fast_delivery", {"fast_delivery": False}, "بدون شرط ارسال فوری"))
    if c.payment == "credit_in_person":
        opts.append(("payment", {"payment": "credit"}, "قسطی (حضوری بودنش ثبت نشده) هم حساب شد"))
    if c.payment != "any":
        opts.append(("payment", {"payment": "any"}, "بدون شرط قسطی"))
    return sorted(opts, key=lambda o: o[0] in keep)   # stable: the user's latest choice is relaxed only as a last resort


def km_label(km: float) -> str:
    """0.4 → «۴۰۰ متر», 2 → «۲ کیلومتر», 2.5 → «۲٫۵ کیلومتر»."""
    if km < 1:
        return f"{to_fa_digits(round(km * 1000))} متر"
    text = f"{km:.1f}".rstrip("0").rstrip(".")
    return to_fa_digits(text).replace(".", "٫") + " کیلومتر"


def find_shops(product_id: str, cond: Conditions, keep: frozenset[str] = frozenset()) -> ShopsResult:
    now = clock.now()
    product, offers, suspicious_only = load_offers(product_id)
    for o in offers:
        _plan_visit(o, cond, now)
    _same_building(offers)
    notes: list[str] = []
    relaxed = None
    passing = [o for o in offers if _passes(o, cond, now)]
    # R4 funnel that adds up: registered nearby → didn't match the terms → closed when you'd arrive → shown.
    nearby = [o for o in offers if _in_radius(o, cond)]
    terms_ok = [o for o in nearby if _passes_terms(o, cond)]
    counts = {"found": len(nearby), "found_tehran": len(offers), "terms_mismatch": len(nearby) - len(terms_ok),
              "closed_at_arrival": sum(1 for o in terms_ok if not _passes_open(o, cond, now)),
              "suspicious_hidden": len(suspicious_only)}
    if not passing:
        for fld, patch, label in _relax_options(cond, keep):
            trial = cond.model_copy(update=patch)
            got = [o for o in offers if _passes(o, trial, now)]
            if got:
                passing = got
                chip = Chip(label=f"همین رو بذار ({label})", kind="direct_call", tool="set_conditions", args=patch)
                relaxed = {"field": fld, "label": label, "chip": chip.model_dump()}
                notes.append(f"با این شرایط فروشنده‌ای نبود؛ {label}: {to_fa_digits(len(got))} فروشنده.")
                break
    if not passing and offers:
        notes.append("نزدیکت فروشنده‌ای برای این مدل ثبت نشده." if not nearby
                     else "فروشنده‌های این مدل با هیچ تغییر ساده‌ای جور نشدن.")
    _score(passing, now)
    # A10: known-open first, then unknown hours, then closed-at-arrival (only reachable via a relaxed «open»).
    open_rank = {"yes": 0, "tight": 0, "unknown": 1, "no": 2}
    passing.sort(key=lambda o: (open_rank[o.open_state], -o.score, o.meters))
    top, more = passing[:TOP_N], passing[TOP_N:]
    _badges(passing, top)
    if not product_has_hours(top) and top:
        notes.append("ساعت کاری این فروشگاه‌ها ثبت نشده؛ قبل از رفتن بپرس.")
    # Shops that registered it with a junk price are still sellers: never list them as «ثبت نکرده».
    cat_only = category_only(product, cond, {o.shop["shop_id"] for o in offers} | suspicious_only)
    counts["passing"] = len(passing)
    return ShopsResult(product, top, more, cat_only, counts, notes, relaxed)


def product_has_hours(items: list[ShopOffer]) -> bool:
    return any(o.shop["hours_known"] for o in items)


# --- wording (R8) -------------------------------------------------------------------------------------------------

def where_line(s: dict) -> str:
    """«پاساژ علاءالدین، طبقه ۲، واحد ۲۱۴» from parsed fields only; else the written address."""
    parts = []
    if s.get("building"):
        parts.append(f"{s.get('building_kind') or 'پاساژ'} {s['building']}")
    if s.get("floor"):
        parts.append(s["floor"] if s["floor"] in ("همکف", "زیرهمکف") or s["floor"].startswith(("منفی", "زیر"))
                     else f"طبقه {s['floor']}")
    if s.get("unit"):
        parts.append(f"واحد {to_fa_digits(s['unit'])}")
    elif s.get("plaque"):
        parts.append(f"پلاک {to_fa_digits(s['plaque'])}")
    return "، ".join(parts) if parts else (s.get("address") or "آدرس ثبت نشده")


def _day_label(dt: datetime, now: datetime) -> str:
    d = (dt.date() - now.date()).days
    day = "امروز" if d == 0 else "فردا" if d == 1 else WEEKDAYS[H.iran_weekday(dt.date())]
    return f"{day} {to_fa_digits(dt.strftime('%H:%M'))}"


def visit_line(o: ShopOffer, now: datetime) -> str:
    move = f"{to_fa_digits(o.minutes)} دقیقه {'پیاده' if o.mode == 'walk' else 'با ماشین'} (تخمینی)"
    if o.open_state in ("yes", "tight"):
        state = f"باز تا {to_fa_digits(o.closes.strftime('%H:%M'))}" + ("، وقت کمه" if o.open_state == "tight" else "")
    elif o.open_state == "no":
        state = "تا برسی بسته‌ست" + (f"، {_day_label(o.next_open, now)} باز می‌کنه" if o.next_open else "")
    else:
        state = "ساعت کاری ثبت نشده"
    return f"{move}، {state}"


def price_line(o: ShopOffer, now: datetime) -> dict:
    age, old = price_age_label(o.changed_at, now)
    if not o.priced:
        return {"text": "قیمت ثبت نشده", "age": age, "old": True}
    price = format_toman(o.price_min) if o.price_min == o.price_max else \
        f"{format_toman(o.price_min)} تا {format_toman(o.price_max)}"
    unit = " (قیمت متری)" if o.per_metre else ""
    return {"text": f"قیمت ثبت‌شده: {price}{unit}", "age": age, "old": old,
            "note": "احتمالاً تغییر کرده" if old else None,
            "min": o.price_min, "max": o.price_max, "per_metre": bool(o.per_metre)}   # numbers for the card layout


def _link(url: str | None) -> str | None:
    """A shop's own Instagram page or website, only as a plain http(s) URL."""
    url = (url or "").strip()
    return url if url.startswith(("https://", "http://")) and " " not in url and len(url) <= 300 else None


def payment_labels(o: ShopOffer) -> list[str]:
    out = []
    for prov, ch in o.payments:
        out.append(f"{prov}، {'حضوری' if ch == 'in_person' else 'حضوری بودنش ثبت نشده'}")
    return out


def card(o: ShopOffer, product: dict, cond: Conditions, now: datetime) -> dict:
    s = o.shop
    reasons = [BADGE_LABELS[b] for b in o.badges if b in BADGE_LABELS][:2]
    if o.same_building_others:
        reasons.append(f"{to_fa_digits(o.same_building_others)} فروشنده‌ی دیگه تو همین پاساژ هم ثبت کرده‌اند")
    call = Chip(label="زنگ بزن بپرس", kind="direct_call", tool="call_shop",
                args={"shop_id": s["shop_id"], "product_id": product["product_id"]})
    return {
        "shop_id": s["shop_id"], "name": s["name"], "name_is_real": bool(s["name_is_real"]),
        "where": where_line(s), "address": s["address"], "area": s["area"],
        "price": price_line(o, now), "stock_label": "موجودی: تأیید نشده", "stock": "listed",
        "visit": {"text": visit_line(o, now), "minutes": o.minutes, "meters": round(o.meters), "mode": o.mode,
                  "estimate": True, "open_at_arrival": o.open_state,
                  "closes": o.closes.strftime("%H:%M") if o.closes else None},
        "details": {"payments": payment_labels(o) or ["شرایط قسطی ثبت نشده"],
                    "warranty": o.warranty or "گارانتی ثبت نشده",
                    "fast_delivery": {True: "ارسال فوری دارد", False: "ارسال فوری ندارد"}.get(
                        None if s["fast_delivery"] is None else bool(s["fast_delivery"]), "ارسال فوری ثبت نشده"),
                    "seller_title": o.seller_title,
                    "seller_note": (o.seller_note[:280] + "…") if o.seller_note and len(o.seller_note) > 280 else o.seller_note},
        "badges": o.badges[:2], "reasons": reasons, "same_building_others": o.same_building_others,
        "lat": s["lat"], "lon": s["lon"], "button": call.model_dump(),
        "instagram": _link(s["instagram"]), "website": _link(s["website"]),   # only the 112 real-named shops have one
    }
