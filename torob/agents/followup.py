"""After a call (spec §6.5, §0.2 R2, R9): the «برنامه‌ی خرید» plan card and alternatives with a stated reason.

Alternatives, in order: (1) the same product at the next shop that passes the conditions and wasn't tried;
(2) the same product with one condition relaxed (stated); (3) a similar product (same category, same brand when known)
sold in the same building or nearby, priced within ±30%. The agent never calls the next shop by itself (R9).
"""

from datetime import datetime

from core.text import format_price_short, format_toman, price_age_label, to_fa_digits
from torob import search, store
from torob.agents import shops as S
from torob.agents.call import CallResult, price_change_text, result_label
from torob.logic.geo import bbox, haversine_m
from torob.models import Chip, Conditions

REASONS = {"no_answer": "جواب نداد", "unavailable": "نداشت", "closed": "تا برسی بسته‌ست"}


def nav(shop: dict) -> dict:
    """Map links that need no API: OpenStreetMap on the web, geo: for the phone's map app."""
    lat, lon = shop["lat"], shop["lon"]
    return {"osm_url": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=18/{lat:.6f}/{lon:.6f}",
            "geo_uri": f"geo:{lat:.6f},{lon:.6f}?q={lat:.6f},{lon:.6f}"}


def call_chip(shop_id: str, product_id: str, label: str, hold: bool = False) -> dict:
    args = {"shop_id": shop_id, "product_id": product_id}
    if hold:
        args["hold"] = True
    return Chip(label=label, kind="direct_call", tool="call_shop", args=args).model_dump()


def _mini(o: S.ShopOffer, now: datetime) -> dict:
    return {"shop_id": o.shop["shop_id"], "name": o.shop["name"], "where": S.where_line(o.shop),
            "visit": S.visit_line(o, now), "price": S.price_line(o, now)["text"]}


def _delta(price: int | None, ref: int | None) -> str:
    """«۲۰ هزار بیشتر» / «۱۵۰ هزار کمتر» / «همون قیمت» against the failed option; empty if unknown."""
    if not price or not ref:
        return ""
    d = price - ref
    if abs(d) < 10_000:
        return "همون قیمت"
    return f"{format_price_short(abs(d))} {'بیشتر' if d > 0 else 'کمتر'}"


def alternative(product_id: str, cond: Conditions, failed_shop: dict, reason: str, tried: set[str],
                now: datetime, hold: bool = False, failed_price: int | None = None) -> dict | None:
    """The next option after `failed_shop` didn't work, with the reason and the price difference on the card (R9).
    `hold=True` (closed before you arrive) makes the chip also ask the next shop to hold it."""
    res = S.find_shops(product_id, cond)
    why = f"چون {failed_shop['name']} {REASONS[reason]}"
    ref = failed_price or next((o.price_min for o in res.items + res.more if o.shop["shop_id"] == failed_shop["shop_id"]), None)
    for o in res.items + res.more:
        if o.shop["shop_id"] not in tried and o.open_state != "no":
            card = _mini(o, now)
            delta = _delta(o.price_min, ref)
            text = f"{why} → {card['name']}، {card['visit']}، {card['price']}" + (f" ({delta})" if delta else "")
            if res.relaxed:
                text += f"، {res.relaxed['label']}"
            label = f"از {card['name']} بپرسم تا رسیدنم نگهش داره؟" if hold else f"به {card['name']} زنگ بزنم؟"
            return {"kind": "shop", "reason": why, "text": text, "shop": card, "relaxed": res.relaxed,
                    "simulated": reason != "closed",   # «جواب نداد/نداشت» comes from the simulated call
                    "chip": call_chip(o.shop["shop_id"], product_id, label, hold=hold)}
    similar = similar_product(product_id, cond, failed_shop)
    if similar:
        delta = _delta(similar["price_min"], ref)
        over = ""
        if cond.budget_max and similar["price_min"] > cond.budget_max:
            over = f"، {format_price_short(similar['price_min'] - cond.budget_max)} بالاتر از بودجه"
        return {"kind": "product", "reason": why, "product": similar, "simulated": reason != "closed",
                "text": (f"{why} → «{similar['title']}» {similar['where_text']}، قیمت ثبت‌شده از "
                         f"{format_toman(similar['price_min'])}" + (f" ({delta})" if delta else "") + over),
                "chip": Chip(label="فروشنده‌هاش", kind="direct_call", tool="show_shops",
                             args={"product_id": similar["product_id"]}).model_dump()}
    return None


