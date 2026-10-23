"""Where things are: the «بورس» of an item or a category, and every seller of a product on the map.

Tehran shoppers ask «بورس هندزفری کجاست؟»: the passage or street where that trade gathers, so they can walk from
shop to shop. The dataset answers it directly: shops have parsed buildings (passage / complex, grouped across
spellings as `building_gid`) and neighbourhoods, and offers say who sells what. The hub of a category is where the most
distinct sellers of it are; each answer says how many, out of how many in Tehran, so «بورس» is a measured claim
(«طبق داده‌ی ترب»), not folklore. Numbers are code; nothing here calls a model.
"""

from collections import Counter
from datetime import datetime
from functools import lru_cache

from core.text import format_price_short, format_toman, to_fa_digits
from torob import clock, store
from torob.agents import shops as shops_agent
from torob.models import Conditions

HUB_MIN_SHOPS = 3        # a building with fewer sellers of the category isn't a hub
HUB_MAX = 5
AREA_MAX = 3


def _sellers_sql(where: str) -> str:
    return f"""
      SELECT DISTINCT s.shop_id, s.building_gid, s.building, s.building_kind, s.area, s.lat, s.lon, s.floor
      FROM offers o JOIN products p USING (product_id) JOIN shops s USING (shop_id)
      WHERE {where} AND p.redacted = 0 AND s.lat IS NOT NULL"""


@lru_cache(maxsize=256)
def hubs(categories: tuple[str, ...] = (), product_id: str | None = None) -> dict:
    """The buildings and neighbourhoods where most sellers of these categories (or of one product) are.
    {"total", "buildings": [{gid, name, kind, area, lat, lon, shops, share, floors}], "areas": [{name, lat, lon, shops,
    share}], "spread": True when no building holds a fair share (the trade is scattered)}."""
    con = store.get()
    if product_id:
        rows = con.execute(_sellers_sql("o.product_id = ?"), (product_id,)).fetchall()
    elif categories:
        rows = con.execute(_sellers_sql(f"p.category IN ({','.join('?' * len(categories))})"), categories).fetchall()
    else:
        return {"total": 0, "buildings": [], "areas": [], "spread": True}
    seen, shops = set(), []
    for r in rows:   # one row per shop (a shop may match through several products)
        if r["shop_id"] not in seen:
            seen.add(r["shop_id"])
            shops.append(r)
    total = len(shops)
    by_b: dict[str, list] = {}
    by_a: dict[str, list] = {}
    for r in shops:
        if r["building_gid"]:
            by_b.setdefault(r["building_gid"], []).append(r)
        if r["area"]:
            by_a.setdefault(r["area"], []).append(r)
    buildings = []
    for gid, rs in sorted(by_b.items(), key=lambda kv: -len(kv[1])):
        if len(rs) < HUB_MIN_SHOPS or len(buildings) == HUB_MAX:
            break
        names = [r["building"] for r in rs if r["building"]]
        name = max(set(names), key=names.count) if names else "پاساژ"
        kinds = [r["building_kind"] for r in rs if r["building_kind"]]
        buildings.append({
            "gid": gid, "name": name, "kind": max(set(kinds), key=kinds.count) if kinds else "پاساژ",
            "area": max({r["area"] for r in rs if r["area"]} or {""}, key=lambda a: sum(r["area"] == a for r in rs)),
            "lat": sum(r["lat"] for r in rs) / len(rs), "lon": sum(r["lon"] for r in rs) / len(rs),
            "shops": len(rs), "share": round(len(rs) / total, 3), "floors": len({r["floor"] for r in rs if r["floor"]})})
    areas = [{"name": a, "lat": sum(r["lat"] for r in rs) / len(rs), "lon": sum(r["lon"] for r in rs) / len(rs),
              "shops": len(rs), "share": round(len(rs) / total, 3)}
             for a, rs in sorted(by_a.items(), key=lambda kv: -len(kv[1]))[:AREA_MAX]]
    spread = not buildings or buildings[0]["share"] < 0.08
    return {"total": total, "buildings": buildings, "areas": areas, "spread": spread}


