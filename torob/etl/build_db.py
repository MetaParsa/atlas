"""Build the read-only Torob dataset DB (SQLite + FTS5) from the official CSV export.

Run locally, then upload the file to the host disk (spec §0.2 R12):
    uv run python -m torob.etl.build_db --csv-dir ../Torob/data/v16 --out data/torob.db
"""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import duckdb

from core.text import normalize_fa
from torob.etl.address import parse_address
from torob.logic import hours as hours_mod
from torob.logic.price import MAX_SANE, MIN_OFFERS_FOR_PRODUCT_RULE, MIN_SANE, PRODUCT_RATIO, warranty_text

DATASET_VERSION = "v16"
EXPORTED_AT = "2026-10-14T00:00:00+03:30"   # zip file dates; newest price change is 2026-10-13
EXPECTED = {"shops": 6476, "products": 225903, "offers": 331506}
BNPL_PROVIDERS = {"ترب‌پی", "اسنپ‌پی", "دیجی پی", "تارا", "ازکی‌وام"}
BNPL_NORM = {normalize_fa(p) for p in BNPL_PROVIDERS}

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE shops(
  shop_id TEXT PRIMARY KEY, name TEXT, name_is_real INTEGER, address TEXT,
  building_kind TEXT, building TEXT, building_key TEXT, floor TEXT, unit TEXT, plaque TEXT,
  area TEXT, lat REAL, lon REAL, categories TEXT, hours TEXT, hours_known INTEGER,
  fast_delivery INTEGER, instagram TEXT, website TEXT, offer_count INTEGER, has_credit INTEGER);
CREATE TABLE shop_payments(
  shop_id TEXT, provider TEXT, channel TEXT, is_bnpl INTEGER, categories TEXT, min_price INTEGER, max_price INTEGER);
CREATE TABLE products(
  rid INTEGER PRIMARY KEY, product_id TEXT UNIQUE, title TEXT, title_norm TEXT, brand TEXT, brand_norm TEXT,
  category TEXT, category_norm TEXT, model TEXT, specs TEXT, spec_count INTEGER, image TEXT,
  sellers_total INTEGER, ok_offers INTEGER, price_min INTEGER, price_median INTEGER, price_max INTEGER,
  fresh_offers_30d INTEGER);
CREATE TABLE offers(
  offer_id TEXT PRIMARY KEY, shop_id TEXT, product_id TEXT, seller_title TEXT, description TEXT,
  price_toman INTEGER, price_changed_at TEXT, warranty TEXT, price_flag TEXT, per_metre INTEGER);
CREATE TABLE categories(
  category TEXT PRIMARY KEY, category_norm TEXT, product_count INTEGER, offer_count INTEGER, shop_count INTEGER);
CREATE TABLE areas(area TEXT PRIMARY KEY, lat REAL, lon REAL, shops INTEGER, shops_with_offers INTEGER);
CREATE VIRTUAL TABLE products_fts USING fts5(
  title_norm, brand_norm, category_norm, content='products', content_rowid='rid',
  tokenize='unicode61 remove_diacritics 2');
