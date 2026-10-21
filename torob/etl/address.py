"""Pull building (passage), floor, unit and plaque out of a free-text Tehran shop address.

Only what is written is extracted; nothing is inferred. Anything not found or ambiguous stays None, and the
UI shows «ثبت نشده».
"""

import re
from dataclasses import dataclass

from core.text import normalize_fa, to_latin_digits

_BUILDING_WORDS = r"پاساژ|مجتمع(?:\s*تجاری)?|مرکز\s*خرید|بازار(?:چه)?|ساختمان|تیمچه|سرای|سرا"
# Letters only: the Persian comma «،» (U+060C) is inside U+0600–U+06FF, so that whole range must not be used.
_FA_LETTER = "ء-يپچژکگی"
# Name runs until a comma or the next address part; «پاساژنادری» (no space) is allowed.
_BUILDING = re.compile(
    rf"(?<![{_FA_LETTER}])(?P<kind>{_BUILDING_WORDS})\s*(?P<name>[^،,؛;()\-–]+?)\s*(?=[،,؛;(\-–]|طبقه|واحد|پلاک|$)")
# A building named right after one of these is a landmark («روبروی پاساژ علاالدین»), not the shop's building.
_LANDMARK = re.compile(r"روبه?\s*روی?|مقابل|جنب|نبش|کنار|پشت|نزدیک|بعد\s*از|قبل\s*از|نرسیده\s*به|بالاتر\s*از|پایین\s*تر\s*از|پائین\s*تر\s*از|بین")
_FLOOR = re.compile(
    rf"طبقه\s*(?:ی\s*)?(?P<floor>زیر\s*زمین(?:\s*(?:اول|دوم|سوم))?|زیر\s*همکف|همکف|منفی\s*(?:[\d۰-۹]+|یک|يک|دو|سه)"
    rf"|-\s*[\d۰-۹]+|[\d۰-۹]+\s*-|[\d۰-۹]+|[{_FA_LETTER}]+)")
_NUM = r"[\d۰-۹]+(?:[.٫/][\d۰-۹]+)?"
_UNIT = re.compile(rf"واحد\s*(?P<unit>{_NUM}(?:\s*(?:و|-|/)\s*{_NUM})*)")
_PLAQUE = re.compile(rf"پلاک\s*(?P<plaque>{_NUM}(?:\s*(?:و|-|/)\s*{_NUM})*)")

_FLOOR_WORDS = {
    "اول": "۱", "یکم": "۱", "دوم": "۲", "سوم": "۳", "چهارم": "۴", "پنجم": "۵",
    "ششم": "۶", "هفتم": "۷", "هشتم": "۸", "نهم": "۹", "دهم": "۱۰",
}
_NOT_A_FLOOR = {"تجاری", "اداری"}
_TO_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


@dataclass(frozen=True)
class ParsedAddress:
    building_kind: str | None   # پاساژ / مجتمع / بازار / …
    building: str | None        # display name as written, e.g. «علاءالدین ۲»
    building_key: str | None    # spelling-insensitive key; grouping also needs area + distance (etl)
    floor: str | None           # display label, e.g. «۲», «همکف», «زیرهمکف», «منفی ۱»
    unit: str | None
    plaque: str | None


def building_key(name: str) -> str:
    """«علاءالدین»/«علاالدین»/«علائدین»/«علالدین» -> one key: drop alef/hamza variants and spaces, collapse repeats.

    Lossy on purpose (it also merges unrelated short names), so same-building grouping must add area + distance.
    """
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
        n = {"یک": "۱", "يک": "۱", "دو": "۲", "سه": "۳"}.get(n, n.translate(_TO_FA))
        return f"منفی {n}" if n else None   # bare «منفی» with no number: don't guess
    digits = to_latin_digits(raw).replace(" ", "")
    if re.fullmatch(r"-\d+|\d+-", digits):
        return "منفی " + digits.strip("-").translate(_TO_FA)
    if digits.isdigit():
        return digits.translate(_TO_FA)
    if raw == "همکف":
        return "همکف"
    return None


def _segment_before(text: str, pos: int) -> str:
    """Text between the previous comma and `pos`."""
    return re.split(r"[،,؛;]", text[:pos])[-1]


def _clean_number(s: str) -> str:
    s = to_latin_digits(s).replace("٫", ".")
    return re.sub(r"\s*(و|-|/)\s*", lambda m: " و " if m.group(1) == "و" else m.group(1), s).strip()


def parse_address(address: str | None) -> ParsedAddress:
    if not address:
        return ParsedAddress(None, None, None, None, None, None)
    text = address.replace("‌", " ").replace("‏", "")   # ZWNJ is not \s; «طبقه‌ی اول» must parse

    building = kind = key = None
    candidates = [m for m in _BUILDING.finditer(text)
                  if len(m.group("name").strip()) >= 2 and not _LANDMARK.search(_segment_before(text, m.start()))]
    if candidates:
        m = candidates[-1]   # the shop's own building usually comes last («…، پاساژ بازار لاله زار، پلاک ۱۱»)
        kind = re.sub(r"\s+", " ", m.group("kind"))
        building = m.group("name").strip()
        key = building_key(building)

    floors = {f for f in (_floor_label(m.group("floor")) for m in _FLOOR.finditer(text)) if f}
    floor = floors.pop() if len(floors) == 1 else None   # two different floors in one address: ambiguous
    um = _UNIT.search(text)
    plaques = _PLAQUE.findall(text)
    unit = _clean_number(um.group("unit")) if um else None
    # Several «پلاک» = street number + shop number: the last one is the shop's.
    plaque = _clean_number(plaques[-1]) if plaques else None
    return ParsedAddress(kind, building, key, floor, unit, plaque)
