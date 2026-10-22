// Torob in-store shopping assistant — one screen, no framework (spec §9, §0.2 R1–R17).
// Every data string goes through textContent; nothing from the server is parsed as HTML.
// Honesty rule: simulated facts live only in amber dashed «simbox» rows; dataset rows are never restyled.
"use strict";

const $ = (s, root = document) => root.querySelector(s);
const stream = $("#stream"), strip = $("#strip"), planSlot = $("#plan-slot");
const sheet = $("#sheet"), sheetTitle = $("#sheet-title"), sheetBody = $("#sheet-body");
const FA = "۰۱۲۳۴۵۶۷۸۹";
const fa = (v) => String(v).replace(/\d/g, (d) => FA[d]);
const toLatin = (s) => String(s).replace(/[۰-۹]/g, (d) => FA.indexOf(d)).replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d)).replace(/[٫,]/g, ".");
const TIMEOUT_MS = 90_000;
let cond = null, busy = false, turn = null, hasResults = false;

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k)));
  return n;
}
const simTag = () => el("span", { class: "sim-tag", text: "شبیه‌سازی" });
const scrollEnd = () => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });

// --- talking to the server ---------------------------------------------------------------------------------------

function setBusy(on) {
  busy = on;
  document.body.classList.toggle("busy-all", on);
  $("#send").disabled = on;
}

async function post(url, body) {
  if (busy) return false;
  setBusy(true);
  turn = { status: null, sawDone: false, block: null };
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body), signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!res.ok || !res.body) {
      let msg = "یه مشکلی پیش اومد؛ دوباره امتحان کن.";
      try { msg = (await res.json()).error || msg; } catch (_) {}
      if (res.status === 422) msg = "پیام خیلی طولانیه یا این گزینه درست نیست.";
      showError(msg);
      return true;
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");   // a proxy may rewrite to CRLF
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        let ev = "message", data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) ev = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        try { handle(ev, JSON.parse(data || "{}")); } catch (e) { console.error(ev, e); }
      }
    }
    if (!turn.sawDone) showError("جواب کامل نرسید؛ دوباره امتحان کن.");
  } catch (e) {
    showError(e && e.name === "TimeoutError" ? "خیلی طول کشید؛ دوباره امتحان کن." : "اینترنت قطع شد؛ دوباره امتحان کن.");
  } finally {
    if (turn.status) turn.status.classList.remove("busy");
    setBusy(false);
    if (turn.block) turn.block.scrollIntoView({ behavior: "smooth", block: "start" });   // show the top-ranked card
    else scrollEnd();
  }
  return true;
}

function showError(text) { leaveStart(); stream.append(el("div", { class: "error", text })); }
function leaveStart() { $("#start")?.remove(); }

function say(text) {
  if (!text.trim() || busy) return false;
  leaveStart();
  stream.append(el("div", { class: "bubble user", text }));
  post("/api/chat", { text });
  return true;
}
// `bubble`: what the user "said" by tapping (null = say nothing, e.g. a silent location change before any search).
function chip(c, bubble = c.label) {
  if (busy) return;
  if (c.kind === "direct_call") {
    if (bubble) { leaveStart(); stream.append(el("div", { class: "bubble user", text: bubble })); }
    post("/api/chat", { chip: c });
  } else say(c.value || c.label);
}
function chipButton(c, cls = "chip", bubble) {
  return el("button", { type: "button", class: cls, text: c.label, onclick: () => chip(c, bubble ?? c.label) });
}
function refine(patch) { if (!busy) post("/api/refine", { conditions: patch }); }

// --- events --------------------------------------------------------------------------------------------------------

