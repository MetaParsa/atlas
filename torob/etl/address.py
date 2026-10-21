"""Pull building (passage), floor, unit and plaque out of a free-text Tehran shop address.

Only what is written is extracted; nothing is inferred. Anything not found stays None, and the UI shows «ثبت نشده».
"""

import re
from dataclasses import dataclass

from core.text import normalize_fa, to_latin_digits

_BUILDING_WORDS = r"پاساژ|مجتمع(?:\s*تجاری)?|مرکز\s*خرید|بازار(?:چه)?|ساختمان|تیمچه|سرای|سرا"
# Name runs until a comma or the next address part; «پاساژنادری» (no space) is allowed.
# Letters only: the Persian comma «،» (U+060C) is inside U+0600–U+06FF, so that range must not be used.
_FA_LETTER = r"ء-يپچژکگی"
_BUILDING = re.compile(rf"(?<![{_FA_LETTER}])(?P<kind>{_BUILDING_WORDS})\s*(?P<name>[^،,؛;()\-–]+?)\s*(?=[،,؛;(\-–]|طبقه|واحد|پلاک|$)")
_FLOOR = re.compile(r"طبقه\s*(?:ی\s*)?(?P<floor>زیر\s*زمین(?:\s*(?:اول|دوم|سوم))?|زیر\s*همکف|همکف|منفی\s*(?:[\d۰-۹]+|یک|يک|دو|سه)|-\s*[\d۰-۹]+|[\d۰-۹]+\s*-|[\d۰-۹]+|[ء-يپچژکگی]+)")
_UNIT = re.compile(r"واحد\s*(?P<unit>[\d۰-۹]+(?:\s*[/و-]\s*[\d۰-۹]+)?)")
_PLAQUE = re.compile(r"پلاک\s*(?P<plaque>[\d۰-۹]+(?:\s*/\s*[\d۰-۹]+)?)")

_FLOOR_WORDS = {
    "اول": "۱", "یکم": "۱", "دوم": "۲", "سوم": "۳", "چهارم": "۴", "پنجم": "۵",
    "ششم": "۶", "هفتم": "۷", "هشتم": "۸", "نهم": "۹", "دهم": "۱۰",
}
_NOT_A_FLOOR = {"تجاری", "اداری"}


@dataclass(frozen=True)
class ParsedAddress:
    building_kind: str | None   # پاساژ / مجتمع / بازار / …
    building: str | None        # display name as written, e.g. «علاءالدین ۲»
    building_key: str | None    # spelling-insensitive key for grouping same-passage shops
    floor: str | None           # display label, e.g. «۲», «همکف», «زیرهمکف», «منفی ۱»
    unit: str | None
    plaque: str | None


def building_key(name: str) -> str:
    """«علاءالدین»/«علاالدین»/«علائدین»/«علالدین» -> one key: drop alef/hamza variants and spaces, collapse repeats."""
    s = re.sub(r"[اآأإءئ]", "", name)      # before normalising, which would turn ئ into ی
    s = normalize_fa(s).replace(" ", "")
    return re.sub(r"(.)\1+", r"\1", s)


def _floor_label(raw: str) -> str | None:
    raw = re.sub(r"\s+", " ", raw.strip())
    if raw in _NOT_A_FLOOR:
        return None
    if raw in _FLOOR_WORDS:
        return _FLOOR_WORDS[raw]
    if raw.replace(" ", "") == "زیرهمکف":
        return "زیرهمکف"
    if raw.startswith("زیر") and "زمین" in raw:
        return raw.replace("زیر زمین", "زیرزمین")
    if raw.startswith("منفی"):
        n = raw.removeprefix("منفی").strip()
        n = {"یک": "۱", "يک": "۱", "دو": "۲", "سه": "۳"}.get(n, n.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")))
        return f"منفی {n}" if n else None   # bare «منفی» with no number: don't guess
    digits = to_latin_digits(raw).replace(" ", "")
    if re.fullmatch(r"-\d+|\d+-", digits):
        return "منفی " + digits.strip("-").translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    if digits.isdigit():
        return digits.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    if raw == "همکف":
        return "همکف"
    return None


def parse_address(address: str | None) -> ParsedAddress:
    if not address:
        return ParsedAddress(None, None, None, None, None, None)
    building = kind = key = None
    m = _BUILDING.search(address)
    if m and len(m.group("name").strip()) >= 2:
        kind = re.sub(r"\s+", " ", m.group("kind"))
        building = m.group("name").strip()
        key = building_key(building)
    fm = _FLOOR.search(address)
    um = _UNIT.search(address)
    pm = _PLAQUE.findall(address)
    floor = _floor_label(fm.group("floor")) if fm else None
    unit = to_latin_digits(um.group("unit")).replace(" ", "") if um else None
    # When there are several «پلاک» (street number + shop number), the last one is the shop's.
    plaque = to_latin_digits(pm[-1]).replace(" ", "") if pm else None
    return ParsedAddress(kind, building, key, floor, unit, plaque)
