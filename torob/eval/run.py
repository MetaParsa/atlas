"""Drive every scenario in scenarios.yaml through the real app (in-process HTTP, real Claude), one at a time.

    uv run --frozen python -m torob.eval.run --name r1 [--only clear-01,copy-02] [--group clear] [--no-llm]

Records per turn: every SSE event, the router's intent, the session state after the turn, latency and tokens.
Writes torob/eval/results/<name>.jsonl (one line per scenario). Grading is separate (grade.py, judge.py).
The API key is read from the Windows user environment into this process only; it is never printed or written.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).parent
RESULTS = HERE / "results"
PRICE_IN, PRICE_OUT = 2.0, 10.0   # Claude Sonnet 5.5, $ per million tokens


def _user_env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def setup_env(use_llm: bool) -> None:
    os.environ["TOROB_LIVE"] = "0"
    os.environ["CALL_LINE_DELAY"] = "0"
    os.environ["LLM_DAILY_TOKENS"] = "50000000"
    os.environ["LLM_SESSION_TOKENS"] = "2000000"
    if use_llm:
        key = _user_env("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("ANTHROPIC_API_KEY not found in the user environment")
        os.environ["ANTHROPIC_API_KEY"] = key
        os.environ["LLM_ENABLED"] = "1"
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["LLM_ENABLED"] = "0"


TOKENS = {"in": 0, "out": 0, "calls": 0, "cache_read": 0, "cache_write": 0}


def usd(t: dict) -> float:
    """Sonnet 5.5: cache reads at a tenth of input, 5-minute cache writes at 1.25×."""
    return (t["in"] * PRICE_IN + t.get("cache_read", 0) * PRICE_IN * 0.1 + t.get("cache_write", 0) * PRICE_IN * 1.25
            + t["out"] * PRICE_OUT) / 1e6
INTENTS: list[str] = []


def instrument() -> None:
    """Count real tokens per Claude call and record the router's decision, without touching app code."""
    from core import llm as core_llm
    from torob import router

    orig = core_llm.LLMClient._anthropic

    def counted(self, *a, **kw):
        text, calls, usage, used = orig(self, *a, **kw)
        TOKENS["in"] += int(usage.get("prompt_tokens") or 0)
        TOKENS["out"] += int(usage.get("completion_tokens") or 0)
        TOKENS["cache_read"] += int(usage.get("cache_read") or 0)
        TOKENS["cache_write"] += int(usage.get("cache_write") or 0)
        TOKENS["calls"] += 1
        return text, calls, usage, used
    core_llm.LLMClient._anthropic = counted

    orig_understand = router.understand

    def understand(*a, **kw):
        out = orig_understand(*a, **kw)
        INTENTS.append(out.intent)
        return out
    router.understand = understand


def parse_sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        ev, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if ev:
            try:
                out.append((ev, json.loads("\n".join(data)) if data else {}))
            except ValueError:
                out.append((ev, {"raw": "\n".join(data)}))
    return out


def chips_in(obj, found=None) -> list[dict]:
    """Every chip/button-shaped dict inside the events (question chips, card buttons, ui chips, hub chips)."""
    found = [] if found is None else found
    if isinstance(obj, dict):
        if "label" in obj and "kind" in obj and obj.get("kind") in ("say", "direct_call"):
            found.append(obj)
        for v in obj.values():
            chips_in(v, found)
    elif isinstance(obj, list):
        for v in obj:
            chips_in(v, found)
    return found


def find_chip(events: list, label: str) -> dict | None:
    cs = chips_in([d for _, d in events])
    for c in cs:
        if c["label"] == label:
            return c
    for c in cs:
        if label in c["label"] or c["label"] in label:
            return c
    return None


def latest_cards(history: list[list]) -> list[dict]:
    for events in reversed(history):
        for e, d in reversed(events):
            if e in ("product_cards", "shop_cards") and d.get("items"):
                return d["items"]
    return []


def long_text(n: int) -> str:
    s = ("هندزفری بلوتوث ارزون میخوام " * 80)[:n]
    return s


