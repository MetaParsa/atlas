# the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ dataset — what's actually in it

Profiled 2026-10-21 with DuckDB. Source: `starcoach-dataset-v16-cap300-csv.zip` (60.2 MB) from the the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ challenge page → unzipped to `the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣/data/v16/` (285 MB). Export built **2026-10-14** (file dates); newest price change 2026-10-13.

**Don't commit the CSVs to GitHub**: `products.csv` is 196 MB, over GitHub's 100 MB file limit. `the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣/data/.gitignore` keeps them out. The app loads them at build/deploy time into its own database.

---

## 1. Headline findings

1. **Covered area: three neighbourhoods around Jomhouri / Ferdowsi.** 85% of shops (5,485 / 6,476) are labelled فردوسی, شیخ هادی or بهارستان: the Aladdin / Charsou / Boushehri passage cluster. Everywhere else in Tehran has 0–151 shops. → **Demo area = Jomhouri/Ferdowsi.** Our original demo location (Charsou + Aladdin) fits.
2. **The catalogue is electronics and electrical goods.** Top categories: building cable, CCTV, generators, phone LCDs, lighting, pumps, watches, breakers, headphones, phone cases, phones. Fashion is thin: no shoe has more than 2 sellers. **The video's "sneakers" example won't demo well.** Use phone accessories, headphones, speakers, phones or alarms.
3. **84% of products have only one seller** (190,649 of 225,903; every product has ≥ 1 offer — an earlier draft divided by an approximate distinct count and said 88%). Comparing *the same product across shops* works for popular items: Apple 20 W charger 81 sellers, Samsung 25 W charger 55, Anker R50i headphones 39, Samsung S25 FE 34. For everything else, comparison has to be between *similar products* (same category/brand/specs).
4. **Prices are not "last week's prices".** The export is from last week, but each price's own change date varies widely:

   | Price last changed (vs Oct 14) | Offers |
   |---|---|
   | 0–7 days | 63,112 (19%) |
   | 8–30 days | 122,985 (37%) |
   | 31–90 days | 69,433 (21%) |
   | 91–365 days | 69,949 (21%) |
   | > 1 year (oldest Dec 2020) | 5,258 (2%) |
   | missing date | 769 |

   → Every price shows its own age ("ثبت‌شده · ۳ هفته پیش"). This is exactly the transparency the Dataset criterion checks.
5. **41% of shops are category-only** (2,684 shops have no offers at all). These are the "activity category is not proof of stock" shops the brief warns about. They can be shown as "might carry this kind of thing", never as "has it".
6. **Same product, same passage: 7,594 (product, passage) pairs have 2+ sellers.** "Two more shops in this passage sell the same thing" is a real, cheap feature.
7a. **Found while building the DB (2026-10-22):**
   - **849 products have the placeholder title «عنوان حذف‌شده برای انتشار»** (title removed for publication). They are marked `redacted` and never searchable or shown.
   - **4,448 titles are shared by 11,221 product IDs.** the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ keeps them as separate base products, e.g. «شارژر اپل توان 20 وات سه پین» = 3 IDs: 59 sellers (median 3.6 M), 29 sellers (median 5.4 M, likely genuine vs. non-genuine) and 1 seller. The "81 sellers" figure in §6 counted by title. The advisor must dedupe or explain same-title picks.
   - After fixing the parser: 4,159 of 4,224 addresses with «طبقه» have a parsed floor. 669 same-building groups (same spelling key + same area + within 200 m). Only 76 offers are explicitly priced per metre.
   - **58 shops declare Friday as a one-second «shift» 00:00:00–00:00:01.** It must not be read as open around the clock; shifts under 5 minutes are dropped, and the day becomes «ساعت کاری ثبت نشده».
   - **Shop coordinates are approximate.** The main Aladdin group (243 shops) spreads about 450 m, and the passage straddles two neighbourhood labels (فردوسی / شیخ هادی). Same-building groups therefore use spelling key + within 350 m of the group's centre, ignoring the label.
   - Price flags: 4,949 offers (1.5%) flagged. Rules are in `starcoach-agents/torob/logic/price.py`.
7. **Several fields promised by the brief are not in the export** (README: "shop descriptions and catalogue coverage are omitted"). See §4.

---

## 2. Tables and columns

Format: UTF-8 CSV with header; nested values are JSON strings; missing = literal `null`. IDs are opaque strings (`shop_…`, `product_…`, `offer_…`). No duplicate IDs, no orphan offers; every product has at least one offer.

### shops.csv — 6,476 rows

