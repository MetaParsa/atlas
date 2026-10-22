# End-to-end test scenario — one shopper, every feature

Written 2026-10-21 against `docs/torob-build-spec.md` v2. Use it three ways: as the **acceptance test** before submission, as the **demo-video script skeleton** (§12 of the spec), and as a checklist that every brief/video requirement is reachable from the one UI.

Shop names, prices and counts below are **illustrative** (shop names in the dataset are fake anyway); the mechanics are what's being tested. Times are Tehran time.

Each scene: **User sees** → **Backend** (what runs) → **Why** (which requirement it serves). Hidden features the shopper never notices are marked *(invisible)*.

---

## Scene 0 — Opening the app (Thursday 18:05, Jomhouri St.)

**User sees:** opens `https://torob.frontage.store` on a phone. One screen: title «چی لازم داری؟», a text box, a location chip already set to «جمهوری / فردوسی», and three starter chips («هندزفری بلوتوثی تا ۳ میلیون» · «شارژر اصل آیفون» · «سامسونگ S25 FE قسطی»). No location prompt pops up. They tap the location chip → «مکان من», allow GPS, and the chip stays «فردوسی».

**Backend:**
- `GET /` serves the static React build; `GET /health` reports `ok` and dataset `v16`. `torob.db` was built locally and uploaded to the disk; the dataset DB is read-only.
- A session row is created; on «مکان من» the browser sends GPS (35.6932, 51.4168).
- Nearest dataset area by distance to each `areas` centre → «فردوسی» (3,282 shops). No LLM call.
- If most shops were closed right now, a line would offer «برای فردا ۱۰ صبح نشون بدم؟».
- Default `Conditions`: centre = GPS, radius 3 km, walk, open = «وقتی برسم», payment = any.

**Why:** step 2 needs the user's location; the brief wants Persian, RTL, mobile. Picking the nearest *dataset* area also prepares the honest "outside coverage" message used in Scene 10. *(invisible: rate-limit bucket opened for this IP; session token cap set.)*

---

## Scene 1 — "I don't know which model" (step 1, pain point 1)

**User types:** «یه هندزفری بلوتوثی می‌خوام برای باشگاه، تا ۳ میلیون» (typed with an Arabic «ي» by their keyboard).

**User sees:** a status line «دارم مدل‌ها رو مقایسه می‌کنم…», then one question: «مهمه که ضد عرق باشه؟» with chips «آره» · «فرقی نداره». They tap «فرقی نداره».

**Backend:**
- `POST /api/chat` opens an SSE stream.
- Orchestrator LLM (gpt-4.1-mini) calls `understand_need` → small model (gpt-4o-mini, JSON) returns `Need{product_type: هندزفری بلوتوثی, keywords: [هندزفری بلوتوثی, ایرباد, earbuds, TWS], category_hints: [هدفون، هدست و هندزفری], budget_max: 3000000, use_case: باشگاه, missing_info: [sweat_resistance?]}`.
- One missing nice-to-know → one `question` event with chips. Rule: max one question per turn.
- The conditions strip appears with «تا ۳ م» already filled: the agent's understanding is visible and editable.