const handlers = {
  status(d) {
    if (!turn.status) { turn.status = el("div", { class: "status" }); stream.append(turn.status); }
    turn.status.textContent = d.text;
    turn.status.classList.toggle("busy", !d.done);
  },
  message(d) { if (d.text) stream.append(el("div", { class: "bubble agent", text: d.text })); },
  error(d) { stream.append(el("div", { class: "error", text: d.text })); },
  conditions(d) { setConditions(d); },
  question(d) {
    stream.append(el("div", { class: "question" }, el("div", { class: "bubble agent", text: d.text }),
      el("div", { class: "chips" }, (d.chips || []).map((c) => chipButton(c)))));
  },
  product_cards(d) { hasResults = true; strip.hidden = false; putBlock("blk-products", productBlock(d)); },
  shop_cards(d) { hasResults = true; strip.hidden = false; putBlock("blk-shops", shopBlock(d)); },
  call_start(d) {
    stream.append(el("div", { class: "call sim", id: `call-${d.call_id}` },
      el("div", { class: "head" }, d.title, simTag()),
      el("div", { class: "why", text: d.reasons }),
      d.real_shop_note ? el("div", { class: "note", text: d.real_shop_note }) : null));
  },
  call_line(d) {
    $(`#call-${d.call_id}`)?.append(el("div", { class: `line ${d.who}`, text: d.text }));
    scrollEnd();
  },
  call_result(d) {
    const box = $(`#call-${d.call_id}`);
    if (turn.status) { turn.status.remove(); turn.status = null; }   // «در حال تماس…» is over
    box?.append(el("div", { class: "result", text: d.label }));
  },
  card_update(d) { updateShopCard(d); },
  plan(d) { renderPlan(d); },
  alternative(d) {
    stream.append(el("div", { class: "card alt" },
      el("div", { class: d.simulated ? "simbox sim" : "" }, el("span", { text: d.text }), d.simulated ? simTag() : null),
      d.chip ? el("div", { class: "chips start-align" }, chipButton(d.chip)) : null));
  },
  done() { turn.sawDone = true; if (turn.status) turn.status.classList.remove("busy"); },
};
const QUIET = new Set(["conditions", "status", "done"]);
function handle(ev, d) {
  if (!QUIET.has(ev)) leaveStart();          // a silent change (e.g. location before searching) keeps the start screen
  (handlers[ev] || (() => {}))(d);
}

function putBlock(id, node) {
  node.id = id;
  node.classList.add("block");
  const old = document.getElementById(id);
  if (old) old.removeAttribute("id");
  stream.append(node);                        // newest block at the bottom; the old one stays as history
  turn.block = node;
}

// --- products (step 1) -----------------------------------------------------------------------------------------------

function money(n) { return fa(Number(n).toLocaleString("en")).replace(/,/g, "٬"); }
function priceRange(min, max) {
  if (!min) return "قیمت ثبت نشده";
  return min === max ? money(min) : `${money(min)} تا ${money(max)}`;
}

function productBlock(d) {
  const items = d.items || [];
  const block = el("section");
  (d.notes || []).forEach((n) => block.append(el("div", { class: "note", text: n })));
  if (!items.length) return block;
  const row = el("div", { class: "row" });
  for (const p of items) {
    const img = p.image ? el("img", { class: "img", src: p.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer",
      onerror: (e) => e.target.replaceWith(el("div", { class: "img" })) }) : el("div", { class: "img" });
    row.append(el("article", { class: "card pcard" }, img,
      el("div", {}, el("span", { class: "tag", text: p.role_label })),
      el("div", { class: "title", text: p.title }),
      el("div", { class: "meta" }, `قیمت ثبت‌شده ${p.price_scope === "area" ? "نزدیکت" : "در تهران"}: `,
        el("bdi", { text: priceRange(p.price_min, p.price_max) })),
      p.spec_lines?.length ? el("ul", { class: "specs" }, p.spec_lines.map((s) => el("li", { text: s }))) : null,
      el("div", { class: "why", text: "چرا: " + p.why }),
      p.button ? chipButton(p.button, "primary", `فروشنده‌های «${p.title.split("|")[0].trim()}»`) : null));
  }
  block.append(row);
  if (d.compare_rows?.length) {
    const t = el("table", { class: "compare" },
      el("tr", {}, el("th", { text: "" }), items.map((p) => el("th", { text: p.role_label }))),
      d.compare_rows.map((r) => el("tr", {}, el("td", { text: r.key }), r.values.map((v) => el("td", { text: v })))));
    block.append(el("details", {}, el("summary", { text: "مقایسه‌ی مشخصات" }), t));
  }
  return block;
}

// --- shops (steps 2–3) --------------------------------------------------------------------------------------------

