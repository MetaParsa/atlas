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
const callNames = {};
const RADISH = "/static/radish.gif";   // Torob's radish mascot is the assistant's face
const agentRow = (bubble) => el("div", { class: "arow" }, bubble, el("img", { class: "avatar-r", src: RADISH, alt: "" }));

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

// Inline stroke icons, built as DOM nodes (no HTML parsing).
const ICONS = {
  pin: ["M12 22s7-6.2 7-12a7 7 0 1 0-14 0c0 5.8 7 12 7 12z", "c12,10,2.5"],
  clock: ["c12,12,9", "M12 7v5l3 2"],
  box: ["M21 8l-9-5-9 5 9 5 9-5z", "M3 8v8l9 5 9-5V8", "M12 13v8"],
  phone: ["M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"],
  store: ["M3 9l1.5-5h15L21 9", "M4 9v11h16V9", "M3 9h18", "M9 20v-6h6v6"],
  nav: ["M3 11l19-9-9 19-2-8-8-2z"],
  check: ["M5 12l5 5L20 7"],
  tag: ["M20.6 13.4l-7.2 7.2a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z", "c7.5,7.5,1.5"],
  list: ["M8 6h13M8 12h13M8 18h13", "M3.5 6h.01M3.5 12h.01M3.5 18h.01"],
  image: ["M3 5h18v14H3z", "c9,10,2", "M21 16l-5-5-9 8"],
  refresh: ["M21 12a9 9 0 1 1-3-6.7L21 8", "M21 3v5h-5"],
};
function icon(name, cls) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  for (const d of ICONS[name] || []) {
    let n;
    if (d.startsWith("c")) {
      const [cx, cy, r] = d.slice(1).split(",");
      n = document.createElementNS(NS, "circle");
      n.setAttribute("cx", cx); n.setAttribute("cy", cy); n.setAttribute("r", r);
    } else { n = document.createElementNS(NS, "path"); n.setAttribute("d", d); }
    svg.append(n);
  }
  return svg;
}
const scrollEnd = () => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });

// --- the radish: one animated mascot that acts out what the assistant is doing ------------------------------------
// Frames come from radish-sheet.png (built by torob/tools/radish_sprites.py). A switch plays the old state's props
// away, a squash-and-pop, then the new state's props in. Each state stays at least DWELL ms, so a burst of events
// still reads as a short story instead of flicker; only the last few requested states are kept.
const Radish = (() => {
  const S = window.RADISH_SPRITES, sheet = new Image(), canvases = new Set();
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DWELL = 900, NAP = 45_000;
  let cur = "idle", queue = [], plan = [], looping = false, since = 0, lastSet = performance.now(), frame = 0;

  function draw(i) {
    frame = i;
    if (!sheet.complete || !sheet.naturalWidth) return;
    for (const c of canvases) {
      const g = c.getContext("2d");
      g.imageSmoothingEnabled = false;
      g.clearRect(0, 0, S.w, S.h);
      g.drawImage(sheet, i * S.w, 0, S.w, S.h, 0, 0, S.w, S.h);
    }
  }
  function step() {
    const now = performance.now();
    if (cur === "idle" && !queue.length && now - lastSet > NAP) queue.push("sleep");
    if (looping && queue.length && now - since >= DWELL) plan = [];
    if (!plan.length) {
      if (queue.length && now - since >= DWELL) {
        const a = S.states[cur], b = S.states[queue[0]], seq = (ids, ms) => ids.map((f) => [f, ms]);
        cur = queue.shift();
        plan = [...seq([...a.in].reverse(), a.ms), ...seq(S.pop, S.popMs), ...seq(b.in, b.ms)];
        since = now + plan.reduce((t, [, ms]) => t + ms, 0);
        looping = false;
      } else {
        const s = S.states[cur];
        plan = s.loop.map((f) => [f, s.ms]);
        looping = true;
      }
    }
    const [f, ms] = plan.shift();
    draw(f);
    setTimeout(step, ms);
  }

  sheet.onload = () => draw(frame);
  sheet.src = "/static/radish-sheet.png";
  if (!still) setTimeout(step, 0); else draw(S.states.idle.loop[0]);
  return {
    attach(c) { c.width = S.w; c.height = S.h; canvases.add(c); draw(frame); },
    set(state) {
      lastSet = performance.now();
      if (still) { cur = state; draw(S.states[state].loop[0]); return; }
      if ((queue.at(-1) ?? cur) === state) return;
      queue.push(state);
      if (queue.length > 3) queue.splice(0, queue.length - 3);
    },
  };
})();