def similar_product(product_id: str, cond: Conditions, near_shop: dict) -> dict | None:
    """Same category (same brand if known), a sane price within ±30% that also fits the budget (+10%), sold — on the
    user's payment terms — in the same building or within the radius; never a same-title duplicate.
    Prefers the same building («همین پاساژ»), then most nearby sellers."""
    con = store.get()
    p = con.execute("SELECT category, brand, price_median, title_norm FROM products WHERE product_id = ?",
                    (product_id,)).fetchone()
    if not p or not p["price_median"]:
        return None
    lo, hi = p["price_median"] * 0.7, p["price_median"] * 1.3
    if cond.budget_max:
        hi = min(hi, cond.budget_max * 1.10)
    if hi < lo:
        return None
    lat, lon = cond.center.lat, cond.center.lng
    radius = cond.radius_km or 50
    la1, la2, lo1, lo2 = bbox(lat, lon, radius)
    pay_sql = {"any": "", "credit": " AND s.has_credit = 1",
               "credit_in_person": " AND EXISTS (SELECT 1 FROM shop_payments sp WHERE sp.shop_id = s.shop_id "
                                   "AND sp.channel = 'in_person')"}[cond.payment]
    fast_sql = " AND s.fast_delivery = 1" if cond.fast_delivery else ""
    rows = con.execute(f"""
      SELECT pr.product_id, pr.title, pr.title_norm, pr.brand, o.shop_id, o.price_toman, s.building_gid, s.lat, s.lon
      FROM products pr JOIN offers o USING (product_id) JOIN shops s USING (shop_id)
      WHERE pr.category = ? AND pr.product_id != ? AND pr.redacted = 0 AND o.price_flag = 'ok'
        AND o.price_toman BETWEEN ? AND ? AND s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?{pay_sql}{fast_sql}""",
                       (p["category"], product_id, lo, hi, la1, la2, lo1, lo2)).fetchall()
    best: dict[str, dict] = {}
    for r in rows:
        if p["brand"] and r["brand"] != p["brand"]:
            continue
        if search.near_duplicate(r["title_norm"], p["title_norm"]):
            continue   # another listing with the same title is not «a similar product»
        if haversine_m(lat, lon, r["lat"], r["lon"]) > radius * 1000:
            continue
        b = best.setdefault(r["product_id"], {"product_id": r["product_id"], "title": r["title"], "brand": r["brand"],
                                               "sellers": set(), "same_building": False, "price_min": r["price_toman"]})
        b["sellers"].add(r["shop_id"])
        b["price_min"] = min(b["price_min"], r["price_toman"])
        if near_shop.get("building_gid") and r["building_gid"] == near_shop["building_gid"]:
            b["same_building"] = True
    if not best:
        return None
    pick = max(best.values(), key=lambda b: (b["same_building"], len(b["sellers"])))
    n = len(pick["sellers"])
    pick["where_text"] = "تو همین پاساژ ثبت شده" if pick["same_building"] else f"{to_fa_digits(n)} فروشنده نزدیکت ثبت کرده‌اند"
    pick["sellers"] = n
    return pick


def plan_card(o: S.ShopOffer, product: dict, result: CallResult, backup: S.ShopOffer | None, now: datetime) -> dict:
    """«برنامه‌ی خرید»: dataset facts in plain style, simulated facts in the simulated style (honesty rule)."""
    s = o.shop
    age, _ = price_age_label(o.changed_at, now)
    has_loc = bool(s["floor"] or s["unit"])
    return {
        "shop_id": s["shop_id"], "product_id": product["product_id"], "product_title": product["title"],
        "name": s["name"], "where": S.where_line(s) if (has_loc or s["building"]) else None,
        "dataset": {"price": S.price_line(o, now)["text"], "age": age, "visit": S.visit_line(o, now)},
        "simulated": {"label": result_label(result), "price_change": price_change_text(result),
                      "hold_until": to_fa_digits(result.hold_until) if result.hold_until else None,
                      "location": result.location, "hold_short": False},
        "arrival": o.arrival.strftime("%H:%M") if o.arrival else None,
        "open_at_arrival": o.open_state,
        "backup": _mini(backup, now) if backup else None,
        "nav": nav(s),
        "recheck": call_chip(s["shop_id"], product["product_id"], "قبل از رفتن دوباره بپرس"),
        "status": "ok",
    }