function shopCard(s, compact = false) {
  const card = el("article", { class: "card scard", "data-shop-id": s.shop_id },
    el("div", { class: "name", text: s.name }),
    el("div", { class: "lines" },
      el("div", { class: "where", text: s.where }),
      el("div", { class: "price" }, el("bdi", { text: s.price.text }), "، ", el("bdi", { class: "age", text: s.price.age }),
        s.price.note ? el("span", { class: "note" }, "، ", el("bdi", { text: s.price.note })) : null),
      el("div", { class: "stock", text: s.stock_label }),
      el("div", { class: "visit", text: s.visit.text })));
  if (!compact && s.reasons?.length) card.append(el("div", { class: "badges" }, s.reasons.map((r) => el("span", { class: "tag", text: r }))));
  if (!compact) {
    const det = s.details;
    card.append(el("details", {}, el("summary", { text: "جزئیات" }),
      el("div", { class: "meta", text: det.payments.join("، ") }),
      el("div", { class: "meta", text: det.warranty }),
      el("div", { class: "meta", text: det.fast_delivery }),
      el("div", { class: "meta", text: "عنوان فروشنده: " + det.seller_title })));
  }
  card.append(chipButton(s.button, compact ? "chip" : "primary", `زنگ به «${s.name}»`));
  return card;
}

function shopBlock(d) {
  const block = el("section");
  if (d.product) block.append(el("h3", { text: `فروشنده‌های «${d.product.title}»` }));
  (d.notes || []).forEach((n) => block.append(el("div", { class: "note", text: n })));
  if (d.relaxed?.chip) block.append(el("div", { class: "chips start-align" }, chipButton(d.relaxed.chip)));
  (d.items || []).forEach((s) => block.append(shopCard(s)));
  if (d.junk_line) block.append(el("div", { class: "junk", text: d.junk_line }));
  if (d.more?.length) {
    block.append(el("details", {}, el("summary", { text: `${fa(d.more_count)} فروشگاه دیگه هم دارن` }),
      d.more.map((s) => shopCard(s, true))));
  }
  if (d.category_only?.length) {
    block.append(el("details", {}, el("summary", { text: `${fa(d.category_only.length)} فروشگاه فقط دسته‌ی مرتبط دارن — موجودی نامعلوم` }),
      d.category_only.map((s) => el("div", { class: "card" }, el("div", { class: "name", text: s.name }),
        el("div", { class: "meta", text: s.where }), el("div", { class: "meta", text: s.stock_label }),
        el("div", { class: "meta", text: `${fa(s.minutes)} دقیقه (تخمینی)` })))));
  }
  return block;
}

// The call result goes into its own simulated row; the dataset rows (where, price, stock label) are untouched.
function updateShopCard(d) {
  const cards = document.querySelectorAll(`[data-shop-id="${CSS.escape(d.shop_id)}"]`);
  if (!cards.length) return;
  const card = cards[cards.length - 1];
  $(".simbox", card)?.remove();
  const box = el("div", { class: "simbox sim" }, el("div", { class: d.stock === "simulated_confirmed" ? "stock-sim" : "stock-bad", text: d.label }),
    d.price_change ? el("div", { text: d.price_change }) : null,
    d.location ? el("div", { text: "نشانی‌ای که گفت: " + d.location }) : null,
    d.hold_until ? el("div", { text: `تا ${fa(d.hold_until)} نگه می‌دارن` }) : null);
  $(".lines", card).after(box);
}

// --- plan («برنامه‌ی خرید») ------------------------------------------------------------------------------------------

const isPhone = /Android|iPhone|iPad/i.test(navigator.userAgent);

function renderPlan(p, quiet = false) {
  const sim = p.simulated || {};
  const nav = isPhone ? p.nav.geo_uri : p.nav.osm_url;
  const card = el("article", { class: "card plan" },
    el("h3", { text: "برنامه‌ی خرید" }),
    el("div", { class: "name", text: `${p.name} — ${p.product_title}` }),
    p.where ? el("div", { class: "meta", text: p.where }) : null,
    el("div", { class: "meta" }, el("bdi", { text: p.dataset.price }), "، ", el("bdi", { text: p.dataset.age })),
    el("div", { class: "meta", text: p.dataset.visit + (p.arrival ? `، رسیدن ${fa(p.arrival)}` : "") }),
    el("div", { class: "simbox sim" }, el("div", { text: sim.label }),
      sim.price_change ? el("div", { text: sim.price_change }) : null,
      sim.hold_until ? el("div", { text: `نگه می‌دارن تا ${sim.hold_until}` }) : null,
      sim.hold_short ? el("div", { class: "warn", text: "نگه‌داشتنش تا رسیدنت کافی نیست." }) : null),
    p.status === "closed_at_arrival" && p.switch_to ? el("div", { class: "card alt" },
      el("div", { text: p.switch_to.text }), p.switch_to.chip ? chipButton(p.switch_to.chip) : null) : null,
    p.backup ? el("div", { class: "meta", text: `فروشگاه پشتیبان: ${p.backup.name}، ${p.backup.visit}` }) : null,
    el("a", { class: "primary", href: nav, target: isPhone ? null : "_blank", rel: "noopener", text: "مسیر" }),
    el("div", { class: "chips start-align" }, chipButton(p.recheck)));
  planSlot.replaceChildren(card);
  const bar = $("#plan-bar");
  bar.hidden = false;
  bar.textContent = `برنامه‌ی خرید: ${p.name}، ${p.dataset.visit}` + (p.status === "closed_at_arrival" ? " — تا برسی بسته‌ست" : "");
  if (!quiet) stream.append(el("div", { class: "status", text: "برنامه‌ی خرید بالای صفحه به‌روز شد ↑" }));
}

