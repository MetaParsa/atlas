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
