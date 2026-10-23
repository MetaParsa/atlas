# Torob demo video — 5-minute shot list

Mandatory (no video = removed from judging) and quality is scored. Show the product working, not code. Speak Persian
over a phone screen recording; a silent recording scores lower but beats nothing. No editing needed: one clean take.

## Before you record (20 minutes)

1. **Phone:** open https://torob.frontage.store in the browser, light mode, full brightness, Do Not Disturb on.
   Start screen recording with the microphone on.
2. **Fresh start:** tap «جدید» so the chat is empty. If «جست‌وجوهای اخیرت» shows old test searches, tap «پاک کردن»
   so the start screen shows what other people searched instead.
3. **Dry run the call outcomes.** They're fixed per shop, product and day, so they'll be the same when you record:
   search «هندزفری بلوتوثی زیر ۳ میلیون» → «فروشنده‌ها» on the first earbud → call the shops one by one. Note which
   shop answers and confirms (use it at 2:20) and which one gets «جواب نداد» (use it at 3:00). Then tap «جدید».
4. **Photo:** have a real product in reach that the data knows: an Apple 20 W charger, Anker Soundcore earbuds or
   AirPods. Try the photo once in the dry run: the status line must say «تو عکس: …».
5. **PC tab** (for 4:10): open the same site and `/admin` in a desktop browser window.

## Timeline

| Time | On screen | Say (Persian, roughly) | Rubric |
|---|---|---|---|
| 0:00–0:20 | Start screen: the radish says hello, the question «چی لازم داری؟», popular searches under it | «تو بازار ایستادی و نمی‌دونی کدوم مدل رو بخری، قیمت ترب شاید سه هفته پیش ثبت شده، مغازه رو تو پاساژ پیدا نمی‌کنی و فقط قسطی می‌تونی بخری. این دستیار خرید حضوریه که روی دیتاست ترب کار می‌کنه.» | Product |
| 0:20–1:00 | Type «یه هندزفری بلوتوثی خوب برای باشگاه می‌خوام زیر ۳ میلیون». The radish thinks, then searches with its magnifier. Point at the status line «… مدل پیدا شد → … تا همونیه که خواستی → … تا نزدیکت → ۳ تا رو مقایسه کردم», then swipe the three cards | «هوش مصنوعی (Claude Sonnet) پیام رو فهمید، بودجه رو خودش گذاشت. از صدها نتیجه، فقط همونایی که واقعاً هندزفری‌ان موندن، نه قاب و کابل. سه تا کنار هم، با برچسب و دلیل.» | Step 1, Intelligence, UX |
| 1:00–1:40 | Tap «فروشنده‌ها» on the best-value card. The radish opens its map. Show the three shop cards: price + «۲ هفته پیش», «موجودی: تأیید نشده», «۶ دقیقه پیاده، باز تا ۲۰:۰۰», floor and unit in the passage, the «فقط دسته‌ی مرتبط» line | «قیمت ثبت‌شده، تاریخش و موجودی هیچ‌وقت قاطی نمی‌شن. طبقه و واحد رو از آدرس درآوردیم. می‌دونه وقتی برسی بازه یا نه.» | Steps 2–3, Dataset |
| 1:40–2:20 | Tap the strip: «پرداخت» → «قسطی». Cards re-render in place. Then «محدوده» → change it once | «بدون چت، با یک ضربه شرایط عوض می‌شه و نتیجه همون‌جا به‌روز می‌شه. اگر قسطی نزدیک نباشه، جایگزین پیشنهاد می‌ده.» | Pain 5, UX |
| 2:20–3:00 | Back to «همه». «زنگ بزن بپرس» on the shop that confirms (from the dry run). The radish picks up the phone; the call reads line by line; the amber «شبیه‌سازی» result; the «برنامه‌ی خرید» card appears | «فقط چیزهایی رو می‌پرسه که داده نداره: قیمت امروز، موجودی، کجای پاساژ. چون شماره‌های دیتاست ساختگی‌ان، تماس شبیه‌سازیه و همیشه همین رو با برچسب نشون می‌ده. نسخه‌ی واقعی همین قرارداد با یک تماس صوتی هوشمنده.» | Step 4, Pains 2 + 4, Intelligence |
| 3:00–3:30 | Call the shop that doesn't answer. «جواب نداد» → an alternative card appears with its reason → tap its chip | «جواب نداد؟ فوراً یه جایگزین با دلیل.» | Pain 3, Intelligence |
| 3:30–3:50 | Type «ساعت نه و نیم شب میام». The strip shows «رسیدن ۲۱:۳۰»; the status line says «… تا موقع رسیدن بسته‌ان»; the shopping plan at the top says «تا برسی بسته‌ست» and suggests another shop; shops without registered hours say «ساعت کاری ثبت نشده» | «حواسش به ساعت رسیدن هست: مغازه‌هایی که تا اون موقع بسته‌ان کنار رفتن، و ساعتی که ثبت نشده رو "نامعلوم" نشون می‌ده، نه "باز".» | Visit conditions, Dataset |
| 3:50–4:10 | Tap «جدید», then the camera button, and photograph the product you have. Status «تو عکس: …» → the exact model and its sellers | «اسمش رو نمی‌دونی؟ عکسش رو بگیر.» | Product, Intelligence |
| 4:10–4:35 | Switch to the PC: same site, wide layout with the shopping plan pinned beside the chat. Then `/admin`: «مدل: claude-sonnet-5-5», tokens used, response times, call results | «روی کامپیوتر هم کار می‌کنه. پشت صحنه: مدل، مصرف توکن با سقف روزانه، زمان پاسخ و آمار دیتاست.» | Deploy, Tech |
| 4:35–5:00 | Back to the phone, reload the page: the whole chat comes back | «روی سرور در ایران با داکر و HTTPS اجرا می‌شه و گفت‌وگو با بارگذاری دوباره حفظ می‌شه. قیمت‌ها، ساعت‌ها و آدرس‌ها همونیه که فروشگاه‌ها تو ترب ثبت کردن؛ اسم‌ها و شماره‌ها ساختگی‌اند و تماس‌ها شبیه‌سازی‌ان. ممنون.» | Deploy, honesty |

## Tips

- **Pace:** let each answer finish before talking over it; a search with the AI takes about 5 seconds. If you run
  over, cut the 1:40 strip beat to one change.
- **If something goes wrong** (an error, an odd result): keep going and redo only that beat at the end; one honest
  retry looks better than a cut.
- **Upload:** Aparat or YouTube, public or unlisted (anyone with the link must be able to watch). Put the link at the
  top of `torob/README.md` and in the submission form.