// --- conditions strip ---------------------------------------------------------------------------------------------

const RADII = [[1, "۱ کیلومتر"], [3, "۳ کیلومتر"], [10, "۱۰ کیلومتر"], [null, "کل تهران"]];
const BUDGETS = [[1e6, "تا ۱ میلیون"], [3e6, "تا ۳ میلیون"], [10e6, "تا ۱۰ میلیون"], [50e6, "تا ۵۰ میلیون"], [null, "فرقی نداره"]];
const PAYMENTS = [["any", "همه"], ["credit", "قسطی"], ["credit_in_person", "قسطی حضوری"]];
const OPENS = [["at_arrival", "باز تا وقتی برسم"], ["now", "باز الان"], ["any", "مهم نیست"]];

function km(r) { return r == null ? "کل تهران" : r < 1 ? `${fa(Math.round(r * 1000))} متر` : `${fa(r)} کیلومتر`; }

function setConditions(d) {
  cond = d;
  $("#loc-label").textContent = d.area || "تهران";
  if (hasResults) strip.hidden = false;      // R1: the strip appears with the first results, not before
  const set = (key, label, on) => { const b = $(`[data-cond="${key}"]`, strip); b.textContent = label; b.classList.toggle("on", on); };
  const pay = (PAYMENTS.find((p) => p[0] === d.payment) || PAYMENTS[0])[1];
  set("radius", km(d.radius_km), d.radius_km !== 3);
  set("budget", d.budget_label || "بودجه: فرقی نداره", !!d.budget_max);
  set("payment", (d.payment === "any" ? "پرداخت: همه" : pay) + (d.fast_delivery ? "، ارسال فوری" : ""),
      d.payment !== "any" || d.fast_delivery);
  set("open", (OPENS.find((o) => o[0] === d.open) || OPENS[0])[1], d.open !== "at_arrival");
  set("arrive", d.arrive_label || (d.arrive_at ? `رسیدن ${fa(d.arrive_at)}` : "رسیدن: الان"), !!d.arrive_at);
  const demo = $("#demo-time");
  demo.hidden = !d.demo_time;
  if (d.demo_time) demo.textContent = `زمان دمو ${fa(d.demo_time)}`;
}

function openSheet(title, build) {
  if (sheet.open || busy) return;
  sheetTitle.textContent = title;
  sheetBody.replaceChildren(build());
  sheet.showModal();
}
function opts(list, current, apply) {
  return el("div", { class: "opts" }, list.map(([v, label]) => el("button", {
    type: "button", class: "opt" + (v === current ? " on" : ""), text: label,
    onclick: () => { sheet.close(); apply(v); } })));
}

