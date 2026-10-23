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
let replaying = false;                        // rebuilding the chat after a reload: no animation, no scrolling
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
  spark: ["M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z", "M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z"],
  checkc: ["c12,12,9", "M8 12.5l2.7 2.7L16 9.5"],
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
const scrollEnd = () => { if (!replaying) window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); };

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
  sheet.src = S.sheet || "/static/radish-sheet.png";   // versioned by content (the CDN caches /static)
  if (!still) setTimeout(step, 0); else draw(S.states.idle.loop[0]);
  return {
    attach(c) { c.width = S.w; c.height = S.h; canvases.add(c); draw(frame); },
    set(state) {
      if (replaying) return;
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
  $("#attach").disabled = on;
  syncDock();
}
// The docked radish acts out the agent's work at the bottom of the chat; while the user scrolls back up through
// results it steps aside, so it never covers a card they are reading.
function syncDock() {
  const away = document.documentElement.scrollHeight - window.scrollY - window.innerHeight > 24;   // it is 54 px tall over a ~64 px gap
  $("#app").classList.toggle("reading", away && !busy);
}

// Three dots in an agent bubble while the answer is on its way; the first event of the answer replaces them.
let typing = null;
function showTyping() {
  if ($("#start") || typing) return;   // a silent change on the start screen gets no bubble
  typing = agentRow(el("div", { class: "bubble agent typing", role: "status", "aria-label": "تربچه داره می‌نویسه" },
    el("i"), el("i"), el("i")));
  stream.append(typing);
  scrollEnd();
}
function hideTyping() { typing?.remove(); typing = null; }

async function post(url, body) {
  if (busy) return false;
  setBusy(true);
  turn = { status: null, sawDone: false, block: null, failed: false };
  Radish.set("think");
  showTyping();
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
    hideTyping();
    if (turn.status) turn.status.classList.remove("busy");
    setBusy(false);
    if (turn.block) turn.block.scrollIntoView({ behavior: "smooth", block: "start" });   // show the top-ranked card
    else scrollEnd();
  }
  return true;
}

// --- start screen suggestions: your recent searches (kept only in this browser), else what people search most -----
const RECENT_KEY = "torob.recent", RECENT_MAX = 5;
function recentSearches() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(v) ? v.filter((s) => typeof s === "string" && s).slice(0, RECENT_MAX) : [];
  } catch (_) { return []; }
}
function saveRecent(list) { try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (_) {} }
function remember(text) {
  const t = text.trim().replace(/\s+/g, " ").slice(0, 120);
  saveRecent([t, ...recentSearches().filter((s) => s !== t)].slice(0, RECENT_MAX));
}
async function renderSuggestions() {
  const box = $("#suggest");
  if (!box) return;
  const recent = recentSearches();
  if (recent.length) {
    box.replaceChildren(
      el("div", { class: "suggest-h" }, el("span", { text: "جست‌وجوهای اخیرت" }),
        el("button", { type: "button", class: "ghost", text: "پاک کردن", onclick: () => { saveRecent([]); renderSuggestions(); } })),
      el("div", { class: "recent" }, recent.map((t) => el("button", { type: "button", onclick: () => say(t) }, icon("clock"), el("span", { text: t })))));
    return;
  }
  box.replaceChildren();
  let popular = [];
  try { popular = (await (await fetch("/api/popular")).json()).searches || []; } catch (_) {}
  if (popular.length && !recentSearches().length) {   // exactly what people typed, no heading
    box.replaceChildren(el("div", { class: "chips" }, popular.map((t) => el("button", { type: "button", class: "chip", text: t, onclick: () => say(t) }))));
  }
}

function showError(text) {   // the same error again (a second tap on «مکان من») replaces itself instead of stacking
  leaveStart();
  const last = stream.lastElementChild;
  if (last?.classList.contains("error") && last.textContent === text) last.remove();
  stream.append(el("div", { class: "error", text })); oops();
}
function leaveStart() { $("#start")?.remove(); $("#radish-dock").hidden = false; }
function oops() { if (turn) turn.failed = true; Radish.set("oops"); }

const userBubble = (text, thumb) => el("div", { class: "bubble user" + (thumb ? " with-photo" : "") },
  thumb?.startsWith("data:image/") ? el("img", { class: "photo", src: thumb, alt: "عکس کالا" }) : null,
  text ? el("span", { text }) : null);

function say(text, ph = null) {
  text = text.trim();
  if ((!text && !ph) || busy) return false;
  if ($("#start") && text && !ph) remember(text);   // a typed search from the start screen joins the recents
  leaveStart();
  stream.append(userBubble(text, ph?.thumb));
  post("/api/chat", ph ? { text: text || null, image: ph.image, thumb: ph.thumb } : { text });
  return true;
}