def run_scenario(client, adb, sc: dict, defaults: dict, areas: list[dict]) -> dict:
    from torob import clock
    clock.set_now(datetime.fromisoformat((sc.get("setup") or {}).get("clock") or defaults["clock"]))
    client.cookies.clear()
    history: list[list] = []
    turns_out = []
    for raw in sc["turns"]:
        body, path, note = None, "/api/chat", raw
        if raw.startswith("chip:"):
            label = raw[5:].strip()
            chip = find_chip(history[-1] if history else [], label)
            if not chip:
                turns_out.append({"input": raw, "skipped": f"chip «{label}» not on screen", "events": [], "ms": 0})
                history.append([])
                continue
            body = {"chip": chip, "bubble": chip["label"]}
        elif m := re.match(r"card(\d+):\s*(.*)", raw):
            items = latest_cards(history)
            n = int(m.group(1)) - 1
            if n >= len(items) or not items[n].get("button"):
                turns_out.append({"input": raw, "skipped": "card not on screen", "events": [], "ms": 0})
                history.append([])
                continue
            chip = items[n]["button"]
            body = {"chip": chip, "bubble": chip["label"]}
        elif raw.startswith("set:"):
            path, body = "/api/refine", {"conditions": {}}
            for kv in raw[4:].split(","):
                k, v = (x.strip() for x in kv.split("="))
                body["conditions"][k] = None if v == "none" else (float(v) if re.fullmatch(r"[\d.]+", v) else v)
        elif raw.startswith("loc:"):
            name = raw[4:].strip()
            a = next((a for a in areas if a.get("area") == name or a.get("label") == name), None)
            if not a:
                turns_out.append({"input": raw, "skipped": "area not found", "events": [], "ms": 0})
                history.append([])
                continue
            body = {"chip": {"label": name, "kind": "direct_call", "tool": "set_location",
                             "args": {"lat": a["lat"], "lng": a.get("lng", a.get("lon")), "label": name, "source": "area"}},
                    "bubble": name}
        elif raw.startswith("gps:"):
            lat, lng = (float(x) for x in raw[4:].split(","))
            body = {"chip": {"label": "مکان من", "kind": "direct_call", "tool": "set_location",
                             "args": {"lat": lat, "lng": lng, "label": "مکان من", "source": "gps"}}, "bubble": "مکان من"}
        elif raw.startswith("http:"):
            body = json.loads(raw[5:])
            if body.get("text", "").startswith("LONG"):
                body["text"] = long_text(int(body["text"][4:]))
        else:
            text = long_text(int(raw[4:])) if raw.startswith("LONG") else raw
            body, note = {"text": text}, text
        INTENTS.clear()
        before = dict(TOKENS)
        t0 = time.perf_counter()
        r = client.post(path, json=body)
        ms = int((time.perf_counter() - t0) * 1000)
        events = parse_sse(r.text) if r.headers.get("content-type", "").startswith("text/event-stream") else []
        sid = client.cookies.get("sid")
        state = adb.load(sid)[1].model_dump(mode="json") if sid else None
        first_status = next((d.get("ms") for e, d in events if e in ("status", "message")), None)
        first_card = next((d.get("ms") for e, d in events if e in ("product_cards", "shop_cards", "hub", "call_start")), None)
        turns_out.append({"input": note, "path": path, "status": r.status_code, "body": None if events else r.text[:500],
                          "events": events, "intent": INTENTS[0] if INTENTS else None, "ms": ms,
                          "first_status_ms": first_status, "first_card_ms": first_card,
                          "tokens_in": TOKENS["in"] - before["in"], "tokens_out": TOKENS["out"] - before["out"],
                          "cache_read": TOKENS["cache_read"] - before["cache_read"],
                          "usd": round(usd(TOKENS) - usd(before), 5),
                          "state": _compact_state(state)})
        history.append(events)
    clock.set_now(None)
    return {"id": sc["id"], "group": sc["group"], "turns": turns_out}


def _compact_state(s: dict | None) -> dict | None:
    if not s:
        return None
    return {"need": s.get("need"), "conditions": s.get("conditions"), "last_block_kind": s.get("last_block_kind"),
            "selected_product_id": s.get("selected_product_id"), "asked": s.get("asked")}


def load_scenarios() -> tuple[dict, list[dict]]:
    doc = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))
    return doc["defaults"], doc["scenarios"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--budget", type=float, default=8.0, help="stop when this run alone has spent this many dollars")
    args = ap.parse_args()
    setup_env(not args.no_llm)
    instrument()

    from fastapi.testclient import TestClient
    from torob import app as app_mod
    from torob.appdb import AppDB

    tmp = Path(tempfile.mkdtemp(prefix="torob-eval-"))
    adb = AppDB(tmp / "app.db")
    app_mod.appdb = lambda: adb
    app_mod.chat_limit.allow = lambda key: True
    client = TestClient(app_mod.app)
    areas = client.get("/api/areas").json()

    defaults, scenarios = load_scenarios()
    only = {x for x in args.only.split(",") if x}
    todo = [s for s in scenarios if (not only or s["id"] in only) and (not args.group or s["group"] == args.group)]
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.name}.jsonl"
    done = set()
    if out.exists():   # resumable: skip scenarios already recorded under this name
        done = {json.loads(l)["id"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}
    with out.open("a", encoding="utf-8") as f:
        for i, sc in enumerate(todo, 1):
            if sc["id"] in done:
                continue
            res = run_scenario(client, adb, sc, defaults, areas)
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
            f.flush()
            cost = usd(TOKENS)
            print(f"[{i}/{len(todo)}] {sc['id']}: {sum(t['ms'] for t in res['turns'])} ms, run ${cost:.3f}", flush=True)
            if cost > args.budget:
                print("budget reached; stopping")
                break
    cost = usd(TOKENS)
    with (RESULTS / "spend.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run": args.name, "kind": "app", "in": TOKENS["in"], "out": TOKENS["out"],
                            "calls": TOKENS["calls"], "cache_read": TOKENS["cache_read"],
                            "cache_write": TOKENS["cache_write"], "usd": round(cost, 4), "at": time.strftime("%H:%M")}) + "\n")
    print(f"done: {TOKENS['calls']} calls, {TOKENS['in']} in / {TOKENS['out']} out tokens, ${cost:.3f}")


if __name__ == "__main__":
    main()