const SHEETS = {
  radius: () => openSheet("محدوده", () => opts(RADII, cond ? cond.radius_km : 3, (v) => refine({ radius_km: v }))),
  budget: () => openSheet("بودجه", () => {
    const input = el("input", { type: "text", inputmode: "decimal", placeholder: "مثلاً ۲٫۵ (میلیون تومان)" });
    const err = el("div", { class: "error" });
    return el("div", {}, opts(BUDGETS, cond ? cond.budget_max : null, (v) => refine({ budget_max: v })),
      el("div", { style: "margin-top:8px" }, input, err,
        el("button", { type: "button", class: "primary", text: "اعمال", onclick: () => {
          const m = parseFloat(toLatin(input.value));
          if (!(m > 0)) { err.textContent = "یه عدد بنویس، مثلاً ۲٫۵"; return; }
          sheet.close(); refine({ budget_max: Math.round(m * 1e6) });
        } })));
  }),
  payment: () => openSheet("پرداخت", () => el("div", {},
    opts(PAYMENTS, cond ? cond.payment : "any", (v) => refine({ payment: v })),
    el("label", { class: "check" },
      el("input", { type: "checkbox", checked: cond?.fast_delivery || null,
        onchange: (e) => { sheet.close(); refine({ fast_delivery: e.target.checked }); } }), "فقط ارسال فوری"))),
  open: () => openSheet("باز بودن", () => opts(OPENS, cond ? cond.open : "at_arrival", (v) => refine({ open: v }))),
  arrive: () => openSheet("کی می‌رسی؟", () => {
    const t = el("input", { type: "time", value: cond?.arrive_at || "" });
    return el("div", {}, t,
      el("button", { type: "button", class: "primary", text: "همین ساعت", onclick: () => { if (t.value) { sheet.close(); refine({ arrive_at: t.value }); } } }),
      el("button", { type: "button", class: "opt wide", text: "الان راه می‌افتم",
        onclick: () => { sheet.close(); refine({ arrive_at: null }); } }));
  }),
};

function pickLocation(args, label) {
  // Before any results, changing the area is silent: the start screen stays, only the chip label changes.
  chip({ label, kind: "direct_call", tool: "set_location", args }, hasResults ? label : null);
}

async function openLocation() {
  if (busy || sheet.open) return;
  let areas = [];
  try { const r = await (await fetch("/api/areas")).json(); if (Array.isArray(r)) areas = r; } catch (_) {}
  openSheet("کجایی؟", () => el("div", { class: "opts" },
    el("button", { type: "button", class: "opt", text: "مکان من (GPS)", onclick: () => {
      sheet.close();
      if (!navigator.geolocation) return showError("مرورگرت مکان رو نمی‌ده؛ یه محله انتخاب کن.");
      navigator.geolocation.getCurrentPosition((p) => {
        const { latitude: lat, longitude: lng } = p.coords;
        if (lat < 35.5 || lat > 35.9 || lng < 51 || lng > 51.8) return showError("بیرون از تهرانی؛ داده‌ی ترب فقط تهران رو داره. یه محله انتخاب کن.");
        pickLocation({ lat, lng, label: "مکان من", source: "gps" }, "مکان من");
      }, () => showError("اجازه‌ی مکان داده نشد؛ یه محله انتخاب کن."), { timeout: 8000 });
    } }),
    areas.slice(0, 14).map((a) => el("button", { type: "button", class: "opt",
      text: `${a.area}، ${fa(a.shops_with_offers)} فروشگاه`, onclick: () => { sheet.close();
        pickLocation({ lat: a.lat, lng: a.lon, label: a.area, source: "area" }, a.area); } }))));
}

// --- wiring ---------------------------------------------------------------------------------------------------------

function measureSticky() {
  const top = $(".top").offsetHeight, s = strip.hidden ? 0 : strip.offsetHeight;
  document.documentElement.style.setProperty("--top-h", `${top}px`);
  document.documentElement.style.setProperty("--strip-h", `${s}px`);
}
new ResizeObserver(measureSticky).observe(document.body);

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("#text");
  if (say(t.value)) t.value = "";             // keep the text if it couldn't be sent (busy)
});
document.addEventListener("click", (e) => {
  const s = e.target.closest("[data-say]"); if (s) say(s.dataset.say);
  const c = e.target.closest("[data-cond]"); if (c && SHEETS[c.dataset.cond]) SHEETS[c.dataset.cond]();
});
$("#loc").addEventListener("click", openLocation);
$("#plan-bar").addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
$("#reset").addEventListener("click", () => {
  fetch("/api/reset", { method: "POST" }).catch(() => {}).finally(() => location.reload());
});
window.addEventListener("scroll", () => strip.classList.toggle("compact", window.scrollY > 160), { passive: true });

(async () => {
  try {
    const s = await (await fetch("/api/state")).json();
    hasResults = !!s.has_results;
    setConditions(s.conditions);
    if (s.plan) renderPlan(s.plan, true);     // a reload keeps the shopping plan
    if (hasResults) $("#start .resume")?.removeAttribute("hidden");
  } catch (_) {}
  measureSticky();
})();