// --- photo: the page shrinks it (≤1024 px JPEG + a small preview) and keeps it in the field until sent ------------
let photo = null;
async function shrink(file, max, quality) {
  const bmp = await createImageBitmap(file);
  const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
  const c = el("canvas", { width: Math.round(bmp.width * k), height: Math.round(bmp.height * k) });
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  bmp.close?.();
  return c.toDataURL("image/jpeg", quality);
}
async function attachPhoto(file) {
  if (!file?.type.startsWith("image/") || busy) return;
  try { photo = { image: await shrink(file, 1024, 0.82), thumb: await shrink(file, 240, 0.7) }; }
  catch (_) { photo = null; showError("این عکس باز نشد؛ یه عکس دیگه امتحان کن."); }
  renderPhoto();
  $("#text").focus();
}
function renderPhoto() {
  const chip = $("#photo");
  chip.hidden = !photo;
  if (photo) $("img", chip).src = photo.thumb;
  syncSend();
}
const syncSend = () => $("#composer").classList.toggle("ready", !!photo || !!$("#text").value.trim());
// `bubble`: what the user "said" by tapping (null = say nothing, e.g. a silent location change before any search).
function chip(c, bubble = c.label) {
  if (busy) return;
  if (c.kind === "direct_call") {
    if (bubble) { leaveStart(); stream.append(userBubble(bubble)); }
    post("/api/chat", { chip: c, bubble: bubble || null });
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
    const budget = (d.chips || []).some((c) => c.tool === "set_budget");
    if (budget) lastBudgetSteps = d.chips.filter((c) => c.args?.budget_max).map((c) => [c.args.budget_max, c.label]);
    stream.append(el("div", { class: "question" }, agentRow(el("div", { class: "bubble agent", text: d.text })),
      el("div", { class: "chips" }, (d.chips || []).map((c) => chipButton(c)),
        budget ? el("button", { type: "button", class: "chip", onclick: () => SHEETS.budget() }, icon("tag"), el("span", { text: "مبلغ دلخواه" })) : null)));
    Radish.set("text");
  },
  product_cards(d) { hasResults = true; strip.hidden = false; putBlock("blk-products", productBlock(d)); Radish.set("happy"); },
  shop_cards(d) { hasResults = true; strip.hidden = false; shownProduct = d.product || null; putBlock("blk-shops", shopBlock(d)); Radish.set("happy"); },
  call_start(d) {
    callNames[d.call_id] = d.shop_name || "فروشنده";
    Radish.set("call");
    stream.append(el("div", { class: "call sim", id: `call-${d.call_id}` },
      el("div", { class: "head" }, el("span", { class: "ring" }, icon("phone")), el("span", {}, d.title, simTag())),
      el("div", { class: "why", text: d.reasons }),
      d.real_shop_note ? el("div", { class: "note", text: d.real_shop_note }) : null));
  },
  call_line(d) {
    const who = d.who === "shop" ? callNames[d.call_id] || "فروشنده" : "تربچه";
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
  hub(d) {
    const box = el("section", { class: "gen hubbox" });
    box.append(agentRow(el("div", { class: "bubble agent", text: d.text })));
    const max = Math.max(...d.buildings.map((b) => b.shops), 1);
    const card = el("div", { class: "card gcard" }, el("div", { class: "gen-title", text: d.spread ? `فروشنده‌های «${d.subject}»` : `بورس «${d.subject}»` }),
      el("div", { class: "meta", text: `طبق داده‌ی ترب: ${fa(d.total)} فروشنده‌ی حضوری در تهران` }));
    const mm = miniMap({ hubs: d.buildings, center: cond?.center, onOpen: () => openHubMap(d) }, "نقشه‌ی بورس");
    const node = mm.firstChild, center = cond?.center;
    node.drawMap = async (M) => {   // every seller of the trade as a dot, then the hubs on top
      let dots = [];
      try { dots = (await (await fetch("/api/map/category?" + d.categories.map((c) => "c=" + encodeURIComponent(c)).join("&"))).json()).dots || []; } catch (_) {}
      M.mini(node, { hubs: d.buildings, dots, center, onOpen: () => openHubMap(d) });
    };
    card.append(mm);
    if (d.buildings.length) card.append(el("div", { class: "hub-rows" }, d.buildings.slice(0, 4).map((b, i) => el("div", { class: "hub-row" + (i === 0 && !d.spread ? " top" : "") },
      el("span", { class: "r-name", text: `${b.kind} ${b.name}` }), el("span", { class: "r-bar" }, el("span", { style: `inline-size:${Math.round((b.shops / max) * 100)}%` })),
      el("bdi", { class: "r-n", text: fa(b.shops) })))));
    box.append(card);
    if (d.chips?.length) box.append(el("div", { class: "chips gen-actions" }, d.chips.map((c) => chipButton(c))));
    stream.append(box);
    Radish.set("map");
    scrollEnd();
  },
  ui(d) {   // generative UI: blocks the model chose for this answer, every value filled in by the server
    if (d.mode === "intro") $("#blk-products .compare-wrap")?.remove();   // the AI's comparison replaces the generic one
    const box = el("section", { class: "gen" });
    if (d.text) box.append(agentRow(el("div", { class: "bubble agent", text: d.text })));
    for (const b of d.blocks || []) { const n = genBlock(b); if (n) box.append(n); }
    stream.append(box);
    Radish.set(d.blocks?.length ? "happy" : "text");
    scrollEnd();
  },
  plan(d) { renderPlan(d, replaying); Radish.set("map"); },
  user(d) { stream.append(userBubble(d.text, d.image)); },   // only in a replayed history
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
  if (ev !== "conditions" && ev !== "done") hideTyping();   // the answer has started
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

// --- maps: Leaflet and map.js load on first use; small maps are drawn when they scroll into view ---------------------
let mapsLoading = null, shownProduct = null;
function maps() {
  if (window.TorobMap) return Promise.resolve(window.TorobMap);
  if (!mapsLoading) {
    const add = (src) => new Promise((ok, no) => document.head.append(el("script", { src, onload: ok, onerror: no })));
    document.head.append(el("link", { rel: "stylesheet", href: "/static/vendor/leaflet-1.9.4/leaflet.css" }));
    mapsLoading = add("/static/vendor/leaflet-1.9.4/leaflet.js")
      .then(() => add(document.querySelector('meta[name="map-js"]').content)).then(() => window.TorobMap)
      .catch((e) => { mapsLoading = null; throw e; });
  }
  return mapsLoading;
}
const mapSeen = "IntersectionObserver" in window ? new IntersectionObserver((es) => es.forEach((e) => {
  if (!e.isIntersecting) return;
  mapSeen.unobserve(e.target);
  maps().then((M) => e.target.drawMap(M)).catch(() => { e.target.classList.add("failed"); e.target.textContent = "نقشه باز نشد"; });
}), { rootMargin: "200px" }) : null;
// A small map card: drawn when visible; a tap opens the full map.
function miniMap(opts, label) {
  const node = el("div", { class: "mini-map" });
  node.drawMap = (M) => M.mini(node, opts);
  const box = el("div", { class: "mini-wrap" }, node,
    opts.onOpen ? el("button", { type: "button", class: "mini-open", onclick: opts.onOpen }, icon("pin"), el("span", { text: label || "نقشه‌ی بزرگ" })) : null);
  if (mapSeen) mapSeen.observe(node); else maps().then((M) => node.drawMap(M)).catch(() => {});
  return box;
}
const openProductMap = (pid, title) => maps().then((M) => M.openProduct(pid, title)).catch(() => showError("نقشه باز نشد؛ اینترنت رو چک کن."));
const openHubMap = (h) => maps().then((M) => M.openHub(h)).catch(() => showError("نقشه باز نشد؛ اینترنت رو چک کن."));
function shopPoint(s, rank) {
  const pr = s.price || {};
  return { shop_id: s.shop_id, name: s.name, lat: s.lat, lon: s.lon, price: pr.min, rank, open: s.visit?.open_at_arrival,
           building_gid: s.building_gid || null, building: s.building || null };
}

// --- generative UI: the model picks the blocks, the server fills every value from the cards on screen -----------------
const genTag = () => el("span", { class: "gen-tag" }, icon("spark"), el("span", { text: "چیده‌شده با هوش مصنوعی" }));
function genCard(cls, title, ...kids) {
  return el("div", { class: `card gcard ${cls}` }, genTag(), el("div", { class: "gen-title", text: title }), ...kids);
}
function genBlock(b) {
  if (b.type === "compare") {
    // A grid, not a table: each row's label gets its own line and the values sit in equal columns, so long spec
    // names never push the page sideways on a phone.
    const best = b.cols.findIndex((c) => c.best);
    const grid = el("div", { class: "gcmp", role: "table", style: `--n:${b.cols.length}` },
      b.cols.map((c, i) => el("div", { class: "gh" + (i === best ? " best" : ""), role: "columnheader" },
        el("div", { class: "ct", text: c.title, title: c.full || c.title }),
        i === best ? el("span", { class: "best-tag" }, icon("check"), "بهترین برای تو") : el("div", { class: "cs", text: c.sub || "" }))),
      b.rows.flatMap((r) => [el("div", { class: "gl", role: "rowheader", text: r.label }),
        ...r.values.map((v, i) => el("div", { class: "gv" + (i === best ? " best" : ""), role: "cell" }, el("bdi", { text: v })))]));
    return genCard("gcompare", b.of === "products" ? "مقایسه‌ی مدل‌ها" : "مقایسه‌ی فروشنده‌ها", grid);
  }
  if (b.type === "bars") {
    const max = Math.max(...b.items.map((i) => i.value)) || 1;
    return genCard("gbars", b.label, el("div", { class: "bars" }, b.items.map((i) => el("div", { class: "brow" + (i.best ? " best" : "") },
      el("span", { class: "bname", text: i.name, title: i.name }),
      el("span", { class: "btrack" }, el("span", { class: "bfill", style: `inline-size:${Math.max(4, (i.value / max) * 100)}%` })),
      el("bdi", { class: "bval", text: i.text })))));
  }
  if (b.type === "pick") {
    return el("div", { class: "card gcard gpick" }, genTag(),
      el("div", { class: "pick-t", text: shortTitle(b.title) }),
      b.sub ? el("div", { class: "meta" }, el("bdi", { text: b.sub })) : null,
      b.reason ? el("div", { class: "pick-r", text: b.reason }) : null,
      b.chip ? chipButton(b.chip, "btn " + (b.kind === "shop" ? "red" : "blue"), null, b.kind === "shop" ? "phone" : "store") : null);
  }
  if (b.type === "checklist") {
    return genCard("gcheck", b.title, el("ul", { class: "checks" }, b.items.map((t) => el("li", {}, icon("checkc"), el("span", { text: t })))));
  }
  if (b.type === "actions") {
    return el("div", { class: "chips gen-actions" }, b.chips.map((c) => chipButton(c)));
  }
  if (b.type === "map") {
    const pts = b.points.map((p) => ({ ...p, pin: p.label, open: "unknown" }));
    const prod = shownProduct;
    return genCard("gmap", "روی نقشه", miniMap({ center: cond?.center, points: pts,
      onOpen: prod ? () => openProductMap(prod.product_id, shortTitle(prod.title)) : null }, "همه‌ی فروشنده‌ها روی نقشه"));
  }
  return null;
}

// --- products (step 1) -----------------------------------------------------------------------------------------------

function money(n) { return fa(Number(n).toLocaleString("en")).replace(/,/g, "٬"); }
function shortMoney(n) {   // «۳٫۴۳ میلیون»: for the small lines where a full price would wrap
  const t = (x) => fa(String(Number(x.toFixed(2)))).replace(".", "٫");
  return n >= 1e9 ? `${t(n / 1e9)} میلیارد` : n >= 1e6 ? `${t(n / 1e6)} میلیون` : n >= 1e3 ? `${t(n / 1e3)} هزار` : money(n);
}
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
    el("span", {}, `${fa(items.length)} مدل`, items.length > 1 ? el("span", { class: "hint", text: "، بکش برای بقیه" }) : null)));
  if (d.hub) block.append(el("button", { type: "button", class: "hub-line", onclick: () => openHubMap(d.hub) },
    icon("pin"), el("span", { text: d.hub.text }), el("span", { class: "hub-cta", text: "روی نقشه" })));
  const row = el("div", { class: "row" });
  const liveLines = [];
  for (const p of items) {
    const liveLine = el("div", { class: "live-line", hidden: true });
    liveLines.push([p.product_id, liveLine]);
    const scope = p.price_scope === "area" ? "نزدیکت" : "در تهران";
    const sellers = p.price_scope === "area" ? p.sellers_in_area : p.sellers_total;
    row.append(el("article", { class: "card pcard" }, productImage(p),
      el("div", { class: "pbody" },
        el("div", { class: "title", text: p.title, title: p.title }),
        p.price_min ? el("div", { class: "price-big" }, el("small", { text: "از " }), el("bdi", { text: money(p.price_min) }), el("small", { text: " تومان" }))
                    : el("div", { class: "price-big" }, el("small", { text: "قیمت ثبت نشده" })),
        el("div", { class: "meta", title: p.why }, "ثبت‌شده" + (sellers ? ` · ${fa(sellers)} فروشنده ${scope}` : ` ${scope}`)),
        liveLine,
        p.over_budget_pct ? el("div", { class: "over", text: `${fa(p.over_budget_pct)}٪ بالاتر از بودجه` }) : null,
        p.copy_suspect ? el("div", { class: "over", text: "احتمالاً طرح یا کپیه (حدس هوش مصنوعی)" }) : null,
        p.button ? chipButton(p.button, "btn blue", `فروشنده‌های «${shortTitle(p.title)}»`, "store") : null)));
  }
  const wrap = el("div", { class: "row-wrap" }, row);
  block.append(wrap);
  edgeFades(row, wrap);
  if (d.compare_rows?.length) {
    const t = el("table", { class: "compare" },
      el("tr", {}, el("th", { text: "" }), items.map((p) => el("th", { text: p.role_label }))),
      d.compare_rows.map((r) => el("tr", {}, el("td", { text: r.key }), r.values.map((v) => el("td", { text: v })))));
    block.append(el("details", { class: "compare-wrap" }, el("summary", { text: "مقایسه‌ی مشخصات" }), t));
  }
  liveProducts(liveLines);
  return block;
}