def locate(name: str) -> tuple[float, float, str | None] | None:
    """A neighbourhood («تجریش»), street («ولیعصر») or passage the shopper names, placed by the dataset's own shops:
    the median position of shops in that area, or whose address or building names it. None if fewer than 3.
    The third value is None for a neighbourhood match; for an address match it is the neighbourhood those shops are in
    («پردیس» the city is not in the data, but «سینما پردیس» in an address is), so the shopper can be told."""
    name = " ".join(str(name).split())[:40]
    if len(name) < 2:
        return None
    con = store.get()
    rows = con.execute("SELECT lat, lon, area FROM shops WHERE lat IS NOT NULL AND area = ?", (name,)).fetchall()
    where = None
    if len(rows) < 3:
        like = f"%{name}%"
        rows = con.execute("SELECT lat, lon, area FROM shops WHERE lat IS NOT NULL AND (address LIKE ? OR building LIKE ?) "
                           "LIMIT 500", (like, like)).fetchall()
        areas = Counter(r["area"] for r in rows if r["area"])
        where = areas.most_common(1)[0][0] if areas else "مرکز تهران"
    if len(rows) < 3:
        return None
    lats, lons = sorted(r["lat"] for r in rows), sorted(r["lon"] for r in rows)
    return lats[len(lats) // 2], lons[len(lons) // 2], where


def hub_sentence(subject: str, h: dict) -> str:
    """«بورس هندزفری طبق داده‌ی ترب: پاساژ علاءالدین (۴۲ فروشنده از ۳۱۰ در تهران)، بعد چارسو (۲۱)؛ محله: جمهوری»."""
    if not h["total"]:
        return f"برای «{subject}» فروشنده‌ی حضوری‌ای در داده‌ی ترب پیدا نکردم."
    area = h["areas"][0]["name"] if h["areas"] else None
    if h["spread"]:
        where = f"بیشترشون در محله‌ی {area} هستن" if area else "پخش‌ان"
        return (f"«{subject}» بورس مشخصی نداره؛ {to_fa_digits(h['total'])} فروشنده‌ی حضوری در داده‌ی ترب، "
                f"{where}.")
    b = h["buildings"]
    first = f"{b[0]['kind']} {b[0]['name']} ({to_fa_digits(b[0]['shops'])} فروشنده از {to_fa_digits(h['total'])} در تهران)"
    rest = "، ".join(f"{x['name']} ({to_fa_digits(x['shops'])})" for x in b[1:3])
    text = f"بورس «{subject}» طبق داده‌ی ترب: {first}"
    if rest:
        text += f"؛ بعدش {rest}"
    if area:
        text += f". محله‌اش: {area}"
    return text + "."


def product_map(product_id: str, cond: Conditions, shown: list[str] = (), now: datetime | None = None) -> dict:
    """Every in-person seller of a product, planned against the shopper's conditions, for the map: price, open when
    they'd arrive, minutes, building and floor, and whether it is inside the search radius. Same facts as the cards."""
    now = now or clock.now()
    product, offers, _ = shops_agent.load_offers(product_id)
    rank = {sid: i + 1 for i, sid in enumerate(shown)}
    points = []
    for o in offers:
        shops_agent._plan_visit(o, cond, now)
        s = o.shop
        points.append({
            "shop_id": s["shop_id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
            "price": o.price_min, "price_text": format_toman(o.price_min) if o.priced else "قیمت ثبت نشده",
            "pin": format_price_short(o.price_min) if o.priced else "؟",
            "age": shops_agent.price_line(o, now)["age"], "open": o.open_state,
            "visit": shops_agent.visit_line(o, now), "minutes": o.minutes, "mode": o.mode,
            "where": shops_agent.where_line(s), "building_gid": s["building_gid"],
            "building": f"{s['building_kind'] or 'پاساژ'} {s['building']}" if s["building"] else None,
            "floor": s["floor"], "area": s["area"], "credit": bool(o.payments),
            "in_radius": shops_agent._in_radius(o, cond), "rank": rank.get(s["shop_id"])})
    points.sort(key=lambda p: (p["price"] is None, p["price"] or 0))
    return {"product": {"product_id": product["product_id"], "title": product["title"].split("|")[0].strip(),
                        "category": product["category"]},
            "center": {"lat": cond.center.lat, "lng": cond.center.lng, "label": cond.center.label},
            "radius_km": cond.radius_km, "points": points,
            "hubs": hubs(product_id=product_id)["buildings"],
            "category": {"name": product["category"], **hubs((product["category"],))}}


def category_map(categories: tuple[str, ...], cond: Conditions) -> dict:
    """Sellers of a category as light dots plus its hubs: the map for «بورس … کجاست؟»."""
    con = store.get()
    rows = con.execute(_sellers_sql(f"p.category IN ({','.join('?' * len(categories))})"), categories).fetchall()
    dots, seen = [], set()
    for r in rows:
        if r["shop_id"] not in seen:
            seen.add(r["shop_id"])
            dots.append([round(r["lat"], 5), round(r["lon"], 5)])
    return {"center": {"lat": cond.center.lat, "lng": cond.center.lng, "label": cond.center.label},
            "dots": dots[:1500], **hubs(categories)}