| Column | Filled | Notes |
|---|---|---|
| `shop_id` | 100% | |
| `name` | 100% | **Fake for 6,364 shops.** Real only for the 112 with Instagram/website. |
| `address` | 100% | Free text. Mentions پاساژ in 3,179, طبقه 4,224, واحد 2,380, پلاک 5,091, مجتمع 467, مرکز خرید 40. Passage/floor/unit must be **parsed from this text**; there are no separate fields. Spelling varies (علاالدین / علاءالدین / علائدین / علاالدین ۲). |
| `location_label` | 100% | Neighbourhood name, 31 values (see §3). |
| `lat`, `lon` | 100% | Real. Bounding box 35.640–35.813 N, 51.305–51.509 E. |
| `category_names` | 100% | JSON list of activity categories (median 2). |
| `contacts` | 100% | Always `[{"channel":"phone","display_number":"…"}]`. **Phone only, all fake.** No WhatsApp/Bale in this export. |
| `weekly_hours` | 69% (2,003 = `{}`) | JSON `{sat..fri: {status, shifts:[{start_time,end_time}]}}`. Status: `declared` / `closed` / `unsupported` (rare). An extra `00:00–00:00` shift is padding: ignore it. Friday: 3,473 closed, 998 open. 26 shops are closed all week. |
| `payment_options` | 266 shops non-empty | JSON list of `{provider_name, channel, min_price_toman, max_price_toman, category_names}`. `channel` = `in_person` or `unknown` ← this is the brief's "type of information". See below. |
| `supports_fast_delivery` | 25% | true 1,168 · false 433 · null 4,875. |
| `instagram` | 22 shops | `{handle, profile_url}` |
| `website_url` | 91 shops | |

Payment providers (shops):

| Provider | in_person | unknown channel |
|---|---|---|
| بازارگاه⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣‌پی (the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ Pay) | 50 | 140 |
| اسنپ‌پی | 53 | 36 |
| دیجی پی | 45 | 16 |
| تارا | 26 | 11 |
| ازکی‌وام | — | 5 |
| 3 shop-specific schemes | — | 3 |

→ Filter label: "بازارگاه⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣‌پی حضوری" only for `in_person`; `unknown` shown as "بازارگاه⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣‌پی (نوع نامشخص)". Min/max price limits are almost always null/0.

### products.csv — 225,903 rows

| Column | Filled | Notes |
|---|---|---|
| `product_id` | 100% | |
| `persian_title` | 100% | Main search text. Persian/Latin digits mixed. |
| `category_name` | 100% | 3,420 categories. |
| `brand_name` | 25.5% | |
| `model` | 1.8% | Nearly empty. |
| `specs` | 100% (81,304 = empty list) | JSON list `{section, key, value}`; median 2 entries. Thin for most products. |
| `image_urls` | 100% (1,219 empty) | `image.torob.com` URLs, median 2. **Loads fine and allows cross-origin** (checked: 200, `access-control-allow-origin: *`). |

No English title, no product description (the brief lists both).

### offers.csv — 331,506 rows

| Column | Filled | Notes |
|---|---|---|
| `offer_id`, `shop_id`, `product_id` | 100% | |
| `seller_title` | 100% | The shop's own title for the product (often has size/colour variants). |
| `description` | 38,377 non-empty (12%) | Seller's description. |
| `price_toman` | 95.5% | **15,083 null** → "قیمت ثبت نشده". Median 3.64 M. **Junk exists:** max 7.3×10¹⁷; 1,051 offers > 5× their product's median and 611 < ⅕ (among products with ≥ 3 priced offers); 5,062 under 10,000 toman. |
| `last_price_changed_at` | 99.8% | ISO timestamp with timezone. See §1.4. |
| `warranty` | 17.8% | JSON `{provider, period_months, description}`. Top: "تست و تحویل در حضور مشتری" 3,974 · شرکتی 12 mo 2,294 · شرکتی 18 mo 2,207 · "گارانتی اصالت و سلامت فیزیکی کالا" 2,058 · بدون گارانتی 1,625. |

Per shop: median 27 offers, max 1,346 offers / 300 distinct products (492 shops ≥ 300 offers). **The same product can appear several times in one shop** (9,557 shop-product pairs, up to 105 offers: variants like size or colour). Group by (shop, product) and show a price range.

---

## 3. Coverage by area (`location_label`)