// --- live prices from torob.com: asked after the cards appear, cached and rate-limited on the server -----------
// (torob/live.py). Always labelled as Torob's live price, apart from the dataset's registered price.
const liveDot = () => el("span", { class: "live-dot", "aria-hidden": "true" });
async function liveProducts(lines) {
  for (const [id, line] of lines) {   // one at a time: the server spaces requests to Torob anyway
    try {
      const r = await (await fetch(`/api/live/product/${encodeURIComponent(id)}`)).json();
      if (r.status !== "ok") continue;
      line.replaceChildren(liveDot(), el("span", { text: `ترب امروز: ${shortMoney(r.price_min)}` }));
      line.hidden = false;
    } catch (_) {}
  }
}
async function liveShops(productId, ids, block) {
  if (!productId || !ids.length) return;
  try {
    const r = await (await fetch(`/api/live/shops/${encodeURIComponent(productId)}?ids=${ids.map(encodeURIComponent).join(",")}`)).json();
    if (r.status !== "ok") return;
    const sum = $(".live-summary", block);
    if (sum && r.tehran?.count) {
      sum.replaceChildren(liveDot(), el("span", { text: `الان در ترب: ${fa(r.tehran.count)} فروشنده‌ی حضوری در تهران، از ${money(r.tehran.min)} تومان` }));
      sum.hidden = false;
    }
    for (const [sid, v] of Object.entries(r.by_shop || {})) {
      const line = block.querySelector(`[data-live-shop="${CSS.escape(sid)}"]`);
      if (!line) continue;
      line.replaceChildren(liveDot(), el("span", { text: v.price ? `ترب الان: ${money(v.price)}` + (v.changed ? ` (${v.changed})` : "")
        : `همین ساختمان در ترب: ${fa(v.spot_count)} فروشنده، از ${money(v.spot_min)}` }));
      line.hidden = false;
    }
  } catch (_) {}
}

