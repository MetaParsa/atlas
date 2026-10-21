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
