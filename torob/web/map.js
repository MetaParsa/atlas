// Maps for the shopping assistant: Leaflet over OpenStreetMap tiles, served through this app (/tiles/…) so
// a shopper's browser only ever talks to us. Loaded on demand by app.js (maps()); uses its helpers (el, icon, fa,
// money, chip, isPhone, showError). Every number on a pin or panel comes from the server's facts, same as the cards.
(() => {
  const TEHRAN_BOUNDS = [[35.45, 50.95], [35.95, 51.85]];
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const OPEN = { yes: "open", tight: "open", no: "closed", unknown: "unknown" };
  const inTehran = (lat, lng) => lat >= 35.5 && lat <= 35.9 && lng >= 51 && lng <= 51.8;

  function shortPrice(n) {
    if (!n) return "؟";
    const t = (x) => fa(String(Number(x.toFixed(2)))).replace(".", "٫");
    return n >= 1e9 ? `${t(n / 1e9)} میلیارد` : n >= 1e6 ? `${t(n / 1e6)} میلیون` : n >= 1e3 ? `${t(n / 1e3)} هزار` : fa(n);
  }
  // --- one Leaflet map (interactive in the full view, still in the chat) --------------------------------------------
  function makeMap(node, interactive) {
    node.setAttribute("dir", "ltr");   // Leaflet lays out its panes left-to-right; our overlays stay RTL
    const m = L.map(node, {
      zoomControl: interactive, attributionControl: true, dragging: interactive, touchZoom: interactive,
      scrollWheelZoom: interactive, doubleClickZoom: interactive, boxZoom: false, keyboard: interactive, tap: false,
      maxBounds: [[35.3, 50.7], [36.1, 52.1]], minZoom: 10, maxZoom: 19, zoomSnap: 0.5 });
    m.attributionControl.setPrefix(false);
    L.tileLayer("/tiles/osm/{z}/{x}/{y}.png", {   // softened (and inverted in dark mode) by app.css
      minZoom: 10, maxZoom: 19, bounds: TEHRAN_BOUNDS, attribution: "© OpenStreetMap contributors" }).addTo(m);
    if (interactive) m.zoomControl.setPosition("topleft");
    return m;
  }
  const divIcon = (node) => L.divIcon({ className: "tm-icon", html: node, iconSize: null });
  const meMarker = (c) => L.marker([c.lat, c.lng], { icon: divIcon(el("div", { class: "tm-me" })), interactive: false, keyboard: false, zIndexOffset: -100 });
  const radius = (c, km) => L.circle([c.lat, c.lng], { radius: km * 1000, className: "tm-radius", interactive: false });
  function pricePin(p) {
    return el("div", { class: `tm-pin ${OPEN[p.open] || "unknown"}${p.rank ? " ranked" : ""}` },
      p.rank ? el("b", { text: fa(p.rank) }) : null, el("span", { text: p.pin || shortPrice(p.price) }));
  }
  function groupPin(g) {
    const open = g.items.some((p) => OPEN[p.open] === "open") ? "open" : g.items.every((p) => p.open === "no") ? "closed" : "unknown";
    const ranked = g.items.find((p) => p.rank);
    return el("div", { class: `tm-pin group ${open}${ranked ? " ranked" : ""}` }, ranked ? el("b", { text: fa(ranked.rank) }) : icon("store"),
      el("span", { text: `${g.name} · ${fa(g.items.length)}` }));
  }
  function hubLabel(b, i) {
    return el("div", { class: "tm-hub" + (i === 0 ? " top" : "") }, i === 0 ? el("b", { text: "بورس" }) : null,
      el("span", { text: `${b.name} · ${fa(b.shops)}` }));
  }
  function addHubs(map, layer, hubs, onClick) {
    const max = Math.max(...hubs.map((b) => b.shops), 1);
    const labels = hubs.map((b, i) => {
      L.circle([b.lat, b.lon], { radius: 70 + 160 * Math.sqrt(b.shops / max), className: "tm-hubzone" + (i === 0 ? " top" : ""),
        interactive: false }).addTo(layer);
      const mk = L.marker([b.lat, b.lon], { icon: divIcon(hubLabel(b, i)), zIndexOffset: 500 - i }).addTo(layer);
      if (onClick) mk.on("click", () => onClick(b));
      return mk;
    });
    // Hubs are often a few doors apart (Aladdin, Charsou, Bozorgmehr): stack labels that would cover each other.
    const declutter = () => {
      const placed = [];
      for (const mk of labels) {
        const node = mk.getElement()?.firstChild;
        if (!node) continue;
        const p = map.latLngToContainerPoint(mk.getLatLng());
        const w = node.offsetWidth || 90, h = 26;
        let dy = 0;
        while (placed.some((q) => Math.abs(q.x - p.x) < (q.w + w) / 2 && Math.abs(q.y - (p.y + dy)) < h)) dy += h;
        node.style.setProperty("--dy", `${dy}px`);
        placed.push({ x: p.x, y: p.y + dy, w });
      }
    };
    map.on("zoomend", declutter);
    setTimeout(declutter, 0);   // after the caller fits the view
  }
  // Shops in one passage share coordinates: one pin with a count, opened as a list (floor, price, open).
  function groupByBuilding(points) {
    const groups = new Map(), singles = [];
    for (const p of points) {
      if (!p.building_gid) { singles.push(p); continue; }
      if (!groups.has(p.building_gid)) groups.set(p.building_gid, []);
      groups.get(p.building_gid).push(p);
    }
    const out = [];
    for (const items of groups.values()) {
      if (items.length === 1) { singles.push(items[0]); continue; }
      out.push({ name: (items[0].building || "پاساژ").replace(/^(پاساژ|مجتمع تجاری|مجتمع|ساختمان|بازار) /, ""), building: items[0].building,
        lat: items.reduce((s, p) => s + p.lat, 0) / items.length, lon: items.reduce((s, p) => s + p.lon, 0) / items.length,
        items: items.sort((a, b) => (a.price || 9e15) - (b.price || 9e15)) });
    }
    return { groups: out, singles };
  }
  function fit(m, latlngs, maxZoom = 17) {
    if (!latlngs.length) return m.setView([35.6935, 51.415], 14);
    if (latlngs.length === 1) return m.setView(latlngs[0], 16);
    // In the full view, keep the shops clear of the info panel: on the right on computers, at the bottom on phones.
    const size = m.getSize(), full = V && m === V.map;
    const pad = !full ? [0, 0] : size.x >= 700 ? [400, 20] : [20, Math.round(size.y * 0.42)];
    m.fitBounds(L.latLngBounds(latlngs).pad(0.1), { maxZoom, paddingTopLeft: [20, 20], paddingBottomRight: pad });
  }

  // --- the full-screen view ------------------------------------------------------------------------------------------
  let V = null;
  function view() {
    if (V) return V;
    V = { mapNode: el("div", { class: "mv-map" }), title: el("strong"), sub: el("span"),
          filters: el("div", { class: "mv-filters", role: "toolbar", "aria-label": "فیلترهای نقشه" }),
          panel: el("div", { class: "mv-panel", hidden: true }), tools: el("div", { class: "mv-tools" }),
          legend: el("div", { class: "mv-legend" }) };
    V.dlg = el("dialog", { class: "mapview", "aria-label": "نقشه" },
      el("div", { class: "mv-top" },
        el("div", { class: "mv-title" }, V.title, V.sub),
        el("button", { type: "button", class: "mv-close", "aria-label": "بستن نقشه", onclick: () => V.dlg.close() }, "بستن")),
      V.filters,
      el("div", { class: "mv-body" }, V.mapNode, V.tools, V.legend, V.panel));
    V.dlg.addEventListener("close", () => { V.map?.remove(); V.map = null; });
    V.filters.classList.add("fade-x");
    edgeFades(V.filters);
    document.body.append(V.dlg);
    return V;
  }
  function openView(title, sub) {
    const v = view();
    v.title.textContent = title; v.sub.textContent = sub || "";
    v.filters.replaceChildren(); v.tools.replaceChildren(); v.legend.replaceChildren(); hidePanel();
    if (!v.dlg.open) v.dlg.showModal();
    v.map?.remove();
    v.map = makeMap(v.mapNode, true);
    v.layer = L.layerGroup().addTo(v.map);
    v.route = L.layerGroup().addTo(v.map);
    return v;
  }
  function hidePanel() { if (V) { V.panel.hidden = true; delete V.panel.dataset.rough; V.panel.replaceChildren(); V.route?.clearLayers(); } }
  function showPanel(...kids) {
    delete V.panel.dataset.rough;
    V.panel.replaceChildren(el("button", { type: "button", class: "mv-x", "aria-label": "بستن", onclick: hidePanel }, "×"), ...kids);
    V.panel.hidden = false;
    V.panel.scrollTop = 0;
  }
  // Leave the map and do something in the chat (call, search here): the chat shows what happened.
  function act(c, bubble) { V.dlg.close(); chip(c, bubble); }
  function filterChip(label, on, toggle) {
    const b = el("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": String(on), text: label,
      onclick: () => { const now = toggle(); b.classList.toggle("on", now); b.setAttribute("aria-pressed", String(now)); } });
    return b;
  }
  function toolButton(ic, label, onclick) {
    return el("button", { type: "button", class: "mv-tool", "aria-label": label, title: label, onclick }, icon(ic), el("span", { text: label }));
  }
  function legendFor(kind) {
    const dot = (cls, t) => el("span", {}, el("i", { class: cls }), t);
    V.legend.replaceChildren(...(kind === "hub"
      ? [dot("lg-dot", "یک فروشنده"), dot("lg-hub", "بورس (بیشترین فروشنده)")]
      : [dot("lg-open", "تا برسی باز"), dot("lg-closed", "بسته"), dot("lg-unknown", "ساعت نامعلوم"), dot("lg-rank", "رتبه در فهرست")]));
  }
  // «مکان من». With `onMoved` (the sellers map) the map stays open: the location is set in the background (the chat
  // behind updates too) and the map redraws from where you are. Errors show in the map, not behind it.
  function locateMe(onMoved) {
    const say = (t) => (onMoved ? showPanel(el("div", { class: "mv-name", text: t })) : showError(t));
    getFix(async ({ lat, lng, acc }) => {
      if (!inTehran(lat, lng)) return say("بیرون از تهرانی؛ داده‌ی ترب فقط تهران رو داره.");
      const c = { label: "مکان من", kind: "direct_call", tool: "set_location", args: { lat, lng, label: "مکان من", source: "gps" } };
      if (!onMoved) return act(c, "مکان من");
      showPanel(el("div", { class: "mv-name", text: "دارم فاصله‌ها رو از جایی که هستی حساب می‌کنم…" }));
      await chip(c, "مکان من");
      await onMoved();
      if (acc > ROUGH_M) showPanel(el("div", { class: "mv-name", text: roughNote(acc) }),
        el("div", { class: "meta", text: "روی جای درستت روی نقشه بزن و «همین‌جا» رو انتخاب کن." }));
      if (acc > ROUGH_M) V.panel.dataset.rough = "1";   // the next tap on the map is the correction, not «close»
    }, (t) => say(t));
  }
  // Tap on an empty spot: offer to search from there.
  function offerHere(e) {
    const { lat, lng } = e.latlng;
    if (!inTehran(lat, lng)) return;
    V.route.clearLayers();
    L.marker([lat, lng], { icon: divIcon(el("div", { class: "tm-drop" })), interactive: false }).addTo(V.route);
    showPanel(el("div", { class: "mv-name", text: "از همین نقطه بگردم؟" }),
      el("div", { class: "meta", text: "محدوده‌ی جست‌وجو می‌شه اینجا و نتیجه‌ها دوباره مرتب می‌شن." }),
      el("div", { class: "mv-actions" }, el("button", { type: "button", class: "btn blue", onclick: () =>
        act({ label: "نقطه‌ی روی نقشه", kind: "direct_call", tool: "set_location",
              args: { lat: +lat.toFixed(6), lng: +lng.toFixed(6), label: "نقطه‌ی روی نقشه", source: "area" } }, "از این نقطه روی نقشه بگرد") },
        icon("pin"), el("span", { text: "همین‌جا" }))));
  }

  // --- product: every seller of the selected model -------------------------------------------------------------------
  async function openProduct(productId, title) {
    const v = openView(`فروشنده‌های «${title}»`, "در حال بارگذاری…");
    let d;
    const load = async () => {
      const r = await fetch(`/api/map/product/${encodeURIComponent(productId)}`);
      const x = await r.json();
      if (!r.ok) throw new Error(x.detail);
      return x;
    };
    try { d = await load(); } catch (e) { v.sub.textContent = (e && e.message) || "نقشه باز نشد؛ دوباره امتحان کن."; return; }
    if (!V.map) return;
    const st = { open: false, credit: false, near: false, hubs: false };   // the bourse layer crowds the pins it sits on: one tap away
    let c = d.center;
    const draw = (refit) => {
      v.layer.clearLayers(); hidePanel();
      meMarker(c).addTo(v.layer);
      if (d.radius_km) radius(c, d.radius_km).addTo(v.layer);
      if (st.hubs && d.category?.buildings?.length) addHubs(v.map, v.layer, d.category.buildings);
      const pts = d.points.filter((p) => (!st.open || OPEN[p.open] === "open") && (!st.credit || p.credit) && (!st.near || p.in_radius));
      const { groups, singles } = groupByBuilding(pts);
      singles.forEach((p) => L.marker([p.lat, p.lon], { icon: divIcon(pricePin(p)), zIndexOffset: p.rank ? 1000 - p.rank : 0,
        title: p.name, alt: p.name }).on("click", () => shopPanel(p, d)).addTo(v.layer));
      groups.forEach((g) => L.marker([g.lat, g.lon], { icon: divIcon(groupPin(g)), zIndexOffset: 800, title: g.building, alt: g.building })
        .on("click", () => groupPanel(g, d)).addTo(v.layer));
      const inR = pts.filter((p) => p.in_radius).length;
      v.sub.textContent = `${fa(pts.length)} از ${fa(d.points.length)} فروشنده‌ی حضوری` + (d.radius_km ? ` · ${fa(inR)} تا داخل محدوده` : "");
      if (refit) {
        const near = pts.filter((p) => p.in_radius);
        fit(v.map, [[c.lat, c.lng], ...(near.length ? near : pts).map((p) => [p.lat, p.lon])]);
      }
      if (!pts.length) showPanel(el("div", { class: "mv-name", text: "با این فیلترها فروشنده‌ای نموند" }),
        el("div", { class: "meta", text: "یکی از فیلترهای بالا رو بردار." }));
    };
    v.filters.append(
      filterChip("تا برسم باز", st.open, () => (st.open = !st.open, draw(false), st.open)),
      filterChip("قسطی", st.credit, () => (st.credit = !st.credit, draw(false), st.credit)),
      filterChip("داخل محدوده", st.near, () => (st.near = !st.near, draw(true), st.near)),
      filterChip("بورس این دسته", st.hubs, () => (st.hubs = !st.hubs, draw(false), st.hubs)));
    v.filters._fade?.();
    const moved = async () => {   // same sellers, measured from the new place: centre, radius, open-at-arrival, pins
      try { d = await load(); c = d.center; } catch (_) { return; }
      if (V.map === v.map) draw(true);
    };
    v.tools.append(toolButton("pin", "مکان من", () => locateMe(moved)),
      toolButton("list", "همه", () => fit(v.map, [[c.lat, c.lng], ...d.points.map((p) => [p.lat, p.lon])])));
    legendFor("product");
    v.map.on("click", (e) => (V.panel.hidden || V.panel.dataset.rough ? offerHere(e) : hidePanel()));
    draw(true);
  }
  function shopPanel(p, d) {
    V.route.clearLayers();
    const c = d.center;
    L.polyline([[c.lat, c.lng], [p.lat, p.lon]], { className: "tm-route", interactive: false }).addTo(V.route);
    const call = { label: "زنگ بزن بپرس", kind: "direct_call", tool: "call_shop", args: { shop_id: p.shop_id, product_id: d.product.product_id } };
    showPanel(
      el("div", { class: "mv-h" }, el("div", { class: "mv-name", text: p.name }),
        p.rank ? el("span", { class: "mv-rank", text: `رتبه‌ی ${fa(p.rank)} در فهرست` }) : null),
      el("div", { class: "meta", text: p.where }),
      el("div", { class: "mv-price" }, el("bdi", { text: p.price_text }), p.price ? el("small", { text: " تومان" }) : null,
        el("small", { class: "meta", text: ` · قیمت ثبت‌شده، ${p.age}` })),
      el("div", { class: `ln ${OPEN[p.open] === "open" ? "open-yes" : p.open === "no" ? "open-no" : ""}` }, icon("clock"), el("span", { text: p.visit })),
      el("div", { class: "ln" }, icon("box"), el("span", { text: "موجودی: تأیید نشده" + (p.credit ? " · قسطی دارد" : "") })),
      el("div", { class: "mv-actions" },
        el("button", { type: "button", class: "btn red", onclick: () => act(call, `زنگ به «${p.name}»`) }, icon("phone"), el("span", { text: "زنگ بزن بپرس" })),
        el("button", { type: "button", class: "btn line", onclick: () => openRoute({ lat: p.lat, lng: p.lon, name: p.name }) }, icon("nav"), el("span", { text: "مسیریابی" }))));
    V.map.panTo([p.lat, p.lon], { animate: true });
  }
  function groupPanel(g, d) {
    V.route.clearLayers();
    showPanel(el("div", { class: "mv-name", text: `${g.building || "پاساژ"} · ${fa(g.items.length)} فروشنده` }),
      el("div", { class: "meta", text: "همه تو یک ساختمان؛ ارزون‌ترها اول. روی هر کدوم بزن." }),
      el("div", { class: "mv-rows" }, g.items.map((p) => el("button", { type: "button", class: "mv-row", onclick: () => shopPanel(p, d) },
        el("i", { class: `st ${OPEN[p.open] || "unknown"}` }),
        el("span", { class: "r-name", text: p.name + (p.rank ? ` (رتبه‌ی ${fa(p.rank)})` : "") }),
        el("span", { class: "r-floor", text: p.floor ? (/^\d|^[۰-۹]/.test(p.floor) ? `طبقه ${fa(p.floor)}` : p.floor) : "" }),
        el("bdi", { class: "r-price", text: p.pin })))));
    V.map.panTo([g.lat, g.lon], { animate: true });
  }

  // --- «بورس»: where a trade gathers --------------------------------------------------------------------------------
  async function openHub(h) {
    const v = openView(`بورس «${h.subject}»`, "در حال بارگذاری…");
    let d;
    try {
      const r = await fetch("/api/map/category?" + h.categories.map((c) => "c=" + encodeURIComponent(c)).join("&"));
      d = await r.json();
      if (!r.ok) throw new Error(d.detail);
    } catch (e) { v.sub.textContent = (e && e.message) || "نقشه باز نشد؛ دوباره امتحان کن."; return; }
    if (!V.map) return;
    const renderer = L.canvas({ padding: 0.3 });
    const dotColor = css("--blue") || "#007aff";
    d.dots.forEach(([lat, lon]) => L.circleMarker([lat, lon], { renderer, radius: 3, stroke: false, fillColor: dotColor, fillOpacity: 0.45,
      interactive: false }).addTo(v.layer));
    if (cond?.center) meMarker(cond.center).addTo(v.layer);
    addHubs(v.map, v.layer, d.buildings, (b) => hubPanel(b, d));
    v.sub.textContent = `${fa(d.total)} فروشنده‌ی حضوری در تهران` + (d.buildings[0] ? ` · بیشترینش ${d.buildings[0].name}` : "");
    legendFor("hub");
    v.tools.append(toolButton("pin", "مکان من", () => locateMe(() => openHub(h))),   // the bourse map redraws around you
      toolButton("list", "همه", () => fit(v.map, d.dots, 15)));
    v.map.on("click", (e) => (V.panel.dataset.rough ? offerHere(e) : hidePanel()));
    fit(v.map, d.buildings.length ? d.buildings.map((b) => [b.lat, b.lon]) : d.dots, 16);
    hubList(d);
  }
  function hubList(d) {
    const max = Math.max(...d.buildings.map((b) => b.shops), 1);
    showPanel(el("div", { class: "mv-name", text: d.buildings.length && !d.spread ? "جاهایی که فروشنده‌هاش جمع‌ان" : "بورس مشخصی نداره" }),
      el("div", { class: "meta", text: "طبق داده‌ی ترب: تعداد فروشنده‌ی حضوری در هر ساختمان." }),
      el("div", { class: "mv-rows" }, d.buildings.map((b) => el("button", { type: "button", class: "mv-row hub", onclick: () => hubPanel(b, d) },
        el("span", { class: "r-name", text: `${b.kind} ${b.name}` }),
        el("span", { class: "r-bar" }, el("span", { style: `inline-size:${Math.round((b.shops / max) * 100)}%` })),
        el("bdi", { class: "r-price", text: `${fa(b.shops)} فروشنده` })))),
      d.areas.length ? el("div", { class: "meta", text: "محله‌ها: " + d.areas.map((a) => `${a.name} (${fa(a.shops)})`).join("، ") }) : null);
  }
  function hubPanel(b, d) {
    V.map.flyTo([b.lat, b.lon], 17, { duration: 0.6 });
    const go = { label: `مدل‌ها تو ${b.name}`, kind: "direct_call", tool: "go_hub", args: { lat: b.lat, lng: b.lon, label: `${b.kind} ${b.name}` } };
    showPanel(el("div", { class: "mv-name", text: `${b.kind} ${b.name}` }),
      el("div", { class: "meta", text: `${fa(b.shops)} فروشنده از ${fa(d.total)} در تهران (${fa(Math.round(b.share * 100))}٪) · ${fa(b.floors)} طبقه · ${b.area}` }),
      el("div", { class: "mv-actions" },
        el("button", { type: "button", class: "btn blue", onclick: () => act(go, go.label) }, icon("store"), el("span", { text: "مدل‌ها همین‌جا" })),
        el("button", { type: "button", class: "btn line", onclick: () => openRoute({ lat: b.lat, lng: b.lon, name: `${b.kind} ${b.name}` }) }, icon("nav"), el("span", { text: "مسیریابی" }))),
      el("button", { type: "button", class: "ghost", text: "همه‌ی بورس‌ها", onclick: () => hubList(d) }));
  }

  // --- choose the search area on the map ------------------------------------------------------------------------------
  function pick() {
    const v = openView("محدوده رو روی نقشه انتخاب کن", "روی هر نقطه‌ای از تهران بزن");
    const c = cond?.center || { lat: 35.6935, lng: 51.415 };
    meMarker(c).addTo(v.layer);
    v.map.setView([c.lat, c.lng], 14);
    v.tools.append(toolButton("pin", "مکان من", () => locateMe()));
    v.map.on("click", offerHere);
  }

  // --- small maps inside the chat (tap to open the full view) -------------------------------------------------------
  function mini(node, { center, radius_km, points = [], dots = [], hubs = [], route = false, onOpen }) {
    const m = makeMap(node, false);
    const latlngs = [];
    if (center) { meMarker(center).addTo(m); latlngs.push([center.lat, center.lng]); }
    if (center && radius_km) radius(center, radius_km).addTo(m);
    if (dots.length) {
      const renderer = L.canvas({ padding: 0.2 }), color = css("--blue") || "#007aff";
      dots.forEach(([lat, lon]) => L.circleMarker([lat, lon], { renderer, radius: 2.5, stroke: false, fillColor: color, fillOpacity: 0.4, interactive: false }).addTo(m));
    }
    if (hubs.length) { addHubs(m, m, hubs.slice(0, 3)); hubs.slice(0, 3).forEach((b) => latlngs.push([b.lat, b.lon])); }
    const { groups, singles } = groupByBuilding(points);
    singles.forEach((p) => { L.marker([p.lat, p.lon], { icon: divIcon(pricePin(p)), interactive: false, zIndexOffset: p.rank ? 1000 - p.rank : 0 }).addTo(m); latlngs.push([p.lat, p.lon]); });
    groups.forEach((g) => { L.marker([g.lat, g.lon], { icon: divIcon(groupPin(g)), interactive: false, zIndexOffset: 800 }).addTo(m); latlngs.push([g.lat, g.lon]); });
    if (route && center && points[0]) {   // the plan: a dashed line from where you are to the shop
      L.polyline([[center.lat, center.lng], [points[0].lat, points[0].lon]], { className: "tm-route", interactive: false }).addTo(m);
    }
    fit(m, latlngs, 16);
    if (onOpen) {
      node.addEventListener("click", onOpen);
      node.setAttribute("role", "button"); node.setAttribute("tabindex", "0"); node.setAttribute("aria-label", "باز کردن نقشه");
      node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } });
    }
    return m;
  }

  window.TorobMap = { openProduct, openHub, pick, mini, shortPrice };
})();