// --- shops (steps 2–3) --------------------------------------------------------------------------------------------

function line(ic, text, cls) {
  return el("div", { class: "ln" + (cls ? " " + cls : "") }, icon(ic), el("span", { class: cls?.startsWith("open-") ? "st" : null, text }));
}

// The 112 shops with a real name in the dataset are the ones with an Instagram page or a website: link to them.
const safeUrl = (u) => (typeof u === "string" && /^https?:\/\/[^\s]+$/.test(u) ? u : null);
function shopLinks(s) {
  const links = [[safeUrl(s.instagram), "اینستاگرام فروشگاه"], [safeUrl(s.website), "سایت فروشگاه"]].filter(([u]) => u);
  if (!links.length) return null;
  return el("div", { class: "shop-links" }, links.map(([href, text]) =>
    el("a", { href, target: "_blank", rel: "noopener noreferrer nofollow", text })));
}

// A seller in four short rows, like a row on Torob's seller list: name and price · where and how old the price is ·
// open when you'd get there and how far · small tags. The registered price, its age and «stock not confirmed» stay
// three separate facts (brief).
function shopCard(s, compact = false) {
  const st = s.visit.open_at_arrival;
  const open = st === "yes" || st === "tight" ? "open-yes" : st === "no" ? "open-no" : "";
  const pr = s.price || {};
  const labels = (s.reasons || []).slice(0, (s.reasons || []).length - (s.same_building_others ? 1 : 0));
  const card = el("article", { class: "card scard" + (compact ? " compact" : ""), "data-shop-id": s.shop_id },
    el("div", { class: "s1" }, el("div", { class: "name", text: s.name }),
      el("div", { class: "amt" }, pr.min ? [pr.max && pr.max !== pr.min ? el("small", { text: "از " }) : null,
        el("bdi", { text: money(pr.min) }), el("small", { text: pr.per_metre ? " تومان، متری" : " تومان" })] : el("small", { text: pr.text }))),
    el("div", { class: "s2" }, el("span", { class: "where", text: s.where, title: s.where }),
      el("bdi", { class: "age" + (pr.old ? " old" : ""), text: pr.age ? `قیمت ${pr.age}` : "" })),
    el("div", { class: `s3 ${open}` }, el("i", { class: "dot" }), el("span", { text: s.visit.text })),
    el("div", { class: "live-line", hidden: true, "data-live-shop": s.shop_id }));
  if (!compact) {
    card.append(el("div", { class: "tags" },
      labels.map((r) => el("span", { class: "tag" + (r === "قسطی" ? " green" : r === "ارزان‌ترین" ? " red" : "") }, r)),
      s.same_building_others ? el("span", { class: "tag", text: `+${fa(s.same_building_others)} تو همین پاساژ` }) : null,
      el("span", { class: "tag stock", text: s.stock_label })));
    const links = shopLinks(s);
    if (links) card.append(links);
  }
  if (!compact) {
    const det = s.details;
    card.append(el("details", { class: "sdet" }, el("summary", { text: "جزئیات" }),
      el("div", { class: "meta", text: det.payments.join("، ") }),
      el("div", { class: "meta", text: det.warranty }),
      el("div", { class: "meta", text: det.fast_delivery }),
      el("div", { class: "meta", text: "عنوان فروشنده: " + det.seller_title }),
      det.seller_note ? el("div", { class: "meta note-seller", text: "توضیح فروشنده: " + det.seller_note }) : null));
  }
  card.append(chipButton(s.button, compact ? "btn line" : "btn red", `زنگ به «${s.name}»`, "phone"));
  return card;
}

