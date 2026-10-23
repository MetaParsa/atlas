# Expectation changes (each with the reason it was wrong, not the reply it was changed for)

| Scenario | Was | Now | Why the expectation itself was wrong |
|---|---|---|---|
| budget-02 «کابل شارژ ۵۰۰ تومن» | must show products | products **or** one question | A cable is useless without the connector; a careful seller asks «تایپ‌سی یا آیفون؟» first. The budget check stays. |
| loc-03, loc-13, loc-15 (first turn) | must show products | products **or** one question | «شارژر گوشی» / «قاب گوشی» without the phone: asking «برای چه گوشی‌ای؟» first is what a good seller does. The location checks stay. |
| copy-09 «گلکسی بادز ۲ پرو» | must show products | products **or** an honest message | The dataset has only spare parts of this model (one earbud, the case); «چیزی پیدا نکردم» is the honest answer. |
| loc-07, loc-14, journey-20 (input) | «شارژر گوشی» / «کابل شارژ» | «شارژر دیواری سامسونگ ۲۵ وات» / «کابل شارژ تایپ سی» | These test opening hours; a deliberately vague first item made the app (rightly) ask a question, so the time step was never reached. |
| follow-11, journey-17 (input) | `chip: شیائومی` / `chip: اپل` | typed «شیائومی باشه» / «اپل باشه» | The brand chips exist only in model-off mode; with the model the question is about use, so the chip could not be tapped. |
| journey-02 (input) | `chip: زنگ بزن بپرس` | `card1: زنگ بزن بپرس` | Authoring bug: that is a card button, not a chip. |
| journey-07 (input) | `chip: فرقی نداره` | typed «فرقی نداره» | Same as follow-11: the chip depends on which question the model asks. |
