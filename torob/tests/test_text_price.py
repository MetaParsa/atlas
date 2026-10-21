from datetime import datetime, timedelta

from core.text import format_million, format_toman, normalize_fa, price_age_label, to_fa_digits
from torob.logic.price import flag_price, warranty_text


def test_normalize_unifies_letters_digits_and_halfspace():
    assert normalize_fa("هندزفري‌ها ۲۰ وات") == normalize_fa("هندزفری ها 20 وات") == "هندزفری ها 20 وات"
    assert normalize_fa("كابل  AirPods") == "کابل airpods"
    assert normalize_fa("مُدل") == "مدل"
    assert normalize_fa(None) == ""


def test_format_toman_and_million():
    assert format_toman(1490000) == "۱٬۴۹۰٬۰۰۰"
    assert format_million(3_000_000) == "۳ میلیون"
    assert format_million(1_490_000) == "۱٫۴۹ میلیون"
    assert to_fa_digits(214) == "۲۱۴"


def test_price_age_labels():
    now = datetime(2026, 10, 14, 12, 0)
    assert price_age_label(now, now) == ("امروز", False)
    assert price_age_label(now - timedelta(days=1), now) == ("دیروز", False)
    assert price_age_label(now - timedelta(days=21), now) == ("۳ هفته پیش", False)
    assert price_age_label(now - timedelta(days=120), now) == ("۴ ماه پیش", True)
    assert price_age_label(None, now) == ("تاریخ ثبت نشده", True)


def test_price_age_from_dataset_iso_string():
    now = datetime(2026, 10, 14, 12, 0)   # naive = Tehran
    assert price_age_label("2026-09-14T08:57:27.401253+00:00", now) == ("۴ هفته پیش", False)
    assert price_age_label("", now)[0] == "تاریخ ثبت نشده"


def test_single_seller_extremes_only():
    assert flag_price(3_000, None, 1, category_median=25_000_000) == "suspicious_low"    # inverter at 3,000 toman
    assert flag_price(3_000, None, 1, category_median=40_000) == "ok"                    # a cheap part
    assert flag_price(1_400_000_000, None, 1, category_median=300_000_000) == "ok"       # a real generator
    assert flag_price(2_000_000_000, None, 1, category_median=500_000) == "suspicious_high"


def test_per_metre_only_when_explicit():
    from torob.logic.price import is_per_metre
    assert is_per_metre("سیم مسی لخت - سایزهای 16 تا 300 - (سایز 70 قیمت متری)")
    assert is_per_metre("کابل افشان ۲.۵ قیمت هر متر")
    assert not is_per_metre("کابل افشان 6*2 لینکو صدمتری")
    assert not is_per_metre("کابل شارژ ۱.۸ متری")
    assert not is_per_metre("جک ۳.۵ میلی‌متری")
    assert not is_per_metre("پمپ کف کش ۲۸ متری")


def test_flag_price_rules():
    assert flag_price(None, 1e6, 5) == "missing"
    assert flag_price(500, None, 1) == "suspicious_low"
    assert flag_price(733518940747298944, None, 1) == "suspicious_high"   # real junk value from the export
    assert flag_price(29_800_000, 1_500_000, 81) == "suspicious_high"        # Apple 20 W charger outlier
    assert flag_price(1_400_000, 1_500_000, 81) == "ok"
    assert flag_price(400_000, 1_500_000, 81) == "suspicious_low"
    assert flag_price(65_080_000, None, 1) == "ok"   # a 100 m cable roll: no category rule any more


def test_warranty_text():
    assert warranty_text('{"description":null,"period_months":18,"provider":"شرکتی"}') == "۱۸ ماه شرکتی"
    assert warranty_text('{"description":null,"period_months":null,"provider":"تست و تحویل در حضور مشتری"}') \
        == "تست و تحویل در حضور مشتری"
    assert warranty_text('{"description":"ضمانت اصالت کالا","period_months":null,"provider":null}') == "ضمانت اصالت کالا"
    assert warranty_text(None) is None
