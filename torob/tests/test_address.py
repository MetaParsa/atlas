"""Address parser on real addresses sampled from shops.csv (2026-10-21)."""

import pytest

from torob.etl.address import building_key, parse_address

CASES = [
    # address, building, floor, unit, plaque
    ("خیابان جمهوری، نبش شیخ هادی، پاساژ علالدین 2، طبقه 6، واحد 622", "علالدین 2", "۶", "622", None),
    ("خیابان جمهوری، نرسیده به پل حافظ، نبش کوچه کلانتری، پاساژ خانه موبایل، طبقه چهارم، پلاک 41",
     "خانه موبایل", "۴", None, "41"),
    ("میدان فردوسی، مجتمع تجارت جهانی فردوسی، طبقه اول، واحد 123و 124", "تجارت جهانی فردوسی", "۱", "123و124", None),
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
