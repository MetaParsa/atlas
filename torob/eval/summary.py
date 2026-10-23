"""Round-by-round table and the worst scenarios, from the graded and judged results.

    uv run --frozen --with pyyaml python -m torob.eval.summary r1 r2 [r3]   → results/summary.md
"""

import json
import statistics as S
import sys

from torob.eval import grade
from torob.eval.run import RESULTS, load_scenarios

AXES = ["understood", "useful", "honest", "persian", "next_step"]


def load(name: str) -> dict:
    s = grade.main(name)   # re-grades with the current expectations
    g = json.loads((RESULTS / f"{name}.grade.json").read_text(encoding="utf-8"))["grades"]
    jpath = RESULTS / f"{name}.judge.jsonl"
    j = [json.loads(l) for l in jpath.read_text(encoding="utf-8").splitlines() if l.strip()] if jpath.exists() else []
    return {"summary": s, "grades": {x["id"]: x for x in g}, "judge": {x["id"]: x for x in j}}


def main(names: list[str]) -> str:
    runs = {n: load(n) for n in names}
    _, scenarios = load_scenarios()
    groups = list(dict.fromkeys(s["group"] for s in scenarios))
    lines = ["| گروه | " + " | ".join(f"{n} قبولی" for n in names) + " | " + " | ".join(f"{n} داور" for n in names) + " |",
             "|---|" + "---|" * (2 * len(names))]
    for gname in groups + ["همه"]:
        row = [gname]
        for n in names:
            gs = [x for x in runs[n]["grades"].values() if gname == "همه" or x["group"] == gname]
            row.append(f"{sum(x['pass'] for x in gs)}/{len(gs)}")
        for n in names:
            js = [x["score"] for x in runs[n]["judge"].values() if gname == "همه" or x["group"] == gname]
            row.append(f"{S.mean(js):.1f}" if js else "–")
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for k, label in (("p50_ms", "زمان پاسخ p50 (ms)"), ("p95_ms", "زمان پاسخ p95 (ms)"),
                     ("card_p50_ms", "تا اولین کارت p50"), ("card_p95_ms", "تا اولین کارت p95"),
                     ("tokens_in", "توکن ورودی (غیرکش)"), ("tokens_out", "توکن خروجی")):
        lines.append(f"| {label} | " + " | ".join(str(runs[n]["summary"].get(k)) for n in names) + " |")
    for a in AXES:
        lines.append(f"| داور: {a} (۰–۳) | " + " | ".join(
            f"{S.mean(x[a] for x in runs[n]['judge'].values()):.2f}" if runs[n]["judge"] else "–" for n in names) + " |")
    last = runs[names[-1]]
    by_id = {s["id"]: s for s in scenarios}
    worst = sorted(last["judge"].values(), key=lambda x: (x["score"], x["id"]))[:20]
    lines += ["", f"## ۲۰ سناریوی ضعیف‌تر در {names[-1]}", "", "| سناریو | امتیاز | جواب (نقل داور) | ایده‌آل | مشکل اصلی |", "|---|---|---|---|---|"]
    for w in worst:
        cell = lambda t: str(t).replace("|", "/").replace("\n", " ")[:220]
        lines.append(f"| {w['id']} | {w['score']} | {cell(w['quote'])} | {cell(by_id[w['id']]['ideal'])} | {cell(w['worst_problem'])} |")
    out = "\n".join(lines)
    (RESULTS / "summary.md").write_text(out, encoding="utf-8")
    return out


if __name__ == "__main__":
    print(main(sys.argv[1:]))
