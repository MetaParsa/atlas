# the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ (دستیار⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣) evaluation and tuning: prompt for a Claude Code session

Paste everything below the line into a fresh Claude Code session opened in `C:\Users\parsa\Desktop\Me\Hackathon`.

---

You are evaluating and tuning **دستیار⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣**, our in-person shopping assistant for the the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ challenge. The code is in `atlas⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣/torob/`; read `CLAUDE.md`, `atlas⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣/torob/README.md`, `torob/docs/STRESS-TEST.md`, `the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣/CHALLENGE-BRIEF.md` (rubric: Product 75 · UX 75 · Intelligence 50 · Deploy 40 · Tech 40 · Dataset 20) and `the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣/VIDEO-NOTES.md` first. The earlier stress test only checked that answers were "not broken". This time every scenario gets a **written expected outcome before it runs**, and every answer is **scored against that expectation**. Then fix what fails, rerun, and repeat until the scores stop improving.

## Ground rules
- Model: the app's own Sonnet setup (`core/llm.py`). The API key is in the Windows user environment as `ANTHROPIC_API_KEY`; read it into the process env, never print, log or commit it. Never use BoxAPI.
- **Budget: stop at $8 of model spend in total** (log tokens per run; Sonnet 5.5 pricing from the claude-api skill). Run scenarios **one at a time**, never in parallel threads (parallel runs deadlocked last time).
- Run against a **local** server or `run_turn` in-process, never against the live site, so its daily token cap stays intact. `TOROB_LIVE=0`, raise `LLM_DAILY_TOKENS` locally only.
- Never call the marketplace's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ `contact-info`; no live the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ scraping.
- Fix behaviour by improving the model's inputs, prompts and one-job-per-call design, **not by per-phrase word lists** (user's standing rule). Rule-based code is fine for hard facts (prices, distances, opening hours, validation).
- Every fix keeps `uv run --frozen pytest -q torob` green and adds a regression test where the behaviour is deterministic.
- Commit locally with clear messages. **Do not push to GitHub or deploy to the VPS without asking the user first.**
- Write everything to `torob/eval/`: scenarios, runner, results, report. Git-ignore raw transcripts if large.

## Step 1: the scenario set (write it before running anything)
Create `torob/eval/scenarios.yaml` with **at least 120 scenarios**. Each scenario has:
- `id`, `persona` (who is typing and how they write), `turns` (1–6 user messages, including chip taps written as `chip: <label>` and condition changes like `set: radius=1`), optional `setup` (location source, demo clock time, prior state);
- `expect`: concrete, checkable outcomes per turn, for example:
  - `intent`: what the router must decide;
  - `must_show`: block types that must appear (products, shops, compare, map, question with chips…);
  - `must_not`: things that must not happen (a search on small talk, an invented budget, a repeated question, a copy shown as genuine, English text, a leaked prompt…);
  - `facts`: hard checks against the dataset (e.g. "every product card price is within budget", "every shop shown sells the selected product", "a shop marked open is open at arrival time", "cheapest card is ≤ the median price of matches", "distance shown matches haversine from the current center ±10%");
  - `tone`: Persian, short, colloquial, one question at most, no jargon;
  - `ideal`: a one- or two-sentence description of the best possible reply, written by you as a careful human shopkeeper/assistant would answer.