**Why:** video step 1 + pain point 1. *(invisible: `core/text.py` normalises ي→ی, digits, ZWNJ before search, so keyboard differences don't break matching; each LLM call is logged with tokens and cost in `llm_calls`.)*

---

## Scene 2 — Three products side by side (step 1 result)

**User sees:** three cards side by side (swipe sideways on a narrow phone):

| Card | Tag | Shows |
|---|---|---|
| Anker Soundcore R50i | بهترین ارزش | registered price range nearby, «۳۹ فروشنده · ۲۱ تا نزدیکت», 2–3 spec lines, «چرا: زیر بودجه و بیشترین فروشنده نزدیک تو» |
| a cheaper earbud | ارزان‌ترین | same rows; specs line «مشخصات ثبت نشده» if the product has none |
| Anker R50i NC | بیشترین فروشنده / کمی بالاتر از بودجه | price range shows it's ~10% over |

One button per card: «فروشنده‌ها».

**Backend:**
- `pick_products`: FTS5 query on normalised keywords, restricted to the category the LLM hinted; products with no sane price dropped.
- **Price sanity** (built at ETL): the R50i has one offer at 26 M toman, > 3× its median → flagged `suspicious_high` and excluded from the range. *(invisible)*
- `sellers_in_area` = offers joined to shops within 3 km (haversine in SQL).
- Roles are chosen in code (best value / cheapest / most available). The LLM only writes the one-line «چرا» from the card's own fields, and is told not to invent specs.

**Why:** step 1 "compare products and prices"; Dataset criterion (no invented data, junk prices hidden); UX "why this was suggested".

---

## Scene 3 — Shops that have it, compared (steps 2 + 3)

**User taps:** «فروشنده‌ها» on the R50i.

**User sees:** «۱۲ فروشگاه نزدیک این مدل رو ثبت کردن» and three shop cards with **identical rows**:

| Row | Shop A | Shop B | Shop C |
|---|---|---|---|
| Where | پاساژ علاءالدین · طبقه ۲ · واحد ۲۱۴ | پاساژ چارسو · همکف | خیابان جمهوری، پلاک ۳۱۲ |
| Price | قیمت ثبت‌شده: ۱٬۴۹۰٬۰۰۰ · ۳ هفته پیش | ۱٬۵۲۰٬۰۰۰ · ۲ روز پیش | ۱٬۴۵۰٬۰۰۰ · ۴ ماه پیش «احتمالاً تغییر کرده» |
| Stock | موجودی: تأیید نشده | موجودی: تأیید نشده | موجودی: تأیید نشده |
| Visit | ۶ دقیقه پیاده · باز تا ۲۰:۰۰ | ۹ دقیقه · باز تا ۲۱:۰۰ | ۴ دقیقه · ساعت کاری ثبت نشده |
| Pay / warranty | اسنپ‌پی (نوع نامشخص) · ۱۸ ماه شرکتی | — · گارانتی ثبت نشده | — · تست و تحویل |
| Badges | همین پاساژ +۲ | قیمت تازه‌تر · دیرتر باز | نزدیک‌ترین · ارزان‌ترین |

Below: «+۹ فروشگاه دیگه» and «۶ فروشگاه فقط دسته‌ی مرتبط دارن — موجودی نامعلوم». Each card has one primary button: «زنگ بزن بپرس».

**Backend:**
- The chip maps 1:1 to `find_shops`, so **no LLM call** for the search itself (faster, cheaper, deterministic). A template writes the one-line message.
- Offers for the product, **grouped per shop** (variants become a price range), joined to shops, filtered by conditions.
- Visit times: Neshan pedestrian route for shops ≤ 2 km, car matrix beyond, cached 30 min. Without a Neshan key, straight line × 4.5 km/h labelled «تخمینی».
- Opening hours parsed from `weekly_hours` (Thursday shifts; `00:00–00:00` padding ignored); arrival = now + walk; `open_at_arrival` computed. Shop C has `{}` hours → «ثبت نشده», never guessed.
- Score = price 0.40 · travel 0.25 · freshness 0.15 · open slack 0.10 · completeness 0.10. Badges come from the same numbers.
- Shop C is cheapest, but its price is 4 months old, so freshness and completeness pull it to third and the card says why.
- «همین پاساژ +۲» = other sellers of this product with the same normalised passage name (علاالدین / علاءالدین / علائدین merged at ETL).
- Category-only shops = same category within radius, **no offer** for this product → never shown as having it.
- Passage/floor/unit come from the address text parser (3,140 of 3,179 passage shops have them).

**Why:** video steps 2 + 3 (compare price, distance, hours); pain point 4 (where in the passage); brief transparency rules (registered price ≠ price age ≠ confirmed stock; unknowns shown); category-only rule; Intelligence "visit conditions".

---

## Scene 4 — Changing conditions without chatting (UX, pain point 5)

**User taps:** the «پرداخت» chip → bottom sheet → «فقط قسطی».

**User sees:** the shop block re-renders in place. A line: «پرداخت: قسطی · ۱ فروشگاه». Only Shop A remains, labelled «اسنپ‌پی (نوع نامشخص)». Under it: «با ۱۰ کیلومتر ۲ فروشگاه قسطی دیگه هست — بگردم؟» plus a chip «فقط حضوری تأییدشده».

They tap «فقط حضوری تأییدشده» → 0 nearby → «نزدیک‌ترین فروشنده‌ی قسطی حضوری برای این مدل ۱۴ دقیقه با ماشین؛ یا R50i NC همین‌جا با ترب‌پی حضوری». They switch back to «همه».

**Backend:**
- `POST /api/refine` with the new `Conditions`: **no LLM**, same `find_shops` with a filter on `shop_payments` (BNPL providers; channel `in_person` vs `unknown`).
- Fewer than 3 results → the alternatives chain (§6.5) relaxes **one** condition (radius) and then tries a similar product, and says which it relaxed.
- Only 266 shops (4%) declare any credit service and 82 confirm in-person, so this fallback runs often.

**Why:** brief "change area, budget, terms easily and see the result"; video pain point 5 (instalment-only filter); Intelligence "alternatives". *(invisible: the strip is the same object the agent edits from chat, so typed and tapped changes never disagree.)*

---

## Scene 5 — Asking in plain words over existing results

**User types:** «کدومش زودتر می‌بنده؟»

**User sees:** «فروشگاه A ساعت ۲۰:۰۰ می‌بنده؛ B تا ۲۱:۰۰ بازه. C ساعتش ثبت نشده — بپرسم؟» with a chip «بپرس».

**Backend:** the orchestrator answers from the **current block's data** in session state (no new search) and quotes numbers from tool output only (rule 2). It sees C's unknown hours and offers a call instead of guessing.

**Why:** chat and cards work together; the agent pays attention to visit conditions.

---

## Scene 6 — The call (step 4, pain points 2 + 4)

**User taps:** «زنگ بزن بپرس» on Shop A.

**User sees:** a call block inside the chat: «تماس با فروشگاه A · شبیه‌سازی», ringing for ~2 s, then lines appear one by one:

> **دستیار:** سلام، وقت بخیر. هندزفری انکر R50i رو دارید؟
> **فروشنده:** سلام، آره داریم.
> **دستیار:** قیمت امروزش چنده؟ قسطی هم دارید؟
> **فروشنده:** یک و پونصد و ده. اسنپ‌پی داریم.
> **دستیار:** دقیقاً کجای پاساژید؟
> **فروشنده:** طبقه دوم، واحد ۲۱۴.

(Only the floor and unit from the dataset address. The simulator never invents landmarks.) The card above gets the amber dashed «شبیه‌سازی» style: «تأیید فروشنده (شبیه‌سازی) ۱۸:۰۹ · ۱٬۵۱۰٬۰۰۰ (۱٪ بیشتر از قیمت ثبت‌شده) · اسنپ‌پی حضوری». The primary button becomes «مسیر · ۶ دقیقه». A chip: «تا ۱۹:۳۰ نگهش دارن؟».

**Backend:**
1. `POST /api/call`. `caller.py` builds the questions **in code** from what is unknown or old: stock (always); today's price (price is 3 weeks old); credit (the user touched the payment filter); location (shop is in a passage).
2. `truth.py` makes a **deterministic hidden truth** from hash(shop, product, date):
   - in stock (85% for prices ≤ 7 days, lower for older ones);
   - price = registered × age drift, rounded to 10,000;
   - credit = only providers the dataset lists for this shop, never invented;
   - in-passage hint from the parsed floor/unit.
3. By default (`CALL_LLM=0`) a template shopkeeper speaks that truth, picking from several bazaar-Persian phrasings. For demo takes (`CALL_LLM=1`), gpt-4o-mini (≤ 200 tokens) phrases it instead, with the template as fallback.
4. A rule-based parser (or gpt-4o-mini JSON when `CALL_LLM=1`) reads the transcript into a `CallResult`. Code checks the price against the registered one; a > 40% gap would trigger a confirm line.
5. `card_update` switches the card to the simulated-confirmed style. `call_logs` and the parser confidence go to `/admin`. «مسیر» opens a `nshn.ir` walking link.

Before ringing, the block shows why: «می‌پرسم چون: قیمت ۳ هفته پیش ثبت شده · داخل پاساژه · فیلتر قسطی روشنه». After the call, the **«برنامه‌ی خرید»** card pins at the top. It shows:
- the shop and «پاساژ علاءالدین · طبقه ۲ · واحد ۲۱۴»;
- the dataset price with its date, plus the simulated price and hold in simulated style;
- «باز تا ۲۰:۰۰ · می‌رسی ۱۸:۱۵»;
- one backup shop;
- one button «مسیر», and a chip «قبل از رفتن دوباره بپرس».

**Why:** video step 4 and pain point 2 (stale price → contact the shop, "like Let Google Call"); pain point 4 (exact spot in the passage); Intelligence "seller follow-up". Honesty: dataset phones are fake, so every result says «شبیه‌سازی» and the README explains the real-world path (voice AI calls). *(invisible: the same seed always gives the same truth, so demo takes repeat and the parser is scored against the truth in tests.)*

---

## Scene 7 — No answer, then out of stock (pain point 3)

Before leaving, the user wants a backup and taps «زنگ بزن بپرس» on Shop B.

**User sees:**
- Shop B call: ringing… «جواب نداد».
- Immediately an alternative card with its reason: «چون B جواب نداد → فروشگاه D همین مدل رو ثبت کرده، ۸ دقیقه، باز تا ۲۰:۳۰», and a chip «به D زنگ بزنم؟».
- They tap it. D's call: «R50i تموم شده، فقط مدل NC داریم». Card D shows «ناموجود (شبیه‌سازی)».
- Next alternative card: «چون D نداشت → مدل NC تو همین پاساژ ثبت شده، ۲٬۴۹۰٬۰۰۰ — ۵۰۰ هزار بالاتر از بودجه».

**Backend:**
- B is forced to `no_answer` by `SIM_OVERRIDES`, so no LLM call, just the no-answer event.
- `find_alternatives(reason=no_answer)` → same product, next-best shop under the same conditions → card + chip. **No automatic second call** (R9).
- D is forced to `unavailable` → `find_alternatives(reason=unavailable)` → similar product (same brand and category) in the same passage, priced within ±30%.
- The over-budget amount is computed in code, not by the LLM.

**Why:** video pain point 3 (seller doesn't answer → quick alternatives); Intelligence "alternative when out of stock". The agent always shows *why* it switched. The video shows only the B → D beat; the D → NC path is covered by tests.

---

## Scene 8 — Arriving later (visit conditions)

**User types:** «الان نمی‌رسم، ساعت هشت و نیم میام».

**User sees:** the strip shows «رسیدن: ۲۰:۳۰». Shop A now reads «تا برسی بسته‌ست (۲۰:۰۰)» and drops below Shop B, which is open till 21:00. The confirmed «نگه‌داری تا ۱۹:۳۰» is marked as no longer enough, with a chip «از B بپرسم تا ۲۱ نگه داره؟».

The plan card switches its main shop to B, with the reason «چون A تا برسی بسته‌ست», and keeps A as the backup for tomorrow.

**Backend:** `set_conditions(arrive_at=20:30)` → re-rank. `open_at_arrival` = `no` for A (−1 penalty) and `yes` for B. Closed-before-arrival triggers `find_alternatives(reason=closed)` automatically. No new search, so no LLM except the short message.

**Why:** Intelligence "attention to visit conditions (time, location, need)".

---

## Scene 9 — Late night / Friday

On Friday at 10:00 the user reopens the session and taps «باز بودن → الان».

**User sees:** «جمعه بیشتر فروشگاه‌های این منطقه بسته‌ان. ۲ فروشنده‌ی این مدل امروز بازن؛ بقیه شنبه ۱۰ باز می‌کنن.» Cards show «شنبه ۱۰:۰۰» as next open.

**Backend:** hours parser: 3,473 shops are closed on Friday; `next_open` is computed per shop. *(The optional holiday calendar would mark official holidays the same way.)*

**Why:** visit conditions; no dead end.

---

## Scene 10 — Outside coverage

The user's friend opens the link in Tehranpars.

**User sees:** «داده‌ی ترب این‌جا فقط ۴ فروشگاه داره. فروشگاه‌های جمهوری/فردوسی رو نشون بدم یا کل تهران؟» with two chips.

**Backend:** the nearest area has < 20 shops with offers → a coverage message instead of an empty list. The dataset is ~10% of Torob's Tehran shops from selected districts, and the app says so rather than pretending.

**Why:** brief "show unknown information clearly"; honest empty states (UX).

---

## Scene 11 — The "might have it" shops

Back in Scene 3, the user expands «۶ فروشگاه فقط دسته‌ی مرتبط دارن».

**User sees:** a short list, each «این مدل رو ثبت نکرده — موجودی نامعلوم» with «زنگ بزن بپرس». A call asks «انکر R50i دارید؟» first.

**Backend:** shops whose `category_names` match, with no offer row for the product (41% of all shops have no offers at all). The truth model gives them a lower in-stock chance.

**Why:** brief rule "an activity category is not proof of stock", shown transparently rather than hidden.

---

## Scene 12 — Things the agent refuses or protects *(mostly invisible)*

- «برام چونه بزن» → «چونه نمی‌زنم، ولی قیمت امروزشون رو پرسیدم: ۱٬۵۱۰٬۰۰۰». The agent never bargains, buys or promises.
- A shopkeeper line containing instructions (tested in evals) is parsed as data only; no tool runs from it.
- Rate limits (20 requests/min/IP, 10 calls/session/hour), per-session token cap, and a daily $ cap protect the $3 BoxAPI credit.
- Network drop → «اینترنت قطع شد؛ دوباره امتحان کن» with retry; the session state survives.

---

## Scene 13 — Behind the scenes (for judges)

A small «پشت صحنه» link in the footer opens `/admin` on the same site:
- dataset stats and build time;
- LLM calls and $ spent per role, average tool calls per turn;
- simulated calls (answered %, stock found %, parser confidence);
- suspicious prices hidden, rate-limit hits, last errors.

**Why:** Tech + Deploy criteria (observability, cost control), and the video's "under the hood" shot.

---

## Coverage check

| Requirement | Scene |
|---|---|
| Video step 1 — choose model, compare products | 1, 2 |
| Video step 2 — nearby shops by location | 0, 3 |
| Video step 3 — compare shops: price, distance, hours | 3, 5, 8 |
| Video step 4 — contact shops for price/stock/hours | 6, 7, 11 |
| Pain 1 — doesn't know the model | 1, 2 |
| Pain 2 — stale price | 3 (age labels), 6 |
| Pain 3 — no answer / closed → alternatives | 7, 8, 9 |
| Pain 4 — find the shop inside the passage | 3, 6 |
| Pain 5 — instalment-only filter | 4 |
| Brief UX — not everything via chat; change area/budget/terms | 3, 4 |
| Brief UX — why suggested + next step | 2, 3 |
| Brief transparency — price ≠ age ≠ confirmed stock; unknowns | 3, 6, 10, 11 |
| Brief — category-only ≠ has product | 3, 11 |
| Intelligence — follow-up, alternatives, visit conditions | 6, 7, 8, 9 |
| Deploy / Tech | 0, 12, 13 |
| Dataset — correct use, junk hidden | 2, 3 |

## Test settings
`DEMO_NOW` fixes the clock (Thu 18:05, then Thu 20:30, Fri 10:00) and `SIM_OVERRIDES` forces Scene 7 (shop B `no_answer`, shop D `unavailable`), so every state is reproducible on any day; `SIM_SEED_DATE` fixed so the same calls give the same answers; `CALL_LLM=0` for routine runs, `CALL_LLM=1` once; one run with Neshan, one without (labels «تخمینی»); one run on FakeLLM (CI) and one on the real provider; phone at 375 px width.
