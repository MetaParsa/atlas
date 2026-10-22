"""Step 4: the simulated call to a shop (spec §7, §0.2 R3, R9, R14, A2, A3, A7).

Dataset phone numbers are fake, so the shop side is simulated — and always labelled «شبیه‌سازی». A deterministic
hidden truth (seeded by shop, product and date) decides what the shop "knows"; the call asks only what the data
leaves unknown or old, and the shopkeeper speaks that truth from templates (CALL_LLM=1 lets a model reword the lines,
never the facts). The result comes straight from the truth, so it can't drift from what was said.

Never invented: hours the dataset lacks, credit providers the shop didn't declare, landmarks inside a passage.
"""

import hashlib
import json
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from core.text import format_price_short, format_toman, price_age_label, to_fa_digits
from torob import clock, store
from torob.agents.shops import ShopOffer, where_line
from torob.logic import hours as H

NO_ANSWER_RATE = 0.15
MAX_DRIFT, DRIFT_PER_MONTH = 0.25, 0.015


@dataclass
class Truth:
    answers: bool
    in_stock: bool
    price_today: int | None
    had_registered_price: bool
    closes_today: str | None        # "HH:MM" from the dataset only
    credit: list[str]               # providers the dataset lists for this shop
    location: str | None            # parsed passage/floor/unit only
    hold_until: str | None


@dataclass
class CallResult:
    call_id: str
    shop_id: str
    product_id: str
    answered: bool
    in_stock: bool | None
    price_today: int | None
    price_registered: int | None
    price_diff_pct: int | None
    closes_today: str | None
    credit: list[str] | None
    location: str | None
    hold_until: str | None
    at: str                         # HH:MM (demo clock)
    simulated: bool = True
    outcome: str = "ok"             # ok | no_answer | unavailable

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CallPlan:
    questions: list[str]            # keys: stock, price, hours, credit, location, hold
    reasons: list[str]              # «می‌پرسم چون: …» (R3)
    lines: list[tuple[str, str]] = field(default_factory=list)   # (agent|shop, text)


# --- demo control (A2) ----------------------------------------------------------------------------------------

_overrides: dict[str, str] = {}


def overrides() -> dict[str, str]:
    """shop_id → ok | no_answer | unavailable. From SIM_OVERRIDES (JSON) or set_overrides() (tests, demo)."""
    if _overrides:
        return _overrides
    try:
        return json.loads(os.environ.get("SIM_OVERRIDES", "{}"))
    except ValueError:
        return {}


def set_overrides(mapping: dict[str, str] | None) -> None:
    _overrides.clear()
    _overrides.update(mapping or {})