// --- talking to the server ---------------------------------------------------------------------------------------

function setBusy(on) {
  busy = on;
  document.body.classList.toggle("busy-all", on);
  $("#send").disabled = on;
}

async function post(url, body) {
  if (busy) return false;
  setBusy(true);
  turn = { status: null, sawDone: false, block: null, failed: false };
  Radish.set("think");
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

function showError(text) { leaveStart(); stream.append(el("div", { class: "error", text })); oops(); }
function leaveStart() { $("#start")?.remove(); $("#radish-dock").hidden = false; }
function oops() { if (turn) turn.failed = true; Radish.set("oops"); }

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
function chipButton(c, cls = "chip", bubble, ic) {
  return el("button", { type: "button", class: cls, onclick: () => chip(c, bubble ?? c.label) },
    ic ? icon(ic) : null, el("span", { text: c.label }));
}
function refine(patch) { if (!busy) post("/api/refine", { conditions: patch }); }

// --- events --------------------------------------------------------------------------------------------------------

const handlers = {
  status(d) {
    if (!turn.status) {
      turn.status = el("div", { class: "status" }, el("span"));
      stream.append(turn.status);
    }
    turn.status.lastChild.textContent = d.text;
    turn.status.classList.toggle("busy", !d.done);
    if (!d.done) Radish.set(d.text.includes("تماس") ? "call" : d.text.includes("فروشنده") ? "map" : "search");
  },
  message(d) { if (d.text) { stream.append(agentRow(el("div", { class: "bubble agent", text: d.text }))); Radish.set("text"); } },
  error(d) { stream.append(el("div", { class: "error", text: d.text })); oops(); },
  conditions(d) { setConditions(d); },
  question(d) {
    stream.append(el("div", { class: "question" }, agentRow(el("div", { class: "bubble agent", text: d.text })),
      el("div", { class: "chips" }, (d.chips || []).map((c) => chipButton(c)))));
    Radish.set("text");
  },
  product_cards(d) { hasResults = true; strip.hidden = false; putBlock("blk-products", productBlock(d)); Radish.set("happy"); },
  shop_cards(d) { hasResults = true; strip.hidden = false; putBlock("blk-shops", shopBlock(d)); Radish.set("happy"); },
  call_start(d) {
    callNames[d.call_id] = d.shop_name || "فروشنده";
    Radish.set("call");
    stream.append(el("div", { class: "call sim", id: `call-${d.call_id}` },
      el("div", { class: "head" }, el("span", { class: "ring" }, icon("phone")), el("span", {}, d.title, simTag())),
      el("div", { class: "why", text: d.reasons }),
      d.real_shop_note ? el("div", { class: "note", text: d.real_shop_note }) : null));
  },
  call_line(d) {
    const who = d.who === "shop" ? callNames[d.call_id] || "فروشنده" : "دستیار";
    $(`#call-${d.call_id}`)?.append(el("div", { class: `line ${d.who}` }, el("span", { class: "who", text: who }), d.text));
    scrollEnd();
  },
  call_result(d) {
    const box = $(`#call-${d.call_id}`);
    if (turn.status) { turn.status.remove(); turn.status = null; }   // «در حال تماس…» is over
    box?.append(el("div", { class: "result" }, icon("check"), el("span", { text: d.label })));
    Radish.set("happy");
  },
  card_update(d) { updateShopCard(d); },
  plan(d) { renderPlan(d); Radish.set("map"); },
  alternative(d) {
    stream.append(el("div", { class: "card alt" },
      el("div", { class: d.simulated ? "simbox sim" : "" }, el("span", { text: d.text }), d.simulated ? simTag() : null),
      d.chip ? el("div", { class: "chips start-align" }, chipButton(d.chip)) : null));
    scrollEnd();
  },
  done() { turn.sawDone = true; if (turn.status) turn.status.classList.remove("busy"); if (!turn.failed) Radish.set("idle"); },
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
const shortTitle = (t) => t.split("|")[0].trim();

function productImage(p) {
  const box = el("div", { class: "pimg" });
  const ph = () => icon("image", "ph");
  box.append(p.image ? el("img", { src: p.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer",
    onerror: (e) => e.target.replaceWith(ph()) }) : ph());
  if (p.role_label) box.append(el("span", { class: `role ${p.role || ""}` }, p.role === "best_value" ? icon("check") : null, p.role_label));
  return box;
}

function productBlock(d) {
  const items = d.items || [];
  const block = el("section");
  (d.notes || []).forEach((n) => block.append(el("div", { class: "note", text: n })));
  if (!items.length) return block;
  block.append(el("div", { class: "block-h" }, el("h3", { text: "مدل‌های پیشنهادی" }),
    el("span", {}, `${fa(items.length)} مدل`, el("span", { class: "hint", text: "، بکش برای بقیه" }))));
  const row = el("div", { class: "row" });
  for (const p of items) {
    const scope = p.price_scope === "area" ? "نزدیکت" : "در تهران";
    const sellers = p.price_scope === "area" ? p.sellers_in_area : p.sellers_total;
    row.append(el("article", { class: "card pcard" }, productImage(p),
      el("div", { class: "pbody" },
        el("div", { class: "title", text: p.title }),
        p.price_min ? el("div", { class: "price-big" }, el("small", { text: "از " }), el("bdi", { text: money(p.price_min) }), el("small", { text: " تومان" }))
                    : el("div", { class: "price-big" }, el("small", { text: "قیمت ثبت نشده" })),
        el("div", { class: "meta" }, p.price_max && p.price_max !== p.price_min ? el("bdi", { text: `تا ${money(p.price_max)}، ` }) : null,
          `قیمت ثبت‌شده ${scope}`),
        sellers ? el("div", {}, el("span", { class: "pill blue" }, icon("store"), `${fa(sellers)} فروشنده ${scope}`)) : null,
        p.over_budget_pct ? el("div", { class: "over", text: `${fa(p.over_budget_pct)}٪ بالاتر از بودجه` }) : null,
        p.spec_lines?.length ? el("ul", { class: "specs" }, p.spec_lines.map((s) => el("li", { text: s, title: s }))) : null,
        el("div", { class: "why", text: "چرا: " + p.why }),
        p.button ? chipButton(p.button, "btn blue", `فروشنده‌های «${shortTitle(p.title)}»`, "store") : null)));
  }
  block.append(row);
  if (d.compare_rows?.length) {
    const t = el("table", { class: "compare" },
      el("tr", {}, el("th", { text: "" }), items.map((p) => el("th", { text: p.role_label }))),
      d.compare_rows.map((r) => el("tr", {}, el("td", { text: r.key }), r.values.map((v) => el("td", { text: v })))));
    block.append(el("details", { class: "compare-wrap" }, el("summary", { text: "مقایسه‌ی مشخصات" }), t));
  }
  return block;
}

// --- shops (steps 2–3) --------------------------------------------------------------------------------------------

function shopPrice(pr) {
  const box = el("div", { class: "sprice" });
  if (pr.min) {
    box.append(el("div", { class: "amt" }, el("bdi", { text: money(pr.min) }), el("small", { text: " تومان" })));
    if (pr.max && pr.max !== pr.min) box.append(el("div", { class: "meta" }, el("bdi", { text: `تا ${money(pr.max)}` })));
    if (pr.per_metre) box.append(el("div", { class: "meta", text: "قیمت متری" }));
  } else box.append(el("div", { class: "amt" }, el("small", { text: pr.text })));
  box.append(el("div", { class: "age" + (pr.old ? " old" : "") }, el("bdi", { text: pr.age }), pr.note ? `، ${pr.note}` : null));
  return box;
}

function line(ic, text, cls) {
  return el("div", { class: "ln" + (cls ? " " + cls : "") }, icon(ic), el("span", { class: cls?.startsWith("open-") ? "st" : null, text }));
}

function shopCard(s, compact = false) {
  const open = s.visit.open_at_arrival === "yes" ? "open-yes" : s.visit.open_at_arrival === "no" ? "open-no" : "";
  const card = el("article", { class: "card scard" + (compact ? " compact" : ""), "data-shop-id": s.shop_id },
    el("div", { class: "shead" },
      compact ? null : el("span", { class: "avatar" }, icon("store")),
      el("div", { class: "sname" }, el("div", { class: "name", text: s.name }), s.area ? el("div", { class: "meta", text: s.area }) : null),
      shopPrice(s.price)),
    el("div", { class: "lines" },
      line("pin", s.where, "where"),
      line("clock", s.visit.text, open),
      compact ? null : line("box", s.stock_label, "stock")));
  if (!compact && s.reasons?.length) {
    card.append(el("div", { class: "badges" }, s.reasons.map((r, i) =>
      el("span", { class: "pill" + (s.badges?.includes("credit") && r === "قسطی" ? " green" : "") }, i === 0 ? icon("tag") : null, r))));
  }
  if (!compact) {
    const det = s.details;
    card.append(el("details", {}, el("summary", { text: "جزئیات فروشنده" }),
      el("div", { class: "meta", text: det.payments.join("، ") }),
      el("div", { class: "meta", text: det.warranty }),
      el("div", { class: "meta", text: det.fast_delivery }),
      el("div", { class: "meta", text: "عنوان فروشنده: " + det.seller_title })));
  }
  card.append(chipButton(s.button, compact ? "btn line" : "btn red", `زنگ به «${s.name}»`, "phone"));
  return card;
}

function shopBlock(d) {
  const block = el("section");
  if (d.product) block.append(el("div", { class: "block-h" }, el("h3", { text: `فروشنده‌های «${shortTitle(d.product.title)}»` }),
    d.items?.length ? el("span", { text: `${fa(d.items.length + (d.more_count || 0))} فروشگاه` }) : null));
  (d.notes || []).forEach((n) => block.append(el("div", { class: "note", text: n })));
  if (d.relaxed?.chip) block.append(el("div", { class: "chips start-align" }, chipButton(d.relaxed.chip)));
  (d.items || []).forEach((s) => block.append(shopCard(s)));
  if (d.junk_line) block.append(el("div", { class: "junk", text: d.junk_line }));
  if (d.more?.length) {
    block.append(el("details", { class: "more" }, el("summary", { text: `${fa(d.more_count)} فروشگاه دیگه هم دارن` }),
      d.more.map((s) => shopCard(s, true))));
  }
  if (d.category_only?.length) {
    block.append(el("details", { class: "more" }, el("summary", { text: `${fa(d.category_only.length)} فروشگاه فقط دسته‌ی مرتبط دارن` }),
      el("div", { class: "meta", text: "این‌ها این مدل رو ثبت نکرده‌اند؛ موجودی نامعلومه." }),
      d.category_only.map((s) => el("div", { class: "card catcard" }, el("div", { class: "name", text: s.name }),
        el("div", { class: "lines" }, line("pin", s.where), line("clock", `${fa(s.minutes)} دقیقه (تخمینی)`), line("box", s.stock_label))))));
  }
  return block;
}

// The call result goes into its own simulated row; the dataset rows (where, price, stock label) are untouched.
function updateShopCard(d) {
  const cards = document.querySelectorAll(`[data-shop-id="${CSS.escape(d.shop_id)}"]`);
  if (!cards.length) return;
  const card = cards[cards.length - 1];
  $(".simbox", card)?.remove();
  const box = el("div", { class: "simbox sim" },
    el("div", { class: d.stock === "simulated_confirmed" ? "stock-sim" : "stock-bad" }, el("span", { text: d.label }), simTag()),
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
  const open = p.open_at_arrival === "yes" ? "open-yes" : p.status === "closed_at_arrival" ? "open-no" : "";
  const card = el("article", { class: "card plan" },
    el("div", { class: "plan-h" }, icon("list"), el("span", { text: "برنامه‌ی خرید" })),
    el("div", { class: "plan-b" },
      el("div", { class: "name", text: p.name }),
      el("div", { class: "meta", text: p.product_title }),
      el("div", { class: "lines" },
        p.where ? line("pin", p.where) : null,
        el("div", { class: "ln" }, icon("tag"), el("span", {}, el("bdi", { text: p.dataset.price }), "، ", el("bdi", { text: p.dataset.age }))),
        line("clock", p.dataset.visit + (p.arrival ? `، رسیدن ${fa(p.arrival)}` : ""), open)),
      el("div", { class: "simbox sim" }, el("div", {}, el("span", { text: sim.label }), simTag()),
        sim.price_change ? el("div", { text: sim.price_change }) : null,
        sim.hold_until ? el("div", { text: `نگه می‌دارن تا ${sim.hold_until}` }) : null,
        sim.hold_short ? el("div", { class: "warn", text: "نگه‌داشتنش تا رسیدنت کافی نیست." }) : null),
      p.status === "closed_at_arrival" && p.switch_to ? el("div", { class: "card alt" },
        el("div", { text: p.switch_to.text }), p.switch_to.chip ? chipButton(p.switch_to.chip) : null) : null,
      p.backup ? el("div", { class: "meta", text: `فروشگاه پشتیبان: ${p.backup.name}، ${p.backup.visit}` }) : null,
      el("a", { class: "btn red", href: nav, target: isPhone ? null : "_blank", rel: "noopener" }, icon("nav"), el("span", { text: "مسیر تا فروشگاه" })),
      el("div", { class: "chips start-align" }, chipButton(p.recheck, "chip", undefined))));
  planSlot.replaceChildren(card);
  const bar = $("#plan-bar");
  bar.hidden = false;
  bar.replaceChildren(icon("list"), el("span", { text: `برنامه‌ی خرید: ${p.name}، ${p.dataset.visit}` + (p.status === "closed_at_arrival" ? " — تا برسی بسته‌ست" : "") }));
  const wide = matchMedia("(min-width: 1024px)").matches;   // desktop pins the plan beside the chat
  if (!quiet) stream.append(el("div", { class: "status", text: wide ? "برنامه‌ی خرید کنار صفحه به‌روز شد" : "برنامه‌ی خرید بالای صفحه به‌روز شد ↑" }));
  measureSticky();
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
        el("button", { type: "button", class: "btn blue", text: "اعمال", onclick: () => {
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
    // Pre-fill only a same-day arrival; a «شنبه ۱۰:۰۰» plan is shown as text so re-confirming can't lose its date.
    const t = el("input", { type: "time", value: cond?.arrive_today ? cond.arrive_at : "" });
    return el("div", {}, cond?.arrive_label ? el("div", { class: "meta", text: "الان: " + cond.arrive_label }) : null, t,
      el("button", { type: "button", class: "btn blue", text: "همین ساعت", onclick: () => { if (t.value) { sheet.close(); refine({ arrive_at: t.value }); } } }),
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
    el("button", { type: "button", class: "opt plain", text: "مکان من (GPS)", onclick: () => {
      sheet.close();
      if (!navigator.geolocation) return showError("مرورگرت مکان رو نمی‌ده؛ یه محله انتخاب کن.");
      navigator.geolocation.getCurrentPosition((p) => {
        const { latitude: lat, longitude: lng } = p.coords;
        if (lat < 35.5 || lat > 35.9 || lng < 51 || lng > 51.8) return showError("بیرون از تهرانی؛ داده‌ی ترب فقط تهران رو داره. یه محله انتخاب کن.");
        pickLocation({ lat, lng, label: "مکان من", source: "gps" }, "مکان من");
      }, () => showError("اجازه‌ی مکان داده نشد؛ یه محله انتخاب کن."), { timeout: 8000 });
    } }),
    areas.slice(0, 14).map((a) => el("button", { type: "button", class: "opt" + (cond && a.area === cond.area ? " on" : ""),
      text: `${a.area}، ${fa(a.shops_with_offers)} فروشگاه`, onclick: () => { sheet.close();
        pickLocation({ lat: a.lat, lng: a.lon, label: a.area, source: "area" }, a.area); } }))));
}

// --- wiring ---------------------------------------------------------------------------------------------------------

function measureSticky() {
  const top = $(".top").offsetHeight, bar = $("#plan-bar");
  document.documentElement.style.setProperty("--top-h", `${top}px`);
  document.documentElement.style.setProperty("--bar-h", `${bar.hidden ? 0 : bar.offsetHeight + 8}px`);
}
const ro = new ResizeObserver(measureSticky);
ro.observe(document.body); ro.observe($(".top"));

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("#text");
  if (say(t.value)) t.value = "";             // keep the text if it couldn't be sent (busy)
});
$("#text").addEventListener("input", (e) => { if (!busy) Radish.set(e.target.value.trim() ? "listen" : "idle"); });
document.querySelectorAll("canvas.radish").forEach((c) => Radish.attach(c));
Radish.set("happy");                          // says hello, then settles
Radish.set("idle");
document.addEventListener("click", (e) => {
  const s = e.target.closest("[data-say]"); if (s) say(s.dataset.say);
  const c = e.target.closest("[data-cond]"); if (c && SHEETS[c.dataset.cond]) SHEETS[c.dataset.cond]();
});
$("#loc").addEventListener("click", openLocation);
$("#plan-bar").addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
$("#reset").addEventListener("click", () => {
  fetch("/api/reset", { method: "POST" }).catch(() => {}).finally(() => location.reload());
});
window.addEventListener("scroll", () => {   // hysteresis: hiding the location row shifts the page, so no flicker at the edge
  const top = $(".top"), y = window.scrollY;
  if (y > 160) top.classList.add("compact"); else if (y < 40) top.classList.remove("compact");
}, { passive: true });

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
