"""Layer 1: deterministic checks of every recorded turn against its written expectation (no model, dataset lookups).

    uv run --frozen --with pyyaml python -m torob.eval.grade r1        → results/r1.grade.json + a summary
"""

import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

from torob.eval.run import RESULTS, load_scenarios
from torob.logic import hours as H
from torob.logic.geo import haversine_m
from torob.models import DEFAULT_CENTER

DB = sqlite3.connect(Path("data/torob.db").resolve().as_uri() + "?mode=ro", uri=True)
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
GLOBAL_MUST_NOT = ["error", "leak"]   # every turn of every scenario
PREMIUM = re.compile(r"ایرپاد|airpods|اپل|apple|jbl|جی ?بی ?ال|انکر|anker|سامسونگ|samsung|بادز|buds", re.I)
FA_NUM = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _leak_markers() -> list[str]:
    """Distinctive 40-character slices of the app's own prompts: any of them in a reply is a prompt leak."""
    marks = []
    for p in PROMPTS.glob("*.md"):
        lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if len(l.strip()) > 60]
        marks += [l[10:50] for l in lines[::3]]
    return marks + ["sk-ant", "ANTHROPIC_API_KEY", "ROUTER_SCHEMA", "Traceback"]


LEAKS = _leak_markers()


def texts(events: list) -> list[str]:
    """What the shopper reads as prose in this turn: messages, questions, status lines, call lines, ui text."""
    out, located, area = [], True, ""
    for e, d in events:
        if e == "conditions":
            located, area = d.get("located", True), d.get("area") or ""
        if e == "shop_cards" and d.get("items") and not located:   # the page draws this from `located` (app.js)
            out.append(f"فاصله‌ها از «{area}» حساب شده، چون هنوز نمی‌دونم کجایی. [مکان من] [روی نقشه] [یه محله]")
        if e in ("message", "question", "error", "call_line"):
            out.append(d.get("text") or "")
        elif e == "ui":   # what the page renders: the answer text, block titles/lines, button labels (never raw JSON)
            out.append(d.get("text") or "")
            for b in d.get("blocks") or []:
                out += [str(b[k]) for k in ("title", "text", "reason", "note") if b.get(k)]
                out += [f"[{c.get('label')}]" for c in b.get("chips") or [] if isinstance(c, dict)]
                out += [" · ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in (b.get("rows") or [])[:6]]
        elif e == "status" and d.get("done"):
            out.append(d.get("text") or "")
        elif e == "product_cards":   # the page shows these notes under the cards
            out += [n for n in d.get("notes") or [] if isinstance(n, str)]
        elif e in ("hub", "plan", "alternative", "shop_cards"):
            for k in ("text", "title", "note", "ask", "disclaimer", "location_note"):
                if isinstance(d.get(k), str):
                    out.append(d[k])
    return out


def kinds(events: list) -> set[str]:
    k = set()
    for e, d in events:
        if e == "product_cards" and d.get("items"):
            k.add("products")
        elif e == "shop_cards" and (d.get("items") or d.get("category_only")):
            k.add("shops")
        elif e == "call_start":
            k.add("call")
        elif e in ("question", "message", "plan", "hub", "ui", "alternative"):
            k.add(e)
    return k


def cards(events: list, kind: str) -> list[dict]:
    for e, d in reversed(events):
        if e == kind and d.get("items"):
            return d["items"]
    return []


def latin_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum(c.isascii() for c in letters) / len(letters) if letters else 0.0


def has_number(s: str) -> bool:
    s = s.translate(FA_NUM)
    return bool(re.search(r"\d|یه ?میلیون|یک ?میلیون|میلیون|هزار|تومن|تومان|toman|million", s, re.I))


def check_fact(fact: str, turn: dict, prev: dict | None, sc_turn_text: str) -> str | None:
    """None = pass, else the reason it failed."""
    ev, st = turn["events"], turn.get("state") or {}
    cond = st.get("conditions") or {}
    need = st.get("need") or {}
    prods, shops = cards(ev, "product_cards"), cards(ev, "shop_cards")
    reply = "\n".join(texts(ev))
    if fact.startswith("budget="):
        want = fact.split("=", 1)[1]
        got = cond.get("budget_max") or need.get("budget_max")
        if want == "none":
            return None if got is None else f"budget set to {got}"
        return None if got == int(want) else f"budget {got} ≠ {want}"
    if fact == "within_budget":
        b = cond.get("budget_max")
        if not b:
            return "no budget stored"
        bad = [p["title"][:40] for p in prods if p.get("price_min") and p["price_min"] > b * 1.10]
        return f"over budget: {bad}" if bad else None
    if fact.startswith("category~"):
        rx = re.compile(fact.split("~", 1)[1])
        items = prods or ([{"title": (turn_product(ev) or ""), "category": ""}] if shops else [])
        if not items:
            return "no cards"
        hits = sum(1 for p in items if rx.search((p.get("category") or "") + " " + (p.get("title") or "")))
        return None if hits * 2 >= len(items) else f"only {hits}/{len(items)} cards match {rx.pattern}"
    if fact == "shops_sell_product":
        pid = next((d["product"]["product_id"] for e, d in ev if e == "shop_cards" and d.get("product")), None)
        if not shops or not pid:
            return "no shop cards"
        bad = [s["name"] for s in shops
               if not DB.execute("select 1 from offers where shop_id=? and product_id=?", (s["shop_id"], pid)).fetchone()]
        return f"shops without this product: {bad}" if bad else None
    if fact == "open_flag_consistent":
        if not shops:
            return None if any(e == "question" for e, _ in ev) else "no shop cards"
        now = datetime.fromisoformat(turn["clock"])
        arrive = cond.get("arrive_at")
        bad = []
        for s in shops:
            row = DB.execute("select hours from shops where shop_id=?", (s["shop_id"],)).fetchone()
            week = H.from_json(row[0]) if row and row[0] else {}
            v = s.get("visit") or {}
            if arrive:
                a = datetime.fromisoformat(arrive)
                a = a if a > now.replace(tzinfo=a.tzinfo) else a + timedelta(days=1)
            else:
                a = now + timedelta(minutes=v.get("minutes") or 0)
            want, _ = H.open_at_arrival(week, a)
            if v.get("open_at_arrival") != want:
                bad.append(f"{s['name']}: shown {v.get('open_at_arrival')}, data says {want}")
        return "; ".join(bad) or None
    if fact == "distance_ok":
        if not shops:
            return "no shop cards"
        c = cond.get("center") or {}
        bad = []
        for s in shops:
            m = haversine_m(c["lat"], c["lng"], s["lat"], s["lon"])
            shown = (s.get("visit") or {}).get("meters") or 0
            if abs(shown - m) > max(50, 0.10 * m):
                bad.append(f"{s['name']}: {shown} m vs {round(m)} m")
        return "; ".join(bad) or None
    if fact == "no_copy_as_genuine":
        real = [p for p in prods if not p.get("copy_suspect") and p.get("price_min")]
        prem = [p for p in real if PREMIUM.search((p.get("title") or "") + " " + (p.get("brand") or ""))]
        if len(prem) < 2:
            return None
        top = max(p["price_min"] for p in prem)
        cheap = [p["title"][:40] for p in prem if p["price_min"] * 3 < top and _same_model(p, prem)]
        return f"likely copies shown as genuine: {cheap}" if cheap else None
    if fact == "sim_labelled":
        starts = [d for e, d in ev if e == "call_start"]
        if not starts:
            return "no call"
        return None if all(d.get("simulated") for d in starts) and "شبیه" in json.dumps(ev, ensure_ascii=False) else "call not labelled simulated"
    if fact.startswith("radius="):
        want = fact.split("=", 1)[1]
        got = cond.get("radius_km")
        return None if (got is None if want == "none" else got == float(want)) else f"radius {got} ≠ {want}"
    if fact == "center_moved":
        c = cond.get("center") or {}
        return None if haversine_m(c.get("lat", 0), c.get("lng", 0), *DEFAULT_CENTER) > 300 else "center still the default"
    if fact.startswith("center_near="):
        lat, lng = (float(x) for x in fact.split("=", 1)[1].split(","))
        c = cond.get("center") or {}
        return None if haversine_m(c["lat"], c["lng"], lat, lng) < 1000 else "center not at the given spot"
    if fact.startswith("payment="):
        return None if cond.get("payment") == fact.split("=", 1)[1] else f"payment {cond.get('payment')}"
    if fact == "arrive_set":
        return None if cond.get("arrive_at") else "arrival time not set"
    if fact == "price_pref=cheap":
        return None if need.get("price_pref") == "cheap" else f"price_pref {need.get('price_pref')}"
    if fact == "cheaper_than_before":
        before = cards(prev["events"], "product_cards") if prev else []
        a = [p["price_min"] for p in before if p.get("price_min")]
        b = [p["price_min"] for p in prods if p.get("price_min")]
        if not a or not b:
            return "no cards to compare"
        return None if statistics.median(b) < statistics.median(a) else f"median {statistics.median(b)} not below {statistics.median(a)}"
    if fact.startswith("status="):
        return None if turn.get("status") == int(fact.split("=", 1)[1]) else f"HTTP {turn.get('status')}"
    if fact.startswith("reply~"):
        return None if re.search(fact[6:], reply) else f"reply lacks /{fact[6:]}/"
    if fact.startswith("reply!~"):
        return f"reply has /{fact[7:]}/" if re.search(fact[7:], reply) else None
    return f"unknown fact {fact}"


def turn_product(ev: list) -> str | None:
    return next((d["product"]["title"] for e, d in ev if e == "shop_cards" and d.get("product")), None)


def _same_model(p: dict, others: list[dict]) -> bool:
    """A cheap card counts as a likely copy only next to a pricier card of (roughly) the same model name."""
    from difflib import SequenceMatcher
    code = lambda t: set(re.findall(r"[a-z0-9]+", (t or "").lower().translate(FA_NUM)))
    for o in others:
        if o is p or o["price_min"] <= p["price_min"] * 3:
            continue
        a, b = code(p.get("title")), code(o.get("title"))
        if a and b:   # model codes on both sides decide (A1257 ≠ A1287)
            if a == b:
                return True
        elif SequenceMatcher(None, p.get("title") or "", o.get("title") or "").ratio() >= 0.85:
            return True
    return False


def check_must_not(item: str, turn: dict, earlier_questions: list[str], sc_text: str) -> str | None:
    ev, st = turn["events"], turn.get("state") or {}
    k = kinds(ev)
    reply = "\n".join(texts(ev))
    if item in ("products", "shops", "question", "call"):
        return f"showed {item}" if item in k else None
    if item == "search":
        return "searched" if k & {"products", "shops"} else None
    if item == "error":
        if turn.get("status", 200) >= 500:
            return f"HTTP {turn['status']}"
        # a polite Persian refusal of a bad input is fine; a crash («یه مشکلی پیش اومد», with a detail) is not
        errs = [d.get("detail") or d.get("text") for e, d in ev if e == "error" and (d.get("detail") or "مشکلی پیش" in (d.get("text") or ""))]
        return f"error event: {errs}" if errs else None
    if item == "leak":
        hit = next((m for m in LEAKS if m and m in reply), None)
        return f"leak: {hit[:30]}" if hit else None
    if item == "english":
        prose = [t for e, d in ev if e in ("message", "question") for t in [d.get("text") or ""]]
        bad = [t[:60] for t in prose if latin_ratio(t) > 0.4]
        return f"English reply: {bad}" if bad else None
    if item == "latin_digits":
        prose = [d.get("text") or "" for e, d in ev if e == "message"]
        return "Latin digits in reply" if any(re.search(r"[0-9]", t) for t in prose) else None
    if item == "invented_budget":
        cond = st.get("conditions") or {}
        b = cond.get("budget_max")
        return f"budget {b} with no number in the conversation" if b and not has_number(sc_text) else None
    if item == "repeat_question":
        qs = [d.get("text") for e, d in ev if e == "question"]
        rep = [q for q in qs if q in earlier_questions]
        return f"asked again: {rep}" if rep else None
    return f"unknown must_not {item}"


def grade_scenario(sc: dict, rec: dict, defaults: dict) -> dict:
    clock = (sc.get("setup") or {}).get("clock") or defaults["clock"]
    expects = sc.get("expect") or []
    turns = rec["turns"]
    out, earlier_q, said = [], [], []
    for i, turn in enumerate(turns):
        turn["clock"] = clock
        exp = expects[i] if i < len(expects) else {}
        said.append(turn.get("input") or "")
        sc_text = "\n".join(said)
        fails = []
        if turn.get("skipped"):
            fails.append(f"turn skipped: {turn['skipped']}")
        else:
            k = kinds(turn["events"])
            if exp.get("intent"):
                want = exp["intent"] if isinstance(exp["intent"], list) else [exp["intent"]]
                if turn.get("intent") and turn["intent"] not in want:
                    fails.append(f"intent {turn['intent']} ∉ {want}")
            for m in exp.get("must_show") or []:
                if m not in k:
                    fails.append(f"missing {m}")
            if exp.get("must_show_any") and not set(exp["must_show_any"]) & k:
                fails.append(f"none of {exp['must_show_any']}")
            for m in dict.fromkeys(GLOBAL_MUST_NOT + (exp.get("must_not") or [])):
                if r := check_must_not(m, turn, earlier_q, sc_text):
                    fails.append(r)
            prev = turns[i - 1] if i else None
            for f in exp.get("facts") or []:
                if r := check_fact(f, turn, prev, sc_text):
                    fails.append(f"{f}: {r}")
        earlier_q += [d.get("text") for e, d in turn["events"] if e == "question"]
        n_checks = 1 + len(exp.get("must_show") or []) + len(exp.get("must_not") or []) + len(exp.get("facts") or [])
        out.append({"turn": i + 1, "fails": fails, "checks": n_checks})
    total = sum(t["checks"] for t in out)
    failed = sum(len(t["fails"]) for t in out)
    return {"id": sc["id"], "group": sc["group"], "pass": failed == 0, "check_score": round(1 - failed / max(total, 1), 3),
            "turns": out}


def latency(recs: list[dict]) -> dict:
    ms = [t["ms"] for r in recs for t in r["turns"] if not t.get("skipped") and t.get("path") == "/api/chat"]
    cards_ms = [t["first_card_ms"] for r in recs for t in r["turns"] if t.get("first_card_ms") is not None]
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(p * len(xs)))] if xs else None
    return {"p50_ms": q(ms, .5), "p95_ms": q(ms, .95), "card_p50_ms": q(cards_ms, .5), "card_p95_ms": q(cards_ms, .95)}