function shopBlock(d) {
  const block = el("section");
  if (d.product) block.append(el("div", { class: "block-h" }, el("h3", { text: "فروشنده‌ها" }),
    d.items?.length ? el("span", { text: `${fa(d.items.length + (d.more_count || 0))} فروشگاه` }) : null),
    el("div", { class: "block-sub", text: shortTitle(d.product.title), title: d.product.title }));
  block.append(el("div", { class: "live-line live-summary", hidden: true }));
  if (d.product && (d.items?.length || d.more?.length)) {
    const pts = [...(d.items || []).map((s, i) => shopPoint(s, i + 1)), ...(d.more || []).slice(0, 12).map((s) => shopPoint(s))]
      .filter((p) => p.lat != null);
    block.append(miniMap({ center: cond?.center, radius_km: cond?.radius_km, points: pts,
      onOpen: () => openProductMap(d.product.product_id, shortTitle(d.product.title)) }, "همه‌ی فروشنده‌ها روی نقشه"));
  }
  if (cond && !cond.located) block.append(locationAsk());
  (d.notes || []).forEach((n) => block.append(el("div", { class: "note", text: n })));
  if (d.relaxed?.chip) block.append(el("div", { class: "chips start-align" }, chipButton(d.relaxed.chip)));
  (d.items || []).forEach((s) => block.append(shopCard(s)));
  liveShops(d.product?.product_id, [...(d.items || []), ...(d.more || [])].map((s) => s.shop_id).slice(0, 30), block);
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
  ($(".tags", card) || $(".s3", card)).after(box);
  const stock = $(".stock", card);   // «موجودی: تأیید نشده» is now answered, by the simulated call above it
  if (stock) stock.hidden = true;
  const btn = card.querySelector(":scope > .btn");   // a shop that didn't have it is no longer the obvious next call
  if (btn && d.stock !== "simulated_confirmed") btn.className = "btn line";
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
      p.lat != null ? miniMap({ center: cond?.center, route: true,
        points: [{ shop_id: p.shop_id, name: p.name, lat: p.lat, lon: p.lon, pin: p.name, rank: null, open: p.open_at_arrival }],
        onOpen: () => openProductMap(p.product_id, shortTitle(p.product_title)) }, "روی نقشه") : null,
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
let lastBudgetSteps = null;   // the steps suggested for the current search (the budget question's buttons)
// «۲٫۵» or «۲ تومن» = 2.5 / 2 million, «۸۰۰ هزار» or «۵۰۰ تومن» = thousands (Tehran slang), «۲۵۰۰۰۰۰» = toman as written
function parseBudget(text) {
  const t = toLatin(text).replace(/[,٬\s]/g, "");
  const n = parseFloat(t.replace(/[^\d.]/g, ""));
  if (!(n > 0)) return null;
  if (/میلیون|م$/.test(t)) return Math.round(n * 1e6);
  if (/هزار|k/i.test(t)) return Math.round(n * 1e3);
  if (n >= 10000) return Math.round(n);
  return n >= 50 ? Math.round(n * 1e3) : Math.round(n * 1e6);
}
const PAYMENTS = [["any", "همه"], ["credit", "قسطی"], ["credit_in_person", "قسطی حضوری"]];
const OPENS = [["at_arrival", "باز تا وقتی برسم"], ["now", "باز الان"], ["any", "مهم نیست"]];

function km(r) { return r == null ? "کل تهران" : r < 1 ? `${fa(Math.round(r * 1000))} متر` : `${fa(r)} کیلومتر`; }

// Sideways rows never end in a hard crop: the edge that still has more to scroll to gets a soft blur/fade
// (CSS: .more-left / .more-right on `host`). RTL: scrollLeft is 0 at the start (right) and negative to the left.
function edgeFades(scroller, host = scroller) {
  const update = () => {
    const max = scroller.scrollWidth - scroller.clientWidth, pos = Math.abs(scroller.scrollLeft);
    host.classList.toggle("more-right", pos > 2);
    host.classList.toggle("more-left", max - pos > 2);
  };
  scroller.addEventListener("scroll", update, { passive: true });
  if ("ResizeObserver" in window) new ResizeObserver(update).observe(scroller);
  scroller._fade = update;   // call after the contents change
  requestAnimationFrame(update);
  return update;
}

function setConditions(d) {
  cond = d;
  $("#loc-label").textContent = (d.area || "تهران") + (d.located ? "" : " (پیش‌فرض؛ بزن تا عوض کنی)");
  if (hasResults) strip.hidden = false;      // R1: the strip appears with the first results, not before
  const set = (key, label, on) => { const b = $(`[data-cond="${key}"]`, strip); b.textContent = label; b.classList.toggle("on", on); };
  set("loc", d.located ? (d.area || "مکان من") : "مکانت کجاست؟", !!d.located);   // stays reachable when the row above hides
  const pay = (PAYMENTS.find((p) => p[0] === d.payment) || PAYMENTS[0])[1];
  set("radius", km(d.radius_km), d.radius_km !== 3);
  set("budget", d.budget_label || "بودجه: فرقی نداره", !!d.budget_max);
  set("payment", (d.payment === "any" ? "پرداخت: همه" : pay) + (d.fast_delivery ? "، ارسال فوری" : ""),
      d.payment !== "any" || d.fast_delivery);
  set("open", (OPENS.find((o) => o[0] === d.open) || OPENS[0])[1], d.open !== "at_arrival");
  set("arrive", d.arrive_label || (d.arrive_at ? `رسیدن ${fa(d.arrive_at)}` : "رسیدن: الان"), !!d.arrive_at);
  strip._fade?.();
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
  loc: () => openLocation(),
  radius: () => openSheet("محدوده", () => opts(RADII, cond ? cond.radius_km : 3, (v) => refine({ radius_km: v }))),
  budget: () => openSheet("بودجه", () => {
    const input = el("input", { type: "text", inputmode: "decimal", placeholder: "مبلغ دلخواه، مثلاً ۲٫۵ میلیون یا ۸۰۰ هزار" });
    const err = el("div", { class: "error" });
    const apply = () => {
      const v = parseBudget(input.value);
      if (!v || v < 1000) { err.textContent = "یه مبلغ بنویس، مثلاً ۲٫۵ میلیون یا ۸۰۰ هزار"; return; }
      sheet.close(); refine({ budget_max: v });
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } });
    const steps = lastBudgetSteps?.length ? [...lastBudgetSteps, [null, "فرقی نداره"]] : BUDGETS;
    setTimeout(() => input.focus(), 50);
    return el("div", {}, opts(steps, cond ? cond.budget_max : null, (v) => refine({ budget_max: v })),
      el("div", { style: "margin-top:8px" }, input, err,
        el("button", { type: "button", class: "btn blue", text: "اعمال", onclick: apply })));
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

function useGps() {
  if (busy) return;
  if (!navigator.geolocation) return showError("مرورگرت مکان رو نمی‌ده؛ یه محله انتخاب کن.");
  navigator.geolocation.getCurrentPosition((p) => {
    const { latitude: lat, longitude: lng } = p.coords;
    if (lat < 35.5 || lat > 35.9 || lng < 51 || lng > 51.8) return showError("بیرون از تهرانی؛ داده‌ی ترب فقط تهران رو داره. یه محله انتخاب کن.");
    pickLocation({ lat, lng, label: "مکان من", source: "gps" }, "مکان من");
  }, () => showError("اجازه‌ی مکان داده نشد؛ یه محله انتخاب کن."), { timeout: 8000 });
}
const pickOnMap = () => { if (!busy) maps().then((M) => M.pick()).catch(() => showError("نقشه باز نشد؛ اینترنت رو چک کن.")); };

// Distances are measured from the city-centre default until the shopper says where they are: say so, and ask.
function locationAsk() {
  return el("div", { class: "loc-ask" },
    el("div", { text: `فاصله‌ها از «${cond.area}» حساب شده، چون هنوز نمی‌دونم کجایی.` }),
    el("div", { class: "chips" },
      el("button", { type: "button", class: "chip", onclick: useGps }, icon("pin"), el("span", { text: "مکان من" })),
      el("button", { type: "button", class: "chip", text: "روی نقشه", onclick: pickOnMap }),
      el("button", { type: "button", class: "chip", text: "یه محله", onclick: openLocation })));
}

async function openLocation() {
  if (busy || sheet.open) return;
  let areas = [];
  try { const r = await (await fetch("/api/areas")).json(); if (Array.isArray(r)) areas = r; } catch (_) {}
  openSheet("کجایی؟", () => el("div", { class: "opts" },
    el("button", { type: "button", class: "opt plain", text: "مکان من (GPS)", onclick: () => { sheet.close(); useGps(); } }),
    el("button", { type: "button", class: "opt plain", text: "روی نقشه انتخاب کن", onclick: () => { sheet.close(); pickOnMap(); } }),
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
  if (say(t.value, photo)) { t.value = ""; fitText(); photo = null; renderPhoto(); }   // keep both if it couldn't be sent (busy)
});
// the message box grows with its lines; Enter sends, Shift+Enter or Ctrl+Enter starts a new line
const fitText = () => { const t = $("#text"); t.style.height = "auto"; t.style.height = `${t.scrollHeight}px`; };
$("#text").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  if (e.ctrlKey || e.metaKey) {                     // a textarea ignores Ctrl+Enter on its own
    e.preventDefault();
    e.target.setRangeText("\n", e.target.selectionStart, e.target.selectionEnd, "end");
    e.target.dispatchEvent(new Event("input"));
  } else if (!e.shiftKey) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#text").addEventListener("input", (e) => { fitText(); syncSend(); if (!busy) Radish.set(e.target.value.trim() ? "listen" : "idle"); });
$("#attach").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", (e) => { attachPhoto(e.target.files[0]); e.target.value = ""; });
$("#photo button").addEventListener("click", () => { photo = null; renderPhoto(); });
$(".field").addEventListener("click", (e) => { if (e.target === e.currentTarget) $("#text").focus(); });
$("#text").addEventListener("paste", (e) => {   // a copied screenshot or photo pastes straight in (PC)
  const f = [...(e.clipboardData?.files || [])].find((x) => x.type.startsWith("image/"));
  if (f) { e.preventDefault(); attachPhoto(f); }
});
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragover", (e) => { if (hasFiles(e)) { e.preventDefault(); $("#composer").classList.add("drag"); } });
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) $("#composer").classList.remove("drag"); });
document.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  $("#composer").classList.remove("drag");
  attachPhoto([...e.dataTransfer.files].find((x) => x.type.startsWith("image/")));
});
document.querySelectorAll("canvas.radish").forEach((c) => Radish.attach(c));
renderSuggestions();
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
  syncDock();
}, { passive: true });

