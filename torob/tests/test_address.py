"""Address parser on real addresses sampled from shops.csv (2026-10-21)."""

import pytest

from torob.etl.address import building_key, parse_address

CASES = [
    # address, building, floor, unit, plaque
    ("خیابان جمهوری، نبش شیخ هادی، پاساژ علالدین 2، طبقه 6، واحد 622", "علالدین 2", "۶", "622", None),
    ("خیابان جمهوری، نرسیده به پل حافظ، نبش کوچه کلانتری، پاساژ خانه موبایل، طبقه چهارم، پلاک 41",
     "خانه موبایل", "۴", None, "41"),
    ("میدان فردوسی، مجتمع تجارت جهانی فردوسی، طبقه اول، واحد 123و 124", "تجارت جهانی فردوسی", "۱", "123 و 124", None),
    ("خیابان جمهوری، تقاطع حافظ، بازار موبایل ایران، طبقه زیر همکف، پلاک ۱۱۰", "موبایل ایران", "زیرهمکف", None, "110"),
    ("جمهوری،تقاطع حافظ،پاساژنادری،طبقه اول،واحد10", "نادری", "۱", "10", None),
    ("خیابان سعدی جنوبی، خیابان بوشهری، کوچه بوشهری، پاساژ نیرو صنعت، طبقه زیر زمین دوم، پلاک ۱۲",
     "نیرو صنعت", "زیرزمین دوم", None, "12"),
    ("مترو سعدی، کوچه برلن، پاساژ کمپانی (برلیان سابق)، طبقه همکف، پلاک ۶۹", "کمپانی", "همکف", None, "69"),
    ("خیابان جمهوری اسلامی، پاساژ حافظ، پلاک ۵", "حافظ", None, None, "5"),
    ("خیابان جمهوری اسلامی، خیابان لاله زار، بین اتحادیه و مهران، پلاک ۲۳۷، پاساژ بازار لاله زار، پلاک ۱۱",
     "بازار لاله زار", None, None, "11"),
    ("لاله زار جنوبی، کوچه شهید محمدرضا علیپور، پلاک٢٢، ساختمان ممتاز، طبقه همکف، پلاک١/١٠",
     "ممتاز", "همکف", None, "1/10"),
    ("خیابان جمهوری، بین حافظ و سی تیر، پاساژ امجد، طبقه منفی ۱، پلاک 13", "امجد", "منفی ۱", None, "13"),
    ("خیابان جمهوری، پاساژ امجد، طبقه -1، پلاک 13", "امجد", "منفی ۱", None, "13"),
    ("خیابان جمهوری، پاساژ چارسو، طبقه منفی یک، واحد ۱۲", "چارسو", "منفی ۱", "12", None),
    # Landmarks before the real building (review S0 #2)
    ("خیابان جمهوری، روبروی پاساژ علاالدین، پاساژ حافظ، طبقه اول، پلاک ۵", "حافظ", "۱", None, "5"),
    ("لاله زار، بالاتر از پاساژ طلا، پاساژ بازارچه لاله زار، پلاک ۸", "بازارچه لاله زار", None, None, "8"),
    ("جمهوری، نرسیده به ساختمان بورس، مجتمع تجاری علاالدین ۳، طبقه ۲", "علاالدین ۳", "۲", None, None),
    ("سعدی، روبروی ساختمان آلومینیوم، پاساژ کاوه، پلاک ۴", "کاوه", None, None, "4"),
    # Decimals and «X و Y» in unit/plaque (#5)
    ("پاساژ علاالدین، طبقه ۲، واحد ۲۱۰.۱", "علاالدین", "۲", "210.1", None),
    ("پاساژ امجد، پلاک ۳.۳ و۳.۴", "امجد", None, None, "3.3 و 3.4"),
    ("پاساژ امجد، پلاک ۵۲ و ۵۳", "امجد", None, None, "52 و 53"),
    # Half-spaces (#6)
    ("خیابان سعدی، پاساژ‌سعدی‌قدیم، طبقه‌ی اول، پلاک ۹", "سعدی قدیم", "۱", None, "9"),
    ("پاساژ فراز، طبقه‌دوم، واحد ۶", "فراز", "۲", "6", None),
    # Two different floors in one address: ambiguous -> None (#6)
    ("پاساژ نادری، طبقه اول، پلاک 20، طبقه همکف، پلاک 8", "نادری", None, None, "8"),
]


@pytest.mark.parametrize("address,building,floor,unit,plaque", CASES)
def test_parse_real_addresses(address, building, floor, unit, plaque):
    a = parse_address(address)
    assert (a.building, a.floor, a.unit, a.plaque) == (building, floor, unit, plaque)


def test_no_building_no_guess():
    a = parse_address("خیابان ولیعصر، نرسیده به پارک ساعی، پلاک ۲۱")
    assert a.building is None and a.floor is None and a.plaque == "21"
    assert parse_address(None).building is None


def test_building_key_merges_spellings():
    keys = {building_key(n) for n in ["علاءالدین", "علاالدین", "علائدین", "علالدین"]}
    assert len(keys) == 1
    assert building_key("علاالدین 2") != building_key("علاالدین")


def test_building_groups_need_same_area_and_distance():
    from torob.etl.build_db import building_groups
    shops = [
        {"shop_id": "a", "building_key": "نور", "area": "فردوسی", "lat": 35.6930, "lon": 51.4200},
        {"shop_id": "b", "building_key": "نور", "area": "فردوسی", "lat": 35.6935, "lon": 51.4205},   # ~70 m
        {"shop_id": "c", "building_key": "نور", "area": "فردوسی", "lat": 35.7400, "lon": 51.4200},   # ~5 km
        {"shop_id": "d", "building_key": "نور", "area": "تجریش", "lat": 35.6931, "lon": 51.4201},    # other area
    ]
    g = building_groups(shops)
    assert g["a"] == g["b"]
    assert len({g["a"], g["c"], g["d"]}) == 3
