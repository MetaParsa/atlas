"""Checks on the built dataset DB (skipped if data/torob.db hasn't been built)."""

import pytest

from torob import store

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


@pytest.fixture(scope="module")
def con():
    return store.connect()


def test_counts_and_meta(con):
    m = store.meta(con)
    assert (m["rows_shops"], m["rows_products"], m["rows_offers"]) == ("6476", "225903", "331506")


def test_areas(con):
    areas = store.areas(con)
    assert len(areas) >= 30
    assert areas[0]["area"] == "فردوسی" and areas[0]["shops"] == 3282


def test_category_only_shops(con):
    n = con.execute("SELECT count(*) FROM shops WHERE offer_count = 0").fetchone()[0]
    assert n == 2684


def test_credit_shops(con):
    n = con.execute("SELECT count(*) FROM shops WHERE has_credit = 1").fetchone()[0]
    assert n == 266


def test_flagged_prices_not_in_ranges(con):
    junk = con.execute("SELECT count(*) FROM products WHERE price_max > 10000000000").fetchone()[0]
    assert junk == 0


def test_product_stats_use_only_sane_offers(con):
    # Three product IDs share this title; the main one has 59 sellers.
    row = con.execute("SELECT product_id, price_min, price_max FROM products WHERE title = 'شارژر اپل توان 20 وات سه پین' "
                      "ORDER BY sellers_total DESC").fetchone()
    mn, mx = con.execute("SELECT min(price_toman), max(price_toman) FROM offers WHERE product_id = ? AND price_flag = 'ok'",
                         (row["product_id"],)).fetchone()
    assert (row["price_min"], row["price_max"]) == (mn, mx)
    assert con.execute("SELECT count(*) FROM offers WHERE product_id = ? AND price_flag = 'suspicious_high'",
                       (row["product_id"],)).fetchone()[0] >= 1          # the 29.8 M outlier is flagged


def test_redacted_titles_not_searchable(con):
    assert con.execute("SELECT count(*) FROM products WHERE redacted = 1").fetchone()[0] == 849
    hits = store.search_products(con, "عنوان حذف شده برای انتشار", limit=50)
    assert not any(r["title"] == "عنوان حذف‌شده برای انتشار" for r in hits)


def test_per_metre_is_rare(con):
    assert con.execute("SELECT count(*) FROM offers WHERE per_metre = 1").fetchone()[0] < 1000


def test_same_building_groups_are_local(con):
    far = con.execute("""
      SELECT count(*) FROM (SELECT building_gid, max(lat) - min(lat) dlat, max(lon) - min(lon) dlon
                            FROM shops WHERE building_gid IS NOT NULL GROUP BY 1)
      WHERE dlat > 0.01 OR dlon > 0.01""").fetchone()[0]                 # > ~1 km spread
    assert far == 0


def test_search_category_filter_and_hostile_input(con):
    rows = store.search_products(con, "شارژر", category="شارژر گوشی", limit=5)
    assert rows and all(r["category"] == "شارژر گوشی" for r in rows)
    for q in ['"', 'NEAR(a b)', '-*', 'a OR', "ok' --", "()"]:
        store.search_products(con, q)                                      # must not raise


def test_health_recovers_after_upload(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from torob import app as app_mod
    partial = tmp_path / "torob.db"
    partial.write_bytes(b"")                                               # an upload that hasn't finished
    monkeypatch.setattr(store, "DB_PATH", partial)
    app_mod._reset()
    client = TestClient(app_mod.app)
    assert client.get("/health").json()["status"] == "building"
    monkeypatch.setattr(store, "DB_PATH", store.Path("data/torob.db"))
    assert client.get("/health").json()["status"] == "ok"


def test_passage_fields_mostly_parsed(con):
    total, with_floor_or_unit = con.execute(
        "SELECT count(*), sum(floor IS NOT NULL OR unit IS NOT NULL OR plaque IS NOT NULL) "
        "FROM shops WHERE building_kind = 'پاساژ'").fetchone()
    assert total > 2500 and with_floor_or_unit / total > 0.9


@pytest.mark.parametrize("query,expect", [
    ("شارژر آیفون 20 وات", "شارژر اپل توان 20 وات"),
    ("شارژر سامسونگ 25 وات", "EP-TA800"),
    ("هندزفری بلوتوثی انکر", "Soundcore"),
    ("گوشی سامسونگ S25 FE", "S25 FE"),
])
def test_search_smoke(con, query, expect):
    titles = [r["title"] for r in store.search_products(con, query, limit=10)]
    assert any(expect in t for t in titles), titles