def _rng(shop_id: str, product_id: str) -> random.Random:
    day = os.environ.get("SIM_SEED_DATE") or clock.now().date().isoformat()
    digest = hashlib.sha256(f"{shop_id}|{product_id}|{day}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


# --- truth ----------------------------------------------------------------------------------------------------

def _stock_chance(o: ShopOffer, now: datetime) -> float:
    if not o.priced:
        return 0.40
    if not o.changed_at:
        return 0.30
    days = (now - datetime.fromisoformat(o.changed_at)).days
    return 0.85 if days <= 7 else 0.70 if days <= 30 else 0.50 if days <= 90 else 0.30


def make_truth(o: ShopOffer, product: dict, now: datetime) -> Truth:
    s = o.shop
    rng = _rng(s["shop_id"], product["product_id"])
    week = H.from_json(s["hours"])
    state, closes = H.status_at(week, now)
    forced = overrides().get(s["shop_id"])
    answers = forced != "no_answer" and state != "closed" and (forced == "ok" or rng.random() >= NO_ANSWER_RATE)
    in_stock = forced != "unavailable" and (forced == "ok" or rng.random() < _stock_chance(o, now))
    base = o.price_min
    if base is None:
        row = store.get().execute("SELECT price_median FROM products WHERE product_id = ?",
                                  (product["product_id"],)).fetchone()
        base = row[0] if row else None
    price = None
    if base:
        months = 0 if not o.changed_at else max(0, (now - datetime.fromisoformat(o.changed_at)).days) / 30
        price = int(round(base * (1 + min(MAX_DRIFT, DRIFT_PER_MONTH * months)) / 10_000) * 10_000) or base
    closes_txt = closes.strftime("%H:%M") if state == "open" and closes else None
    hold = None
    if in_stock and closes:
        h = closes - timedelta(minutes=30)
        hold = h.strftime("%H:%M") if h > now else None
    has_loc = bool(s["floor"] or s["unit"])
    return Truth(answers, in_stock, price, o.price_min is not None, closes_txt,
                 [p for p, _ in o.payments], where_line(s) if has_loc else None, hold)


# --- questions (code, from what's unknown or old) --------------------------------------------------------------

def plan_questions(o: ShopOffer, product: dict, payment_filter: str, now: datetime, want_hold: bool) -> CallPlan:
    q, why = ["stock"], []
    age, old = price_age_label(o.changed_at, now)
    if not o.priced:
        q.append("price")
        why.append("قیمت ثبت نشده")
    elif o.changed_at and (now - datetime.fromisoformat(o.changed_at)).days > 7:
        q.append("price")
        why.append(f"قیمت {age} ثبت شده")
    if not o.shop["hours_known"]:
        q.append("hours")
        why.append("ساعت کاری ثبت نشده")
    elif o.open_state == "tight":
        q.append("hours")
        why.append("تا برسی وقت کمه")
    if payment_filter != "any":
        q.append("credit")
        why.append("دنبال قسطی هستی")
    if o.shop["building"]:
        q.append("location")
        why.append("داخل پاساژه")
    if want_hold:
        q.append("hold")
        why.append("خواستی نگهش دارن")
    if len(why) == 0:
        why.append("موجودی رو هیچ‌وقت توی داده نمی‌شه دید")
    return CallPlan(q, why)


# --- the template shopkeeper (CALL_LLM=0, also the fallback) ----------------------------------------------------

_GREET = ["سلام، وقت بخیر.", "سلام، خسته نباشید."]
_YES = ["آره داریم.", "بله، موجوده.", "آره هست، بیاید."]
_NO = ["نه متأسفانه، تموم کردیم.", "فعلاً نداریم.", "نه، این مدل الان موجود نیست."]
_DONT_KNOW_HOURS = ["ساعتمون ثابت نیست؛ قبل اومدن یه زنگ دیگه بزنید."]   # no invented hours


def _price_words(p: int) -> str:
    return format_price_short(p)


def script(plan: CallPlan, truth: Truth, product_title: str, rng: random.Random) -> list[tuple[str, str]]:
    """Agent and shop lines. Every shop sentence restates a truth field — nothing else."""
    short = product_title.split("|")[0].strip()[:60]
    lines = [("agent", f"{rng.choice(_GREET)} «{short}» رو دارید؟")]
    if not truth.answers:
        return lines
    if not truth.in_stock:
        lines.append(("shop", rng.choice(_NO)))
        return lines
    lines.append(("shop", rng.choice(_YES)))
    asks, answers = [], []
    if "price" in plan.questions and truth.price_today:
        asks.append("قیمت امروزش چنده؟")
        answers.append(f"امروز {_price_words(truth.price_today)}.")
    if "hours" in plan.questions:
        asks.append("امروز تا کی بازید؟")
        answers.append(f"تا {to_fa_digits(truth.closes_today)} هستیم." if truth.closes_today else rng.choice(_DONT_KNOW_HOURS))
    if "credit" in plan.questions:
        asks.append("قسطی یا ترب‌پی دارید؟")
        answers.append(("بله، " + " و ".join(truth.credit) + " داریم.") if truth.credit else "نه، فقط نقدی و کارت.")
    if "location" in plan.questions:
        asks.append("دقیقاً کجای پاساژید؟")
        answers.append(f"{truth.location} هستیم." if truth.location else "توی پاساژ بپرسید، همه می‌شناسن.")
    if "hold" in plan.questions:
        asks.append("می‌شه تا یه ساعت دیگه نگهش دارید؟")
        answers.append(f"باشه، تا {to_fa_digits(truth.hold_until)} نگه می‌دارم." if truth.hold_until
                       else "نمی‌تونم قول بدم، زودتر بیاید.")
    if asks:
        lines.append(("agent", " ".join(asks)))
        lines.append(("shop", " ".join(answers)))
    return lines


def phrase_with_llm(llm, lines: list[tuple[str, str]], truth: Truth, session_id: str) -> list[tuple[str, str]]:
    """CALL_LLM=1 (demo takes only, needs LLM_ENABLED): reword the shop's lines in bazaar Persian, same facts.
    Any failure keeps the template lines."""
    try:
        prompt = ("تو فروشنده‌ی یک مغازه در تهران هستی و تلفنی جواب می‌دهی. جمله‌های زیر را محاوره‌ای‌تر بازنویسی کن، "
                  "هیچ عدد، ساعت، مکان یا روش پرداختی اضافه یا عوض نکن، و فقط JSON بده: {\"lines\": [...]}\n"
                  f"حقیقت‌ها: {json.dumps(asdict(truth), ensure_ascii=False)}\n"
                  f"جمله‌ها: {json.dumps([t for w, t in lines if w == 'shop'], ensure_ascii=False)}")
        res = llm.complete("call", [{"role": "user", "content": prompt}], session_id=session_id, max_tokens=200,
                           json_mode=True)
        new = [str(x) for x in res.json().get("lines", [])]
        shop_idx = [i for i, (w, _) in enumerate(lines) if w == "shop"]
        if len(new) != len(shop_idx) or not all(_same_numbers(lines[i][1], new[k]) for k, i in enumerate(shop_idx)):
            return lines
        out = list(lines)
        for k, i in enumerate(shop_idx):
            out[i] = ("shop", new[k])
        return out
    except Exception:
        return lines


def _same_numbers(a: str, b: str) -> bool:
    """A rewording may not add, drop or change any digit sequence (prices, times, units)."""
    import re
    from core.text import to_latin_digits
    nums = lambda s: sorted(re.findall(r"\d+", to_latin_digits(s)))
    return nums(a) == nums(b)


def run_call(o: ShopOffer, product: dict, payment_filter: str, call_id: str, *, want_hold: bool = False,
             llm=None, session_id: str = "-") -> tuple[CallPlan, CallResult]:
    now = clock.now()
    truth = make_truth(o, product, now)
    plan = plan_questions(o, product, payment_filter, now, want_hold)
    rng = _rng(o.shop["shop_id"] + "|script", product["product_id"])
    plan.lines = script(plan, truth, product["title"], rng)
    if llm is not None and os.environ.get("CALL_LLM") == "1" and getattr(llm, "enabled", False):
        plan.lines = phrase_with_llm(llm, plan.lines, truth, session_id)
    outcome = "no_answer" if not truth.answers else ("ok" if truth.in_stock else "unavailable")
    diff = None
    if truth.answers and truth.in_stock and truth.price_today and o.price_min:
        diff = round((truth.price_today / o.price_min - 1) * 100)
    res = CallResult(
        call_id=call_id, shop_id=o.shop["shop_id"], product_id=product["product_id"], answered=truth.answers,
        in_stock=(truth.in_stock if truth.answers else None),
        price_today=(truth.price_today if truth.answers and truth.in_stock and "price" in plan.questions else None),
        price_registered=o.price_min, price_diff_pct=diff if "price" in plan.questions else None,
        closes_today=(truth.closes_today if truth.answers else None),
        credit=(truth.credit if truth.answers and "credit" in plan.questions else None),
        location=(truth.location if truth.answers and truth.in_stock and "location" in plan.questions else None),
        hold_until=(truth.hold_until if truth.answers and truth.in_stock and want_hold else None),
        at=now.strftime("%H:%M"), outcome=outcome)
    return plan, res


def result_label(r: CallResult) -> str:
    """The only allowed confirmation wording (honesty rule, A7)."""
    t = to_fa_digits(r.at)
    if r.outcome == "no_answer":
        return f"جواب نداد (شبیه‌سازی) {t}"
    if r.outcome == "unavailable":
        return f"ناموجود (شبیه‌سازی) {t}"
    return f"تأیید فروشنده (شبیه‌سازی) {t}"


def price_change_text(r: CallResult) -> str | None:
    """«۱٬۴۹۰٬۰۰۰ → ۱٬۵۱۰٬۰۰۰ (شبیه‌سازی)» shown only inside the call block and simulated cells."""
    if r.price_today is None:
        return None
    if r.price_registered is None:
        return f"قیمت امروز {format_toman(r.price_today)} (قیمت ثبت‌شده نداشت · شبیه‌سازی)"
    return f"{format_toman(r.price_registered)} → {format_toman(r.price_today)} (شبیه‌سازی)"