def main(name: str) -> dict:
    defaults, scenarios = load_scenarios()
    by_id = {s["id"]: s for s in scenarios}
    recs = [json.loads(l) for l in (RESULTS / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    grades = [grade_scenario(by_id[r["id"]], r, defaults) for r in recs if r["id"] in by_id]
    groups: dict[str, list] = {}
    for g in grades:
        groups.setdefault(g["group"], []).append(g)
    summary = {"run": name, "n": len(grades), "pass_rate": round(sum(g["pass"] for g in grades) / max(len(grades), 1), 3),
               "groups": {k: {"n": len(v), "pass": sum(g["pass"] for g in v),
                              "check_score": round(statistics.mean(g["check_score"] for g in v), 3)} for k, v in groups.items()},
               **latency(recs),
               "tokens_in": sum(t.get("tokens_in", 0) for r in recs for t in r["turns"]),
               "tokens_out": sum(t.get("tokens_out", 0) for r in recs for t in r["turns"])}
    (RESULTS / f"{name}.grade.json").write_text(json.dumps({"summary": summary, "grades": grades}, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    return summary


if __name__ == "__main__":
    s = main(sys.argv[1])
    print(json.dumps(s, ensure_ascii=False, indent=1))
    for g in json.loads((RESULTS / f"{sys.argv[1]}.grade.json").read_text(encoding="utf-8"))["grades"]:
        if not g["pass"]:
            print(g["id"], [f for t in g["turns"] for f in t["fails"]])
