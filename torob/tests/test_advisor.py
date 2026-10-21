"""S2 advisor on the real dataset, rule-based understanding only (no LLM). Golden needs = regression set."""

import pytest

from torob import store
from torob.agents.advisor import pick_products
from torob.models import Conditions
from torob.router import rule_route
from torob.search import brands_in, model_tokens, near_duplicate, queries_for

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")

# (request, expected category, a word every pick title should contain or None)
GOLDEN = [
    ("یه هندزفری بلوتوثی برای باشگاه می‌خوام، تا ۳ میلیون", "هدفون، هدست و هندزفری", None),
    ("شارژر اصل آیفون", "شارژر گوشی", "اپل"),
    ("دزدگیر سیم کارتی برای مغازه", "دزدگیر اماکن و لوازم جانبی", None),
    ("اسپیکر بلوتوثی", "اسپیکر و سیستم صوتی", None),
    ("پاوربانک ۲۰۰۰۰", "پاوربانک و شارژر همراه", None),
    ("کتونی", "کتانی و کفش اسپرت", None),
    ("کلید مینیاتوری ۲۵ آمپر", "کلید و فیوز مینیاتوری", None),
    ("منبع تغذیه ۱۲ ولت", "منبع تغذیه سوییچینگ", None),
    ("قاب گوشی آیفون", "کیف و کاور گوشی", None),
    ("محافظ برق یخچال", "سیم سیار و چندراهی برق", None),
    ("کابل شارژ تایپ سی", "کابل و تبدیل Type C", None),
    ("مودم ۴G", "مودم LTE/3G/4G/5G", None),
    ("هارد اکسترنال", "هارد اکسترنال", None),
    ("دوربین مداربسته", "دوربین مدار بسته", None),
    ("ساعت مچی مردانه", "ساعت مچی عقربه‌ ای و دیجیتالی", None),
    ("ایرپاد", "هدفون، هدست و هندزفری", None),
]


def run(text):
    r = rule_route(text)
    return pick_products(r.need, Conditions(budget_max=r.conditions_patch.get("budget_max"))), r


def test_golden_needs_hit_rate():
    misses = []
    for text, cat, word in GOLDEN:
        res, _ = run(text)
        ok = res.picks and all(p.c.category == cat for p in res.picks)
        if word:
            ok = ok and all(word in p.c.title for p in res.picks)
        if not ok:
            misses.append((text, res.categories, [p.c.title[:40] for p in res.picks]))
    assert len(misses) <= 2, misses      # done-criterion: ≥ 12 of 15 (we hold ourselves to 14 of 16)


@pytest.mark.parametrize("text", [g[0] for g in GOLDEN])
def test_labels_are_true_and_budget_held(text):
    res, r = run(text)
    assert 1 <= len(res.picks) <= 3
    prices = [p.price for p in res.picks]
    for p in res.picks:
        if p.role == "cheapest":
            assert p.price == min(prices) and prices.count(p.price) == 1
        if p.role == "most_available":
            sellers = [x.area.sellers for x in res.picks]
            assert p.area.sellers == max(sellers)
    budget = r.conditions_patch.get("budget_max")
    if budget:
        assert all(p.price <= budget * 1.10 for p in res.picks)
        assert all((p.over_budget_pct is not None) == (p.price > budget) for p in res.picks)
    titles = [p.c.title_norm for p in res.picks]
    assert not any(near_duplicate(a, b) for i, a in enumerate(titles) for b in titles[i + 1:])


def test_seller_floor_and_honest_note():
    res, _ = run("اسپیکر بلوتوثی")
    assert res.floor_used == 3 and all(p.area.sellers >= 3 for p in res.picks) and not res.notes
    res, _ = run("کتونی")               # the video's sneakers: the data has no shoe with > 2 sellers
    assert res.floor_used <= 2 and res.notes and "فروشنده" in res.notes[0]


def test_named_model_is_matched():
    res, _ = run("گوشی سامسونگ S25 FE")
    assert res.named and "S25 FE" in res.named.c.title and res.question is None
    res, _ = run("هدفون انکر R50i")
    assert res.named and "R50i" in res.named.c.title


def test_spec_rows_only_when_shared_and_different():
    res, _ = run("یه هندزفری بلوتوثی برای باشگاه می‌خوام، تا ۳ میلیون")
    for row in res.compare_rows:
        known = [v for v in row["values"] if v != "ثبت نشده"]
        assert len(known) >= 2 and len({v.replace(" ", "") for v in known}) >= 2
    for p in res.picks:
        assert len(p.spec_lines) == 0 or len(p.spec_lines) >= 2


def test_outside_coverage_falls_back_to_tehran_honestly():
    r = rule_route("شارژر اصل آیفون")
    far = Conditions.model_validate({"center": {"lat": 35.75, "lng": 51.60, "label": "x"}, "radius_km": 1})
    res = pick_products(r.need, far)
    assert not res.in_area and res.notes and all(p.card()["price_scope"] == "tehran" for p in res.picks)


def test_one_question_with_direct_chips():
    res, _ = run("اسپیکر بلوتوثی")      # no budget, prices 2M–105M → budget question
    assert res.question and res.question["text"].startswith("بودجه")
    assert all(c["kind"] == "direct_call" and c["tool"] == "set_budget" for c in res.question["chips"])


def test_search_helpers():
    assert "هدفون بلوتوثی" in queries_for(rule_route("هندزفری بلوتوثی").need)
    assert model_tokens("گوشی سامسونگ S25 FE") == ["s25"]
    assert model_tokens("شارژر 20 وات") == []
    assert brands_in("شارژر اصل آیفون", {"اپل", "سامسونگ"}) == {"اپل"}
    assert not near_duplicate("شارژر اپل توان 20 وات سه پین", "شارژر اپل توان 40 وات سه پین")