| Area | Shops | With offers | Offers | Centre (lat, lon) |
|---|---|---|---|---|
| فردوسی | 3,282 | 1,962 | 175,182 | 35.693, 51.420 |
| شیخ هادی | 1,587 | 936 | 83,647 | 35.695, 51.409 |
| بهارستان | 616 | 369 | 32,102 | 35.692, 51.426 |
| آسمان | 151 | 86 | 4,920 | 35.778, 51.354 |
| باغ فیض | 107 | 53 | 4,544 | 35.740, 51.316 |
| ایرانشهر | 99 | 59 | 6,792 | 35.711, 51.422 |
| سنائی | 96 | 54 | 4,653 | 35.719, 51.422 |
| سهروردی | 82 | 36 | 2,489 | 35.721, 51.430 |
| تجریش | 76 | 38 | 1,269 | 35.806, 51.430 |
| جنت آباد جنوبی | 68 | 39 | 2,379 | 35.738, 51.310 |
| امجدیه | 54 | 33 | 2,488 | 35.715, 51.428 |
| آذربایجان | 51 | 38 | 4,083 | 35.695, 51.404 |
| 19 more areas | 1–37 each | | | |

Top passages by shop count (from address text, spelling variants not merged): علاالدین/علاءالدین (~325 combined), بوشهری 149, چارسو 137, اتحادیه 97, امجد 77, کیان 72, فراز 60, یگانه 54, صدقی نژاد 51, حافظ 49, سبحان 49, نادری 48, ایرانیان 44.

Real-named shops (Instagram/website) are mostly in شیخ هادی (43), فردوسی (29), بهارستان (13).

---

## 4. Brief vs export — what's missing

| Brief promises | In the export? | What we do |
|---|---|---|
| Business type, intro description | ✗ (descriptions omitted) | Use `category_names` only. |
| Shop images | ✗ | Show product images instead. |
| the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ history, licences | ✗ | No trust badge from data; don't invent one. |
| Short address, location notes | Partly: `location_label` + free-text `address` | Parse passage/floor/unit with regex + normalisation. |
| WhatsApp / Bale contacts | ✗ (phone only, fake) | Simulated shop inbox for follow-up. Hide the phone, or show it clearly labelled. |
| English title, product description | ✗ | Search on Persian title + specs + seller titles. |
| Price unit | ✗ (column is `price_toman`) | Assume toman. Some seller titles say "قیمت متری" (per metre); show the seller title so this is visible. |
| Kalabarg (کالابرگ) support | ✗ (not anywhere, 0 matches) | Leave out of filters; ask organizers. |
| the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ Pay in-person + instalments with info type | ✓ `payment_options.channel` | Filter + honest label. |
| Express delivery | ✓ 25% filled | Filter; null = unknown. |
| Category-only vs has-product | ✓ implied: shops without offers | Two-tier results. |

---

## 5. Rules for the app (from the data)

1. **Search index:** normalise Persian (ی/ي, ک/ك, digits, ZWNJ, spelling of passage names), FTS over `persian_title` + `seller_title` + `brand_name` + `category_name` + spec values. LLM turns the user's need into queries and a category.
2. **Price sanity:** per product, flag offers > 3× or < ⅓ of the median of its priced offers (and absolute < 10,000 toman) as "قیمت مشکوک"; never rank them first, never use them in "cheapest". Null price → "قیمت ثبت نشده".
3. **Price age label on every price**, from `last_price_changed_at`: "امروز / N روز پیش / N هفته پیش / بیش از ۳ ماه پیش — احتمالاً تغییر کرده".
4. **Stock is never confirmed by the data.** Offer = "در بازارگاه⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ ثبت کرده". Only the follow-up step can produce "موجودی تأییدشده".
5. **Hours:** missing `{}` → "ساعت کاری ثبت نشده"; ignore `00:00–00:00` shifts; Friday mostly closed.
6. **Group offers per (shop, product)** and show a price range when there are variants.
7. **Areas:** use `location_label` + coordinates for the area chip; default to Ferdowsi/Jomhouri; outside the dense areas, say so and offer the nearest dense area.
8. **Load once** into SQLite (FTS5) or Postgres at deploy; the 285 MB of CSV becomes a single DB file on the the platform⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ disk.

---

## 6. Demo-friendly products (many sellers, real price spread)

| Product | Sellers | Price range (toman) |
|---|---|---|
| شارژر اپل ۲۰ وات سه پین | 81 | 480k – 29.8M (outliers!) |
| شارژر سامسونگ EP-TA800 ۲۵ وات | 55 | 350k – 8.95M |
| دزدگیر سایلکس SG8 LITE | 51 | 13.4M – 25.4M |
| هدفون بلوتوثی انکر Soundcore R50i | 39 | 1.39M – 26M |
| هندزفری انکر R50i NC | 38 | 2.19M – 6.59M |
| اسپیکر هارمن کاردن Aura Studio 5 | 37 | 53.9M – 680M |
| گوشی سامسونگ S25 FE 256/8 | 34 | 184M – 224M |

Good demo story: "I want a wireless earbud under 3 M near Jomhouri" → compare Anker R50i vs alternatives → sellers in Aladdin/Charsou with price + age + warranty → two in the same passage → follow-up confirms one → the other doesn't answer → the agent suggests the alternative.
