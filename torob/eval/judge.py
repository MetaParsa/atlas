"""Layer 2: Claude Sonnet 5.5 as a strict judge of tone and closeness to the written ideal (structured output).

    uv run --frozen --with pyyaml python -m torob.eval.judge r1 [--only id,id]   → results/r1.judge.jsonl

Five 0–3 scores per scenario: understood · useful · honest · persian (quality + brevity) · next_step. The judge must
quote the exact reply text it judged, so a verdict can be checked against the transcript.
"""

import argparse
import json
import time

from torob.eval.grade import cards, texts
from torob.eval.run import PRICE_IN, PRICE_OUT, RESULTS, load_scenarios, setup_env

MODEL = "claude-sonnet-5-5"
AXES = ["understood", "useful", "honest", "persian", "next_step"]

SYSTEM = """You grade replies of «تربچه», a Persian in-person shopping assistant that works only from Torob's dataset of
Tehran shops (prices with their age, opening hours, sellers; phone numbers are fake, so calls are simulated and must be
labelled «شبیه‌سازی»). You are a strict, experienced Iranian shopkeeper and UX reviewer. A 3 is rare: it means a
careful human assistant could not do clearly better. Grade the WHOLE conversation, every assistant turn.

Scores, 0–3 each:
- understood: did it grasp what the shopper meant (typos, Finglish, market slang like «۲ تومن» = ۲ میلیون, context like «اولی»)?
- useful: are the shown cards/shops/answers the right things for this need (right product type, not accessories; a
  sensible budget; the screen moves the shopper forward)? A reply that shows nothing when something was possible scores ≤1.
- honest: no invented facts (numbers, stock, phone numbers, originality), copies flagged, simulated things labelled,
  limits admitted. Any invented number or a copy presented as genuine scores 0.
- persian: natural colloquial Persian, short (a chat bubble, not an essay), at most one question per turn, no English or
  jargon, Persian digits. Robotic, repetitive or long-winded scores ≤1.
- next_step: is it obvious what to tap or say next (a button, one clear question)?

Compare against the expectation's `ideal` (written before the run), but do not reward copying its wording, and do not
punish a different reply that is at least as good. Cards are summarized as data; judge whether they are the right
cards, not their layout. Quote the single most important assistant sentence you judged, exactly as it appears."""

SCHEMA = {
    "type": "object",
    "properties": {
        "quote": {"type": "string"},
        **{a: {"type": "integer", "enum": [0, 1, 2, 3]} for a in AXES},
        "worst_problem": {"type": "string"},
    },
    "required": ["quote", *AXES, "worst_problem"],
    "additionalProperties": False,
}


def transcript(rec: dict) -> str:
    lines = []
    for t in rec["turns"]:
        lines.append(f"USER: {t['input'][:400]}")
        if t.get("skipped"):
            lines.append(f"(this step could not be taken: {t['skipped']})")
            continue
        if t.get("status") and t["status"] != 200:
            lines.append(f"(HTTP {t['status']}: {str(t.get('body'))[:200]})")
        ev = t["events"]
        for line in texts(ev):
            if line.strip():
                lines.append(f"ASSISTANT: {line[:700]}")
        for p in cards(ev, "product_cards")[:4]:
            lines.append(f"  [product card] {p['title'][:70]} · {p.get('category')} · از {p.get('price_min')} تومان"
                         f" · {p.get('role_label') or ''} · فروشنده نزدیک {p.get('sellers_in_area')}"
                         f"{' · کپی احتمالی' if p.get('copy_suspect') else ''} · چرا: {p.get('why', '')[:90]}")
        for s in cards(ev, "shop_cards")[:3]:
            lines.append(f"  [shop card] {s['name']} · {s.get('where')} · {(s.get('price') or {}).get('text')} "
                         f"({(s.get('price') or {}).get('age')}) · {(s.get('visit') or {}).get('text')}")
        for e, d in ev:
            if e == "question":
                lines.append(f"  [buttons] {' | '.join(c['label'] for c in d.get('chips', []))}")
            elif e == "call_result":
                lines.append(f"  [call result] {d.get('label')}")
    return "\n".join(lines)


def judge_one(client, sc: dict, rec: dict) -> tuple[dict, dict]:
    exp = {"persona": sc.get("persona"), "expect": sc.get("expect"), "ideal": sc.get("ideal"), "tone": sc.get("tone")}
    user = f"SCENARIO EXPECTATION:\n{json.dumps(exp, ensure_ascii=False)}\n\nTRANSCRIPT:\n{transcript(rec)}"
    resp = client.messages.create(model=MODEL, max_tokens=700, system=SYSTEM,
                                  messages=[{"role": "user", "content": user}],
                                  thinking={"type": "between_tools"},
                                  output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text), {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    setup_env(True)
    import os

    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=2)
    _, scenarios = load_scenarios()
    by_id = {s["id"]: s for s in scenarios}
    only = {x for x in args.only.split(",") if x}
    recs = [json.loads(l) for l in (RESULTS / f"{args.name}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out = RESULTS / f"{args.name}.judge.jsonl"
    done = {json.loads(l)["id"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()} if out.exists() else set()
    tin = tout = 0
    with out.open("a", encoding="utf-8") as f:
        for r in recs:
            if r["id"] in done or (only and r["id"] not in only) or r["id"] not in by_id:
                continue
            try:
                v, u = judge_one(client, by_id[r["id"]], r)
            except Exception as e:   # one bad verdict must not stop the batch
                print(r["id"], "judge failed:", type(e).__name__)
                continue
            tin, tout = tin + u["in"], tout + u["out"]
            v["id"], v["group"] = r["id"], r["group"]
            v["score"] = round(sum(v[a] for a in AXES) / (3 * len(AXES)) * 100, 1)
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
            f.flush()
    usd = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
    with (RESULTS / "spend.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run": args.name, "kind": "judge", "in": tin, "out": tout, "usd": round(usd, 4),
                            "at": time.strftime("%H:%M")}) + "\n")
    print(f"judge: {tin} in / {tout} out, ${usd:.3f}")


if __name__ == "__main__":
    main()
