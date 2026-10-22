// Torob in-store shopping assistant — one screen, no framework (spec §9, §0.2 R1–R17).
// Every data string goes through textContent; nothing from the server is parsed as HTML.
"use strict";

const $ = (s, root = document) => root.querySelector(s);
const stream = $("#stream"), strip = $("#strip"), planSlot = $("#plan-slot");
const sheet = $("#sheet"), sheetTitle = $("#sheet-title"), sheetBody = $("#sheet-body");
const FA = "۰۱۲۳۴۵۶۷۸۹";
const fa = (v) => String(v).replace(/\d/g, (d) => FA[d]);
let cond = null, busy = false, turn = null;

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
const scrollEnd = () => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });

// --- talking to the server ---------------------------------------------------------------------------------------

async function post(url, body) {
  if (busy) return;
  busy = true;
  document.body.classList.add("busy-all");
  $("#send").disabled = true;
  $("#start")?.remove();
  turn = { status: null };
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok || !res.body) {
      let msg = "یه مشکلی پیش اومد؛ دوباره امتحان کن.";
      try { msg = (await res.json()).error || msg; } catch (_) {}
      if (res.status === 422) msg = "پیام خیلی طولانیه یا این گزینه درست نیست.";
      stream.append(el("div", { class: "error", text: msg }));
      return;
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
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
  } catch (_) {
    stream.append(el("div", { class: "error", text: "اینترنت قطع شد؛ دوباره امتحان کن." }));
  } finally {
    busy = false;
    document.body.classList.remove("busy-all");
    $("#send").disabled = false;
    scrollEnd();
  }
}

function say(text) {
  if (!text.trim() || busy) return;
  stream.append(el("div", { class: "bubble user", text }));
  post("/api/chat", { text });
}
function chip(c) {
  if (busy) return;
  if (c.kind === "direct_call") {
    if (c.label) stream.append(el("div", { class: "bubble user", text: c.label }));
    post("/api/chat", { chip: c });
  } else say(c.value || c.label);
}
function chipButton(c, cls = "chip") { return el("button", { type: "button", class: cls, text: c.label, onclick: () => chip(c) }); }
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
  product_cards(d) { putBlock("blk-products", productBlock(d)); },
  shop_cards(d) { putBlock("blk-shops", shopBlock(d)); },
  call_start(d) {
    const box = el("div", { class: "call sim", id: `call-${d.call_id}` },
      el("div", { class: "head" }, d.title, el("span", { class: "sim-tag", text: "شبیه‌سازی" })),
      el("div", { class: "why", text: d.reasons }),
      d.real_shop_note ? el("div", { class: "note", text: d.real_shop_note }) : null,
      el("div", { class: "status busy ringing", text: "در حال تماس" }));
    stream.append(box);
  },
  call_line(d) {
    const box = $(`#call-${d.call_id}`);
    if (box) box.append(el("div", { class: `line ${d.who}`, text: d.text }));
    scrollEnd();
  },
  call_result(d) {
    const box = $(`#call-${d.call_id}`);
    if (!box) return;
    $(".ringing", box)?.remove();
    if (turn.status) { turn.status.remove(); turn.status = null; }   // «در حال تماس…» is over
    box.append(el("div", { class: "result", text: d.label }));
  },
  card_update(d) { updateShopCard(d); },
  plan(d) { renderPlan(d); },
  alternative(d) {
    stream.append(el("div", { class: "card alt" }, el("div", { text: d.text }),
      d.chip ? el("div", { class: "chips", style: "justify-content:flex-start;margin-top:6px" }, chipButton(d.chip)) : null));
  },
  done() {},
};
function handle(ev, d) { (handlers[ev] || (() => {}))(d); }

function putBlock(id, node) {
  node.id = id;
  const old = document.getElementById(id);
  if (old) old.removeAttribute("id");
  stream.append(node);            // newest block at the bottom; the old one stays as history
}

// --- products (step 1) -----------------------------------------------------------------------------------------------