"""

INDEXES = """
CREATE INDEX offers_product ON offers(product_id);
CREATE INDEX offers_shop ON offers(shop_id);
CREATE INDEX shops_area ON shops(area);
CREATE INDEX shops_latlon ON shops(lat, lon);
CREATE INDEX shops_building ON shops(building_key);
CREATE INDEX payments_shop ON shop_payments(shop_id);
CREATE INDEX products_category ON products(category);
"""


def load(con: duckdb.DuckDBPyConnection, csv_dir: Path) -> None:
    def read(name: str, types: str = "") -> None:
        path = (csv_dir / f"{name}.csv").as_posix().replace("'", "''")
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_csv('{path}', header=true, nullstr='null', sample_size=-1{types})")

    read("shops")
    read("products")
    read("offers", ", types={'last_price_changed_at': 'VARCHAR', 'price_toman': 'BIGINT'}")
    for name, n in EXPECTED.items():
        got = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        assert got == n, f"{name}: expected {n} rows, got {got}"


def flag_offers(con: duckdb.DuckDBPyConnection) -> None:
    """Price flags in SQL (same rules as torob.logic.price.flag_price, which the tests cover)."""
    con.execute(f"""
    CREATE TABLE offer_flags AS
    WITH pm AS (SELECT product_id, median(price_toman) med, count(price_toman) n FROM offers GROUP BY 1)
    SELECT o.offer_id,
      CASE
        WHEN o.price_toman IS NULL THEN 'missing'
        WHEN o.price_toman < {MIN_SANE} THEN 'suspicious_low'
        WHEN o.price_toman > {MAX_SANE} THEN 'suspicious_high'
        WHEN pm.n >= {MIN_OFFERS_FOR_PRODUCT_RULE} AND o.price_toman > {PRODUCT_RATIO} * pm.med THEN 'suspicious_high'
        WHEN pm.n >= {MIN_OFFERS_FOR_PRODUCT_RULE} AND o.price_toman < pm.med / {PRODUCT_RATIO} THEN 'suspicious_low'
        ELSE 'ok' END AS price_flag
    FROM offers o JOIN pm USING (product_id)
    """)


def build(csv_dir: Path, out: Path) -> dict:
    t0 = time.perf_counter()
    con = duckdb.connect()
    load(con, csv_dir)
    flag_offers(con)

    tmp = out.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)

    # Offers
    rows = con.execute("""
      SELECT o.offer_id, o.shop_id, o.product_id, o.seller_title, nullif(trim(o.description), ''), o.price_toman,
             o.last_price_changed_at, o.warranty, f.price_flag, (o.seller_title LIKE '%متری%')::INT
      FROM offers o JOIN offer_flags f USING (offer_id)""").fetchall()
    db.executemany("INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [(*r[:7], warranty_text(r[7]), r[8], r[9]) for r in rows])

    # Products (+ stats from sane offers only)
    rows = con.execute(f"""
      WITH s AS (
        SELECT o.product_id, count(DISTINCT o.shop_id) sellers,
               count(*) FILTER (WHERE f.price_flag = 'ok') ok_n,
               min(o.price_toman) FILTER (WHERE f.price_flag = 'ok') pmin,
               median(o.price_toman) FILTER (WHERE f.price_flag = 'ok') pmed,
               max(o.price_toman) FILTER (WHERE f.price_flag = 'ok') pmax,
               count(*) FILTER (WHERE f.price_flag = 'ok'
                 AND o.last_price_changed_at::TIMESTAMPTZ >= TIMESTAMPTZ '{EXPORTED_AT}' - INTERVAL 30 DAY) fresh
        FROM offers o JOIN offer_flags f USING (offer_id) GROUP BY 1)
      SELECT p.product_id, p.persian_title, p.brand_name, p.category_name, p.model, p.specs,
             coalesce(json_array_length(p.specs), 0), json_extract_string(p.image_urls, '$[0]'),
             s.sellers, s.ok_n, s.pmin::BIGINT, s.pmed::BIGINT, s.pmax::BIGINT, s.fresh
      FROM products p JOIN s USING (product_id)""").fetchall()
    db.executemany(
        "INSERT INTO products(product_id,title,title_norm,brand,brand_norm,category,category_norm,model,specs,"
        "spec_count,image,sellers_total,ok_offers,price_min,price_median,price_max,fresh_offers_30d) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], normalize_fa(r[1]), r[2], normalize_fa(r[2]), r[3], normalize_fa(r[3]), *r[4:]) for r in rows])

    # Shops
    offer_counts = dict(con.execute("SELECT shop_id, count(*) FROM offers GROUP BY 1").fetchall())
    rows = con.execute("""SELECT shop_id, name, address, location_label, lat, lon, category_names, weekly_hours,
                                 supports_fast_delivery, instagram, website_url, payment_options FROM shops""").fetchall()
    shops, payments = [], []
    for (sid, name, address, area, lat, lon, cats, weekly, fast, insta, site, pay) in rows:
        a = parse_address(address)
        week = hours_mod.parse_weekly_hours(weekly)
        has_credit = 0
        for p in json.loads(pay or "[]"):
            # Every payment_options entry is a declared credit / instalment service (incl. shop-run schemes);
            # is_bnpl marks the well-known providers.
            prov = p.get("provider_name")
            bnpl = int(normalize_fa(prov) in BNPL_NORM)
            has_credit = 1
            payments.append((sid, prov, p.get("channel"), bnpl, json.dumps(p.get("category_names") or [], ensure_ascii=False),
                             p.get("min_price_toman") or None, p.get("max_price_toman") or None))
        insta_url = json.loads(insta).get("profile_url") if insta else None
        shops.append((sid, name, int(bool(insta or site)), address, a.building_kind, a.building, a.building_key,
                      a.floor, a.unit, a.plaque, area or None, lat, lon, cats, hours_mod.to_json(week),
                      int(hours_mod.hours_known(week)), None if fast is None else int(fast), insta_url, site,
                      offer_counts.get(sid, 0), has_credit))
    db.executemany("INSERT INTO shops VALUES (" + ",".join("?" * 21) + ")", shops)
    db.executemany("INSERT INTO shop_payments VALUES (?,?,?,?,?,?,?)", payments)

    # Categories (used by the advisor's category lookup) and areas
    cats = con.execute("""
      SELECT p.category_name, count(DISTINCT p.product_id), count(o.offer_id), count(DISTINCT o.shop_id)
      FROM products p JOIN offers o USING (product_id) GROUP BY 1""").fetchall()
    db.executemany("INSERT INTO categories VALUES (?,?,?,?,?)", [(c, normalize_fa(c), *rest) for c, *rest in cats])
    db.execute("""INSERT INTO areas
      SELECT area, avg(lat), avg(lon), count(*), sum(offer_count > 0) FROM shops WHERE area IS NOT NULL GROUP BY area""")

    db.executescript(INDEXES + "INSERT INTO products_fts(products_fts) VALUES('rebuild');")
    stats = {
        "dataset_version": DATASET_VERSION, "exported_at": EXPORTED_AT,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "build_seconds": round(time.perf_counter() - t0, 1),
        **{f"rows_{k}": db.execute(f"SELECT count(*) FROM {k}").fetchone()[0] for k in ("shops", "products", "offers")},
        "flagged_offers": db.execute("SELECT count(*) FROM offers WHERE price_flag LIKE 'suspicious%'").fetchone()[0],
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in stats.items()])
    db.commit()
    db.execute("PRAGMA journal_mode=DELETE")
    db.close()
    os.replace(tmp, out)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("data/torob.db"))
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    stats = build(args.csv_dir, args.out)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