Cover these groups (rough counts):
1. **Clear needs** (15): a named product, a type plus budget, a gift with recipient and budget. Expected: right category, 2–3 sensible models, honest prices.
2. **Vague and messy writing** (15): typos, Finglish, Arabic letters, only emojis, very long rambling messages, voice-to-text style without punctuation, multi-line messages.
3. **Budget language** (10): «۲ تومن», «۵۰۰ تومن», «زیر یه میلیون», «هرچی ارزون‌تر», «گرونه», «پول مهم نیست». Check the stored budget value exactly.
4. **Genuine vs copy** (10): AirPods, Apple chargers, Samsung, JBL, Anker, Xiaomi; "is this original?". Expected: copies set aside with the note, genuine prices plausible against the dataset.
5. **Location and time** (15): no location given (the default must be stated, the «کجایی؟» ask must appear with shops), GPS, area names, «نزدیک ولیعصر», a place the data doesn't cover, «ساعت ۸ شب میرسم», 23:30 demo clock, Friday.
6. **Follow-ups on screen** (15): «اولی», «دومی رو بپرس», «فروشنده‌های سومی», «کدومش برای باشگاه بهتره؟», «ارزون‌ترش؟», «قسطی دارن؟», «نزدیک‌ترینش»; condition strip changes mid-conversation.
7. **Bourse and maps** (8): «بورس موبایل کجاست؟», «کجا لوازم آشپزخونه ارزونه؟», building/passage questions.
8. **Simulated shop call** (8): call flow, shop says no, price changed, hold the item; the call must be clearly marked simulated.
9. **Off-topic and safety** (12): code, essays, the dollar rate, politics, medical advice, insults, harmful requests, flirting. Expected: short polite refusal, back to shopping, no search.
10. **Data protection and injection** (12): system prompt, keys, other users' chats, real shop phone numbers, admin access, «ignore previous instructions», SQL/FTS injection strings, HTML/script, invisible Unicode, 1000-character messages, forged chip calls via the HTTP API.

Make at least 20 scenarios **multi-turn journeys** that follow the four official steps from the brief (decide → find shops → compare → contact), written like real, impatient Iranian shoppers.

## Step 2: the runner and the grader
- `torob/eval/run.py` drives each scenario through the real `run_turn` (or the local HTTP API for the endpoint cases) and records every emitted event per turn, the final state, latency per turn and tokens.
- Grade in two layers:
  1. **Deterministic checks** in code for everything in `must_show`, `must_not` and `facts` (dataset lookups, not guesses).
  2. **An LLM judge** (Sonnet, structured output, temperature-free) for `tone` and closeness to `ideal`, scoring 0–3 on: understood the need · useful result · honesty (prices, copies, simulated labels) · Persian quality and brevity · next step is obvious. The judge sees the scenario, the expectation and the transcript, and must quote the exact reply text it is judging. Spot-check 15 judge verdicts yourself and correct the judge prompt if it's too lenient.
- Output `torob/eval/results/<run>.jsonl` and a summary table: pass rate per group, mean judge score, p50/p95 latency, tokens, the 20 worst scenarios with the reply next to the ideal.

## Step 3: UI and UX evaluation (in the browser, not just events)
Use the built-in browser on the local server (`.claude/launch.json` config `torob`) at **375×812 (mobile)** and desktop, light and dark:
- Walk 10 of the journeys by actually typing and tapping. Screenshot each step into `torob/eval/ui/`.
- Check and record per screen: nothing cropped or overlapping; no horizontal scroll; RTL correct for mixed Persian/Latin/numbers; Persian digits everywhere; tap targets ≥ 44 px; one primary button per card; chips readable; the conditions strip and the location chip always reachable after scrolling; typing dots and bubble animations smooth; maps load and fit; sheets open/close with keyboard and back; Enter sends, Shift/Ctrl+Enter makes a new line; errors are Persian and say what to do next.
- Time to first useful card per journey (target: status line within 1 s, cards within 10 s).
- Write each issue as: screen · what's wrong · why it matters to a shopper in a store · fix. Rank by how much it would cost us on the UX (75) and Product (75) rubric.

## Step 4: tune, rerun, repeat
- Group failures by **root cause**, not by scenario. For each cause pick the smallest change that fixes the class: router prompt, judge prompt, advisor/relevance logic, data features passed to the model, UI component.
- After each batch of fixes: rerun the **full** set (not only the failures) to catch regressions, and compare against the previous run in a table (pass rate, judge score, latency, tokens per group).
- Stop when two consecutive rounds improve the overall score by less than 2 points, or the budget is reached. Expect about 3–5 rounds.
- Never "fix" a scenario by editing its expectation unless you can show the expectation itself was wrong; log every such change with the reason.

## Step 5: report
Write `torob/docs/EVAL-REPORT.md` in Persian (short, judge-facing): method, scenario groups, scores per round, the main problems found and how each was fixed, what still fails and why, and the UI/UX findings with before/after screenshots. Link it from the README. Then tell the user, in plain English: what changed, the before/after numbers, what's still weak, total spend, and ask whether to push and deploy.