function priceRange(min, max) {
  if (!min) return "قیمت ثبت نشده";
  const f = (n) => fa(Number(n).toLocaleString("en")).replace(/,/g, "٬");
  return min === max ? f(min) : `${f(min)} تا ${f(max)}`;
}

function productBlock(d) {
  const items = d.items || [];
  const block = el("section", { class: "block" });
  (d.notes || []).forEach((n) => block.append(el("div", { class: "note", text: n })));
  if (!items.length) return block;
  const row = el("div", { class: "row" });
  for (const p of items) {
    const img = p.image ? el("img", { class: "img", src: p.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer",
      onerror: (e) => e.target.replaceWith(el("div", { class: "img" })) }) : el("div", { class: "img" });
    row.append(el("article", { class: "card pcard" }, img,
      el("div", {}, el("span", { class: "tag", text: p.role_label })),
      el("div", { class: "title", text: p.title }),
      el("div", { class: "meta", text: `قیمت ثبت‌شده ${p.price_scope === "area" ? "نزدیکت" : "در تهران"}: ${priceRange(p.price_min, p.price_max)}` }),
      p.spec_lines?.length ? el("ul", { class: "specs" }, p.spec_lines.map((s) => el("li", { text: s }))) : null,
      el("div", { class: "why", text: "چرا: " + p.why }),
      p.button ? chipButton(p.button, "primary") : null));
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
  card.append(chipButton(s.button, compact ? "chip" : "primary"));
  return card;
}

function shopBlock(d) {
  const block = el("section", { class: "block" });
  if (d.product) block.append(el("h3", { text: `فروشنده‌های «${d.product.title}»` }));
  (d.notes || []).forEach((n) => block.append(el("div", { class: "note", text: n })));
  if (d.relaxed?.chip) block.append(el("div", { class: "chips", style: "justify-content:flex-start" }, chipButton(d.relaxed.chip)));
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

function updateShopCard(d) {
  const card = document.querySelector(`#blk-shops [data-shop-id="${CSS.escape(d.shop_id)}"]`);
  if (!card) return;
  card.classList.add("sim");
  const stock = $(".stock", card);
  stock.textContent = d.label;
  stock.className = "stock " + (d.stock === "simulated_confirmed" ? "stock-sim" : "stock-bad");
  stock.append(el("span", { class: "sim-tag", text: "شبیه‌سازی" }));
  if (d.price_change) $(".lines", card).append(el("div", { class: "stock-sim", text: d.price_change }));
  if (d.location) $(".where", card).textContent = d.location;
}

// --- plan («برنامه‌ی خرید») ------------------------------------------------------------------------------------------

function renderPlan(p) {
  const sim = p.simulated || {};
  const card = el("article", { class: "card plan" },
    el("h3", { text: "برنامه‌ی خرید" }),
    el("div", { class: "name", text: `${p.name} — ${p.product_title}` }),
    p.where ? el("div", { class: "meta", text: p.where }) : null,
    el("div", { class: "meta" }, el("bdi", { text: p.dataset.price }), "، ", el("bdi", { text: p.dataset.age })),
    el("div", { class: "meta", text: p.dataset.visit + (p.arrival ? `، رسیدن ${fa(p.arrival)}` : "") }),
    el("div", { class: "simbox sim" }, el("span", { text: sim.label }), el("span", { class: "sim-tag", text: "شبیه‌سازی" }),
      sim.price_change ? el("div", { text: sim.price_change }) : null,
      sim.hold_until ? el("div", { text: `نگه می‌دارن تا ${sim.hold_until}` }) : null,
      sim.hold_short ? el("div", { class: "warn", text: "نگه‌داشتنش تا رسیدنت کافی نیست." }) : null),
    p.status === "closed_at_arrival" && p.switch_to ? el("div", { class: "card alt" },
      el("div", { text: p.switch_to.text }), p.switch_to.chip ? chipButton(p.switch_to.chip) : null) : null,
    p.backup ? el("div", { class: "meta", text: `فروشگاه پشتیبان: ${p.backup.name}، ${p.backup.visit}` }) : null,
    el("div", { class: "links" },
      el("a", { class: "primary", href: p.nav.osm_url, target: "_blank", rel: "noopener", text: "مسیر" }),
      /Android|iPhone|iPad/i.test(navigator.userAgent) ? el("a", { class: "primary", href: p.nav.geo_uri, text: "باز کردن در نقشه" }) : null),
    el("div", { class: "chips", style: "justify-content:flex-start;margin-top:6px" }, chipButton(p.recheck)));
  planSlot.replaceChildren(card);
  // The full card lives at the top of the page; a slim sticky bar keeps it one tap away while scrolling.
  const bar = $("#plan-bar");
  bar.hidden = false;
  bar.textContent = `برنامه‌ی خرید: ${p.name}، ${p.dataset.visit}` + (p.status === "closed_at_arrival" ? " — تا برسی بسته‌ست" : "");
  stream.append(el("div", { class: "status", text: "برنامه‌ی خرید بالای صفحه به‌روز شد" }));
}

// --- conditions strip ---------------------------------------------------------------------------------------------

const RADII = [[1, "۱ کیلومتر"], [3, "۳ کیلومتر"], [10, "۱۰ کیلومتر"], [null, "کل تهران"]];
const BUDGETS = [[1e6, "تا ۱ میلیون"], [3e6, "تا ۳ میلیون"], [10e6, "تا ۱۰ میلیون"], [50e6, "تا ۵۰ میلیون"], [null, "فرقی نداره"]];
const PAYMENTS = [["any", "همه"], ["credit", "قسطی"], ["credit_in_person", "قسطی حضوری"]];
const OPENS = [["at_arrival", "باز تا وقتی برسم"], ["now", "باز الان"], ["any", "مهم نیست"]];

function km(r) { return r == null ? "کل تهران" : r < 1 ? `${fa(Math.round(r * 1000))} متر` : `${fa(r)} کیلومتر`; }

function setConditions(d) {
  cond = d;
  strip.hidden = false;
  $("#loc-label").textContent = d.area || "تهران";
  const set = (key, label, on) => { const b = $(`[data-cond="${key}"]`, strip); b.textContent = label; b.classList.toggle("on", on); };
  set("radius", km(d.radius_km), d.radius_km !== 3);
  set("budget", d.budget_label || "بودجه: فرقی نداره", !!d.budget_max);
  set("payment", (PAYMENTS.find((p) => p[0] === d.payment) || [0, "همه"])[1] === "همه" ? (d.fast_delivery ? "ارسال فوری" : "پرداخت: همه")
    : (PAYMENTS.find((p) => p[0] === d.payment)[1] + (d.fast_delivery ? "، ارسال فوری" : "")), d.payment !== "any" || d.fast_delivery);
  set("open", (OPENS.find((o) => o[0] === d.open) || OPENS[0])[1], d.open !== "at_arrival");
  set("arrive", d.arrive_at ? `رسیدن ${fa(d.arrive_at)}` : "رسیدن: الان", !!d.arrive_at);
  const demo = $("#demo-time");
  demo.hidden = !d.demo_time;
  if (d.demo_time) demo.textContent = `زمان دمو ${fa(d.demo_time)}`;
}

function openSheet(title, build) {
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
  radius: () => openSheet("محدوده", () => opts(RADII, cond?.radius_km ?? 3, (v) => refine({ radius_km: v }))),
  budget: () => openSheet("بودجه", () => {
    const input = el("input", { type: "number", min: "0.1", step: "0.1", placeholder: "مثلاً ۲٫۵ (میلیون تومان)" });
    return el("div", {}, opts(BUDGETS, cond?.budget_max ?? null, (v) => refine({ budget_max: v })),
      el("div", { style: "margin-top:8px" }, input,
        el("button", { type: "button", class: "primary", text: "اعمال", onclick: () => {
          const m = parseFloat(String(input.value).replace(/[۰-۹]/g, (d) => FA.indexOf(d)));
          if (m > 0) { sheet.close(); refine({ budget_max: Math.round(m * 1e6) }); }
        } })));
  }),
  payment: () => openSheet("پرداخت", () => el("div", {},
    opts(PAYMENTS, cond?.payment ?? "any", (v) => refine({ payment: v })),
    el("label", { style: "display:flex;gap:6px;margin-top:10px;align-items:center" },
      el("input", { type: "checkbox", checked: cond?.fast_delivery || null,
        onchange: (e) => { sheet.close(); refine({ fast_delivery: e.target.checked }); } }), "فقط ارسال فوری"))),
  open: () => openSheet("باز بودن", () => opts(OPENS, cond?.open ?? "at_arrival", (v) => refine({ open: v }))),
  arrive: () => openSheet("کی می‌رسی؟", () => {
    const t = el("input", { type: "time", value: cond?.arrive_at || "" });
    return el("div", {}, t,
      el("button", { type: "button", class: "primary", text: "همین ساعت", onclick: () => { if (t.value) { sheet.close(); refine({ arrive_at: t.value }); } } }),
      el("button", { type: "button", class: "opt", style: "width:100%;margin-top:6px", text: "الان راه می‌افتم",
        onclick: () => { sheet.close(); refine({ arrive_at: null }); } }));
  }),
};

async function openLocation() {
  let areas = [];
  try { areas = await (await fetch("/api/areas")).json(); } catch (_) {}
  openSheet("کجایی؟", () => el("div", { class: "opts" },
    el("button", { type: "button", class: "opt", text: "مکان من (GPS)", onclick: () => {
      sheet.close();
      if (!navigator.geolocation) return stream.append(el("div", { class: "error", text: "مرورگرت مکان رو نمی‌ده؛ یه محله انتخاب کن." }));
      navigator.geolocation.getCurrentPosition((p) => {
        const { latitude: lat, longitude: lng } = p.coords;
        if (lat < 35.5 || lat > 35.9 || lng < 51 || lng > 51.8) {
          return stream.append(el("div", { class: "note", text: "بیرون از تهرانی؛ داده‌ی ترب فقط تهران رو داره. یه محله انتخاب کن." }));
        }
        chip({ label: "مکان من", kind: "direct_call", tool: "set_location", args: { lat, lng, label: "مکان من", source: "gps" } });
      }, () => stream.append(el("div", { class: "note", text: "اجازه‌ی مکان داده نشد؛ یه محله انتخاب کن." })), { timeout: 8000 });
    } }),
    areas.slice(0, 14).map((a) => el("button", { type: "button", class: "opt",
      text: `${a.area}، ${fa(a.shops_with_offers)} فروشگاه`, onclick: () => { sheet.close();
        chip({ label: a.area, kind: "direct_call", tool: "set_location", args: { lat: a.lat, lng: a.lon, label: a.area, source: "area" } }); } }))));
}

// --- wiring ---------------------------------------------------------------------------------------------------------

$("#composer").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#text"); say(t.value); t.value = ""; });
document.addEventListener("click", (e) => {
  const s = e.target.closest("[data-say]"); if (s) say(s.dataset.say);
  const c = e.target.closest("[data-cond]"); if (c && SHEETS[c.dataset.cond]) SHEETS[c.dataset.cond]();
});
$("#loc").addEventListener("click", openLocation);
$("#plan-bar").addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
$("#reset").addEventListener("click", async () => { await fetch("/api/reset", { method: "POST" }); location.reload(); });
window.addEventListener("scroll", () => strip.classList.toggle("compact", window.scrollY > 160), { passive: true });

(async () => {
  try {
    const s = await (await fetch("/api/state")).json();
    $("#loc-label").textContent = s.conditions.area || "تهران";
    if (s.has_results) setConditions(s.conditions); else cond = s.conditions;
  } catch (_) {}
})();