// A reload rebuilds this session's chat from the server's log, with the same handlers as a live turn.
async function restoreHistory() {
  let evs = [];
  try { evs = (await (await fetch("/api/history")).json()).events || []; } catch (_) {}
  if (!evs.length) return false;
  leaveStart();
  replaying = true;
  stream.classList.add("replay");             // a restored chat appears as it was, without replaying entrances
  turn = { status: null, sawDone: false, block: null, failed: false };
  for (const { event, data } of evs) {
    if (event === "user") turn = { status: null, sawDone: false, block: null, failed: false };
    try { (handlers[event] || (() => {}))(data); } catch (e) { console.error(event, e); }
    turn.status?.classList.remove("busy");
  }
  replaying = false;
  for (const n of stream.children) n.classList.add("old");
  stream.classList.remove("replay");
  turn = null;
  window.scrollTo({ top: document.body.scrollHeight });
  return true;
}

strip.classList.add("fade-x");
edgeFades(strip);

(async () => {
  try {
    const s = await (await fetch("/api/state")).json();
    hasResults = !!s.has_results;
    const restored = await restoreHistory();   // any saved chat, even one that has no results yet
    setConditions(s.conditions);
    if (s.plan) renderPlan(s.plan, true);     // a reload keeps the shopping plan
    if (hasResults && !restored) $("#start .resume")?.removeAttribute("hidden");
  } catch (_) {}
  measureSticky();
})();
