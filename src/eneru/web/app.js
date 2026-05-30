"use strict";
// Eneru dashboard — a thin client over the REST API. No third-party code; all
// logic stays server-side. The auth token (session or API key) lives in
// sessionStorage and is sent as a Bearer header, so there is no cookie and thus
// no CSRF surface.

const TOKEN_KEY = "eneru_token";
const THEME_KEY = "eneru_theme";
const SVG_NS = "http://www.w3.org/2000/svg";
let lastEvents = [];
let knownEventSources = [];
// Whether the server has API auth enabled. Learned from /api/v1/config at start;
// when false there is nothing to sign into, so the Sign-in button stays hidden.
let authEnabled = false;
// Snapshots fetched once per refresh and shared by the drill-down, so opening a
// detail panel never triggers per-card config/remote-health requests.
let cfgSnapshot = null;
let remoteHealthSnapshot = [];
let lastUpsRows = [];
let lastGroups = [];

// ----- theme (light / dark / system) -----

function applyTheme(value) {
  const v = (value === "light" || value === "dark") ? value : "system";
  // "system" -> no attribute, so the pure-CSS @media(prefers-color-scheme) rules
  // apply (flash-free default). An explicit choice pins data-theme.
  if (v === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = v;
  try { localStorage.setItem(THEME_KEY, v); } catch (_e) { /* private mode */ }
}

function initTheme() {
  let saved = "system";
  try { saved = localStorage.getItem(THEME_KEY) || "system"; } catch (_e) { /* */ }
  applyTheme(saved);
  const sel = document.getElementById("theme-select");
  if (sel) {
    sel.value = saved;
    sel.addEventListener("change", () => applyTheme(sel.value));
  }
}

function token() { return sessionStorage.getItem(TOKEN_KEY) || ""; }
function setToken(t) {
  if (t) sessionStorage.setItem(TOKEN_KEY, t);
  else sessionStorage.removeItem(TOKEN_KEY);
}

async function api(path, opts) {
  opts = opts || {};
  const headers = opts.headers || {};
  if (token()) headers["Authorization"] = "Bearer " + token();
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body });
  if (res.status === 401) { setToken(""); refreshAuthUI(); }
  let data = null;
  try { data = await res.json(); } catch (_e) { /* non-JSON (static) */ }
  return { ok: res.ok, status: res.status, data };
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    if (k === "class") node.className = attrs[k];
    else if (k === "text") node.textContent = attrs[k];
    else node.setAttribute(k, attrs[k]);
  }
  (children || []).forEach((c) => node.appendChild(c));
  return node;
}

function showError(msg) {
  const box = document.getElementById("error");
  if (!msg) { box.hidden = true; return; }
  box.textContent = msg; box.hidden = false;
}

function statusClass(status) {
  const s = (status || "").toUpperCase();
  if (s.includes("OB") || s.includes("LB") || s.includes("FSD")) return "crit";
  if (s.includes("BOOST") || s.includes("TRIM") || s.includes("BYPASS")) return "warn";
  return "ok";
}

// ----- rendering -----

// A UPS counts as healthy for a redundancy rollup when it is reachable and not
// on battery / low / replace-battery.
function upsHealthy(u) {
  if (u.connectionState && u.connectionState !== "connected") return false;
  const s = (u.status || "").toUpperCase();
  return !(s.includes("OB") || s.includes("LB") || s.includes("RB")
           || s.includes("FSD") || s === "");
}

function batteryClass(charge) {
  if (isNaN(charge)) return "";
  if (charge < 20) return "crit";
  if (charge < 50) return "warn";
  return "ok";
}

function renderUps(payload) {
  const wrap = document.getElementById("ups-cards");
  wrap.replaceChildren();
  const rows = (payload && payload.ups) || [];
  lastUpsRows = rows;
  const sel = document.getElementById("graph-ups");
  const prev = sel.value;
  sel.replaceChildren();
  rows.forEach((u) => {
    const charge = parseFloat(u.batteryCharge);
    const card = el("div", { class: "card card-click", tabindex: "0",
      role: "button", title: "View details" }, [
      el("h3", { text: u.label || u.name }),
      el("div", { class: "row" }, [
        el("span", { text: "Status" }),
        el("span", { class: "badge " + statusClass(u.status), text: u.status || "—" }),
      ]),
      el("div", { class: "row" }, [el("span", { text: "Battery" }),
        el("b", { class: batteryClass(charge), text: isNaN(charge) ? "—" : charge + "%" })]),
      el("div", { class: "bar" }, [el("span", { class: batteryClass(charge) }, [])]),
      el("div", { class: "row" }, [el("span", { text: "Runtime" }),
        el("b", { text: u.runtime != null ? u.runtime + "s" : "—" })]),
      el("div", { class: "row" }, [el("span", { text: "Load" }),
        el("b", { text: u.load != null ? u.load + "%" : "—" })]),
    ]);
    if (!isNaN(charge)) card.querySelector(".bar > span").style.width =
      Math.max(0, Math.min(100, charge)) + "%";
    card.addEventListener("click", () => openDetail(u.name));
    card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDetail(u.name); }
    });
    wrap.appendChild(card);
    sel.appendChild(el("option", { value: u.name, text: u.label || u.name }));
  });
  if (prev) sel.value = prev;

  const groups = (payload && payload.redundancyGroups) || [];
  lastGroups = groups;
  const gsec = document.getElementById("groups-section");
  const gwrap = document.getElementById("group-cards");
  gwrap.replaceChildren();
  gsec.hidden = groups.length === 0;
  const byName = {};
  rows.forEach((u) => { byName[u.name] = u; });
  groups.forEach((g) => {
    const sources = g.upsSources || [];
    const healthy = sources.filter((n) => byName[n] && upsHealthy(byName[n])).length;
    const min = g.minHealthy;
    const cls = healthy < min ? "crit" : (healthy === min ? "warn" : "ok");
    gwrap.appendChild(el("div", { class: "card" }, [
      el("h3", { text: g.name }),
      el("div", { class: "row" }, [el("span", { text: "Healthy" }),
        el("span", { class: "badge " + cls, text: healthy + " / " + min + " required" })]),
      el("div", { class: "row" }, [el("span", { text: "Sources" }),
        el("b", { text: String(sources.length) })]),
    ]));
  });
  updateEventSourceFilter(rows, groups);
}

// ----- UPS detail drill-down (Slice D) -----

let openDetailName = null;

function detailRow(label, value) {
  return el("div", { class: "row" }, [
    el("span", { text: label }),
    el("b", { text: (value === undefined || value === null || value === "")
      ? "—" : String(value) }),
  ]);
}

function detailSection(title, rows) {
  return el("div", { class: "detail-section" },
    [el("h4", { text: title })].concat(rows));
}

function openDetail(name) {
  openDetailName = name;
  renderDetail(name);
  document.getElementById("detail-modal").hidden = false;
}

function closeDetail() {
  openDetailName = null;
  document.getElementById("detail-modal").hidden = true;
}

function renderDetail(name) {
  const u = lastUpsRows.find((r) => r.name === name);
  const body = document.getElementById("detail-body");
  document.getElementById("detail-title").textContent =
    (u && (u.label || u.name)) || name;
  if (!u) { body.replaceChildren(el("p", { text: "No data for this UPS." })); return; }
  const pq = u.powerQuality || {};
  const sections = [];

  sections.push(detailSection("Live status", [
    el("div", { class: "row" }, [el("span", { text: "Status" }),
      el("span", { class: "badge " + statusClass(u.status), text: u.status || "—" })]),
    detailRow("Battery", u.batteryCharge != null ? u.batteryCharge + "%" : null),
    detailRow("Runtime", u.runtime != null ? u.runtime + "s" : null),
    detailRow("Load", u.load != null ? u.load + "%" : null),
    detailRow("Connection", u.connectionState),
    detailRow("Time on battery", u.timeOnBattery != null ? u.timeOnBattery + "s" : null),
  ]));

  sections.push(detailSection("Power quality", [
    detailRow("Input voltage", pq.inputVoltage != null ? pq.inputVoltage + " V" : null),
    detailRow("Output voltage", pq.outputVoltage != null ? pq.outputVoltage + " V" : null),
    detailRow("Battery voltage", pq.batteryVoltage != null ? pq.batteryVoltage + " V" : null),
    detailRow("Input frequency", pq.inputFrequency != null ? pq.inputFrequency + " Hz" : null),
    detailRow("Output frequency", pq.outputFrequency != null ? pq.outputFrequency + " Hz" : null),
    detailRow("Temperature", pq.temperature != null ? pq.temperature + " °C" : null),
  ]));

  // Configuration (from the shared /api/v1/config snapshot).
  const cfgUps = ((cfgSnapshot && cfgSnapshot.ups) || []).find((c) => c.name === name);
  if (cfgUps) {
    const rows = [
      detailRow("Local host", cfgUps.isLocal ? "yes" : "no"),
      detailRow("Remote servers", (cfgUps.remoteServers || []).length),
    ];
    (cfgUps.remoteServers || []).forEach((s, i) =>
      rows.push(detailRow("• server " + (i + 1), s.host || s.name || "configured")));
    if (cfgSnapshot && cfgSnapshot.nutControl) {
      rows.push(detailRow("UPS control", cfgSnapshot.nutControl.enabled ? "enabled" : "disabled"));
    }
    sections.push(detailSection("Configuration", rows));
  }

  // Redundancy group membership.
  const member = lastGroups.filter((g) => (g.upsSources || []).includes(name));
  if (member.length) {
    sections.push(detailSection("Redundancy groups",
      member.map((g) => detailRow(g.name,
        (g.upsSources || []).length + " sources, " + g.minHealthy + " required"))));
  }

  // Remote health rows for this source.
  const rh = remoteHealthSnapshot.filter((r) =>
    r.group === name || r.group === u.label || r.group === u.groupId);
  if (rh.length) {
    sections.push(detailSection("Remote health", rh.map((r) => {
      const host = r.server || r.host || "host";
      const healthy = (r.healthy === true || r.status === "healthy"
        || r.status === "ok" || r.reachable === true);
      return el("div", { class: "row" }, [
        el("span", { text: host }),
        el("span", { class: "badge " + (healthy ? "ok" : "crit"),
          text: healthy ? "reachable" : "unreachable" }),
      ]);
    })));
  }

  body.replaceChildren(...sections);
}

// Banner driven by LIVE status (not stale events): low-battery / shutdown-pending
// is critical; on-battery is a warning; otherwise hidden.
function renderBanner() {
  const banner = document.getElementById("banner");
  const rows = lastUpsRows;
  let crit = null, warn = null;
  for (const u of rows) {
    const s = (u.status || "").toUpperCase();
    if (s.includes("LB") || s.includes("FSD") || u.triggerActive) {
      crit = u; break;
    }
    if (s.includes("OB") && !warn) warn = u;
  }
  if (crit) {
    banner.className = "banner crit";
    const why = crit.triggerReason ? (": " + crit.triggerReason) : "";
    banner.textContent = "⚠️ Shutdown imminent — " +
      (crit.label || crit.name) + " is on low battery" + why;
    banner.hidden = false;
  } else if (warn) {
    banner.className = "banner warn";
    banner.textContent = "🔋 On battery — " + (warn.label || warn.name) +
      " is running on battery power";
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

// Source-qualified identity: the per-DB `id` is only unique within one UPS, so
// it must be paired with `source`. Falls back to a content key for safety.
function eventKey(e) {
  const id = (e.id !== undefined && e.id !== null) ? e.id
    : (e.ts + "|" + (e.eventType || "") + "|" + (e.detail || ""));
  return (e.source || "") + "|" + id;
}

// Merge incoming events into the accumulated, de-duplicated list (so polling and
// "Load older" both grow it without repeats), sorted ascending by (ts, id) and
// capped so a long session can't grow unbounded.
function mergeEvents(incoming) {
  const map = new Map();
  for (const e of lastEvents) map.set(eventKey(e), e);
  for (const e of (incoming || [])) map.set(eventKey(e), e);
  lastEvents = Array.from(map.values())
    .sort((a, b) => (a.ts - b.ts) || ((a.id || 0) - (b.id || 0)));
  if (lastEvents.length > 2000) lastEvents = lastEvents.slice(-2000);
  updateEventTypeFilter(lastEvents);
  applyEventFilters();
}

function eventRangeFrom() {
  const v = document.getElementById("event-range").value;
  if (v === "all") return null;
  return Math.floor(Date.now() / 1000) - parseInt(v, 10);
}

async function loadEvents(beforeEvent) {
  let q = "limit=200";
  const from = eventRangeFrom();
  if (from !== null && !beforeEvent) q += "&from=" + from;
  if (beforeEvent) {
    q += "&before=" + encodeURIComponent(beforeEvent.ts);
    if (beforeEvent.source && beforeEvent.id !== undefined && beforeEvent.id !== null) {
      q += "&beforeSource=" + encodeURIComponent(beforeEvent.source);
      q += "&beforeId=" + encodeURIComponent(beforeEvent.id);
    }
  }
  const res = await api("/api/v1/events?" + q);
  if (res.ok && res.data) mergeEvents(res.data.events);
}

async function loadOlderEvents() {
  const oldest = lastEvents[0];   // ascending sort -> [0] is the oldest shown
  if (!oldest) { await loadEvents(); return; }
  await loadEvents(oldest);
}

function resetEvents() {
  lastEvents = [];
  selectedEvents = new Set();
  loadEvents();
}

function updateEventSourceFilter(upsRows, groups) {
  knownEventSources = [];
  (upsRows || []).forEach((u) => knownEventSources.push({
    value: u.name, label: u.label || u.name,
  }));
  (groups || []).forEach((g) => knownEventSources.push({
    value: "redundancy:" + g.name, label: "redundancy:" + g.name,
  }));
  const sel = document.getElementById("event-source-filter");
  const prev = sel.value;
  sel.replaceChildren(el("option", { value: "", text: "All sources" }));
  knownEventSources.forEach((source) => {
    sel.appendChild(el("option", { value: source.value, text: source.label }));
  });
  if (knownEventSources.some((s) => s.value === prev)) sel.value = prev;
}

function updateEventTypeFilter(rows) {
  const sel = document.getElementById("event-type-filter");
  const prev = sel.value;
  const types = Array.from(new Set((rows || [])
    .map((e) => e.eventType || e.event || "")
    .filter((v) => v))).sort();
  sel.replaceChildren(el("option", { value: "", text: "All types" }));
  types.forEach((type) => sel.appendChild(el("option", { value: type, text: type })));
  if (types.includes(prev)) sel.value = prev;
}

function eventMatchesSource(event, source) {
  if (!source) return true;
  const haystack = [
    event.group || "",
    event.ups || "",
    event.source || "",
    event.detail || "",
    event.details || "",
  ].join(" ").toLowerCase();
  return haystack.includes(source.toLowerCase());
}

// Selected event keys ((source,id)). Persists across re-render so a filter tweak
// or poll doesn't drop the user's selection; cleared after a delete, on range
// change, and on sign-out. Only currently-visible selected rows are ever deleted.
let selectedEvents = new Set();

function visibleEvents() {
  const source = document.getElementById("event-source-filter").value;
  const type = document.getElementById("event-type-filter").value;
  const text = document.getElementById("event-text-filter").value.trim().toLowerCase();
  return lastEvents.filter((e) => {
    const eventType = e.eventType || e.event || "";
    const detail = (e.detail || e.details || "").toLowerCase();
    return eventMatchesSource(e, source)
      && (!type || eventType === type)
      && (!text || detail.includes(text));
  });
}

function applyEventFilters() {
  const body = document.querySelector("#events tbody");
  body.replaceChildren();
  const signedIn = !!token();
  // The selection column + Delete action only exist when signed in; keep the
  // header and empty-state colspan in sync so widths never mismatch.
  document.getElementById("events-head").replaceChildren(...[
    ...(signedIn ? [el("th", { text: "" })] : []),
    el("th", { text: "Time" }), el("th", { text: "Type" }),
    el("th", { text: "Detail" }),
  ]);
  document.getElementById("event-delete").hidden = !signedIn;
  const rows = visibleEvents();
  if (rows.length === 0) {
    body.appendChild(el("tr", null, [
      el("td", { colspan: signedIn ? "4" : "3", text: "No events." })]));
    return;
  }
  rows.forEach((e) => {
    const ts = e.ts ? new Date(e.ts * 1000).toLocaleString() : "—";
    const cells = [];
    if (signedIn) {
      const cb = el("input", { type: "checkbox" });
      cb.checked = selectedEvents.has(eventKey(e));
      cb.addEventListener("change", () => {
        if (cb.checked) selectedEvents.add(eventKey(e));
        else selectedEvents.delete(eventKey(e));
      });
      const td = el("td"); td.appendChild(cb); cells.push(td);
    }
    cells.push(
      el("td", { text: ts }),
      el("td", { text: e.eventType || e.event || "" }),
      el("td", { text: e.detail || e.details || "" }),
    );
    body.appendChild(el("tr", null, cells));
  });
}

async function deleteSelected() {
  // Only visible + selected rows with a real id are deletable — a filtered-out
  // selection is never touched.
  const chosen = visibleEvents().filter(
    (e) => selectedEvents.has(eventKey(e)) && e.id !== undefined && e.id !== null);
  if (chosen.length === 0) return;
  // Group the chosen events by UPS name (the DELETE path is
  // /api/v1/ups/{name}/events).
  const byUps = new Map();
  for (const e of chosen) {
    if (!byUps.has(e.ups)) byUps.set(e.ups, []);
    byUps.get(e.ups).push(e);
  }
  // Prune ONLY rows the server actually deleted: a failed/forbidden request
  // must not make the row vanish from the UI.
  const gone = new Set();
  let failed = 0;
  for (const [ups, evs] of byUps) {
    const items = evs.map((e) => ({ id: e.id, ts: e.ts, eventType: e.eventType || e.event }));
    const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/events",
      { method: "DELETE", body: JSON.stringify({ items }) });
    if (res.ok) evs.forEach((e) => gone.add(eventKey(e)));
    else failed += evs.length;
  }
  if (gone.size) lastEvents = lastEvents.filter((e) => !gone.has(eventKey(e)));
  selectedEvents = new Set();
  showError(failed ? ("Could not delete " + failed + " event(s).") : "");
  applyEventFilters();
}

// Cache the last series so a resize can redraw without refetching.
let lastGraphSeries = null;

function renderGraph(series) {
  if (series !== undefined) lastGraphSeries = series;
  const host = document.getElementById("graph");
  // Size the viewBox to the host's real pixel width so the coordinate system maps
  // 1:1 to screen pixels (no horizontal stretch -> no distorted line/labels).
  // When the host is hidden or not yet laid out, clientWidth is 0; skip rather
  // than emit a broken viewBox — the ResizeObserver redraws once it has width.
  const W = host.clientWidth;
  if (!W) return;
  host.replaceChildren();
  const pts = (lastGraphSeries && lastGraphSeries.data) || [];
  const H = 220, pad = 30;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const axis = (x1, y1, x2, y2) => {
    const l = document.createElementNS(SVG_NS, "line");
    l.setAttribute("x1", x1); l.setAttribute("y1", y1);
    l.setAttribute("x2", x2); l.setAttribute("y2", y2);
    l.setAttribute("class", "axis");
    l.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(l);
  };
  axis(pad, H - pad, W - 5, H - pad);
  axis(pad, 5, pad, H - pad);
  const vals = pts.map((p) => p.value).filter((v) => typeof v === "number" && !isNaN(v));
  if (vals.length >= 2) {
    const min = Math.min(...vals), max = Math.max(...vals);
    const span = max - min || 1;
    const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts, tspan = (t1 - t0) || 1;
    const x = (t) => pad + ((t - t0) / tspan) * (W - pad - 5);
    const y = (v) => (H - pad) - ((v - min) / span) * (H - pad - 5);
    let d = "";
    pts.forEach((p) => {
      if (typeof p.value !== "number" || isNaN(p.value)) return;
      d += (d ? " L" : "M") + x(p.ts).toFixed(1) + " " + y(p.value).toFixed(1);
    });
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d); path.setAttribute("class", "plot");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(path);
    const lab = (txt, yy) => {
      const t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("x", 2); t.setAttribute("y", yy);
      t.setAttribute("class", "lbl"); t.textContent = txt; svg.appendChild(t);
    };
    lab(max.toFixed(0), 12); lab(min.toFixed(0), H - pad);
  } else {
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", W / 2); t.setAttribute("y", H / 2);
    t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "lbl");
    t.textContent = "Not enough data yet"; svg.appendChild(t);
  }
  host.appendChild(svg);
}

// One global observer redraws the cached graph on layout changes (window resize,
// the host going from hidden/zero-width to visible). Registered once in init();
// never recreated by the polling refresh. A rAF coalesces bursts of events.
let _graphRedrawPending = false;
function observeGraphResize() {
  const host = document.getElementById("graph");
  if (!host) return;
  const redraw = () => {
    if (_graphRedrawPending) return;
    _graphRedrawPending = true;
    requestAnimationFrame(() => { _graphRedrawPending = false; renderGraph(); });
  };
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(redraw).observe(host);
  } else {
    window.addEventListener("resize", redraw);
  }
}

// ----- control panel (5c) -----

async function renderControl(payload) {
  const sec = document.getElementById("control-section");
  const panel = document.getElementById("control-panel");
  // Control is only meaningful when authenticated and nut_control is enabled.
  const nutEnabled = cfgSnapshot && cfgSnapshot.nutControl &&
    cfgSnapshot.nutControl.enabled;
  if (!token() || !nutEnabled) { sec.hidden = true; return; }
  sec.hidden = false;
  panel.replaceChildren();
  const rows = (payload && payload.ups) || [];
  for (const u of rows) {
    const box = el("div", { class: "control-ups" }, [el("h3", { text: u.label || u.name })]);
    box.appendChild(el("h4", { text: "Commands" }));
    const cmds = el("div", { class: "cmds" });
    const res = await api("/api/v1/ups/" + encodeURIComponent(u.name) + "/commands");
    ((res.data && res.data.commands) || []).forEach((c) => {
      const btn = el("button", { type: "button", text: c });
      btn.addEventListener("click", () => runCommand(u.name, c));
      cmds.appendChild(btn);
    });
    if (!cmds.childNodes.length) cmds.appendChild(el("span", { class: "who", text: "No allowlisted commands." }));
    box.appendChild(cmds);
    box.appendChild(el("h4", { text: "Variables" }));
    box.appendChild(await renderVariableForms(u.name));
    panel.appendChild(box);
  }
}

async function renderVariableForms(ups) {
  const vars = el("div", { class: "vars" });
  const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/variables");
  const rows = (res.data && res.data.variables) || [];
  rows.forEach((v) => {
    const name = v.name || v.variable || "";
    if (!name) return;
    const input = el("input", { name: "value", value: v.value || "" });
    const form = el("form", { class: "var-form" }, [
      el("label", null, [
        el("span", { text: name }),
        input,
      ]),
      el("button", { type: "submit", text: "Set" }),
    ]);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      setVariable(ups, name, input.value);
    });
    vars.appendChild(form);
  });
  if (!vars.childNodes.length) {
    vars.appendChild(el("span", { class: "who", text: "No allowlisted variables." }));
  }
  return vars;
}

async function runCommand(ups, command) {
  showError("");
  const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/command",
    { method: "POST", body: JSON.stringify({ command }) });
  if (!res.ok) showError("Command failed: " + ((res.data && res.data.error && res.data.error.message) || res.status));
  else setStatus("Ran " + command + " on " + ups);
}

async function setVariable(ups, variable, value) {
  showError("");
  const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/variables/" +
    encodeURIComponent(variable), { method: "PUT", body: JSON.stringify({ value }) });
  if (!res.ok) {
    showError("Variable update failed: " +
      ((res.data && res.data.error && res.data.error.message) || res.status));
  } else {
    setStatus("Set " + variable + " on " + ups);
  }
}

// ----- auth UI -----

function refreshAuthUI() {
  const authed = !!token();
  // Hide Sign-in when already signed in OR when the server has auth disabled
  // (login would just 404 with "Authentication is disabled").
  document.getElementById("loginBtn").hidden = authed || !authEnabled;
  document.getElementById("logoutBtn").hidden = !authed;
  const who = document.getElementById("who");
  who.hidden = !authed;
  if (authed) who.textContent = "Signed in";
}

// Learn whether auth is enabled server-side. /api/v1/config is open (sanitized)
// and reports api.auth.enabled even to anonymous callers.
async function loadAuthState() {
  const res = await api("/api/v1/config");
  authEnabled = !!(res.ok && res.data && res.data.api &&
                   res.data.api.auth && res.data.api.auth.enabled);
}

function openLogin() {
  document.getElementById("login-error").hidden = true;
  document.getElementById("login-modal").hidden = false;
  document.getElementById("login-user").focus();
}
function closeLogin() { document.getElementById("login-modal").hidden = true; }

async function doLogin(ev) {
  ev.preventDefault();
  const username = document.getElementById("login-user").value;
  const password = document.getElementById("login-pass").value;
  const res = await api("/api/v1/auth/login",
    { method: "POST", body: JSON.stringify({ username, password }) });
  if (res.ok && res.data && res.data.token) {
    setToken(res.data.token); closeLogin(); refreshAuthUI(); refresh();
  } else {
    // Surface the server's actual reason (e.g. "Authentication is disabled",
    // "invalid credentials") so a misconfiguration is self-diagnosable.
    const e = document.getElementById("login-error");
    const detail = res.data && res.data.error && res.data.error.message;
    e.textContent = detail ? ("Sign in failed: " + detail) : "Sign in failed.";
    e.hidden = false;
  }
}

async function doLogout() {
  await api("/api/v1/auth/logout", { method: "POST" });
  setToken(""); selectedEvents = new Set(); refreshAuthUI(); refresh();
}

// ----- polling -----

function setStatus(msg) {
  document.getElementById("status-line").textContent =
    msg + " · " + new Date().toLocaleTimeString();
}

async function loadGraph() {
  const ups = document.getElementById("graph-ups").value;
  const metric = document.getElementById("graph-metric").value;
  if (!ups) { renderGraph(null); return; }
  let q = "metric=" + encodeURIComponent(metric);
  const range = document.getElementById("graph-range").value;
  if (range !== "all") {
    const to = Math.floor(Date.now() / 1000);
    q += "&to=" + to + "&from=" + (to - parseInt(range, 10));
  }
  // range "all" omits `from`; the server clamps it to the retention horizon.
  const res = await api("/api/v1/ups/" + encodeURIComponent(ups) +
    "/history?" + q);
  renderGraph(res.ok ? res.data : null);
}

async function refresh() {
  // One shared config + remote-health snapshot per cycle so the drill-down reads
  // from memory instead of firing per-card requests.
  const [cfg, rh] = await Promise.all([
    api("/api/v1/config"), api("/api/v1/remote-health"),
  ]);
  if (cfg.ok && cfg.data) {
    cfgSnapshot = cfg.data;
    // Re-read auth-enabled every poll, not just at init: with dynamic
    // auto-enable the server can turn auth on at runtime, and the Sign-in button
    // must appear without a reload.
    const a = cfg.data.api && cfg.data.api.auth;
    authEnabled = !!(a && a.enabled);
    // If we hold a token but the server treats us as anonymous (sanitized
    // config), the session was invalidated server-side — e.g. the account was
    // deleted. Reads stay open (no 401 to trip the api() handler), so detect it
    // here and sign out locally instead of showing a stale "Signed in".
    if (token() && cfg.data.detail === "sanitized") {
      setToken(""); selectedEvents = new Set();
    }
    refreshAuthUI();
  }
  if (rh.ok) remoteHealthSnapshot = (rh.data && rh.data.servers) || [];

  const ups = await api("/api/v1/ups");
  if (ups.ok) {
    renderUps(ups.data); renderControl(ups.data); renderBanner(); showError("");
  } else if (ups.status !== 401) {
    showError("Could not load UPS status (HTTP " + ups.status + ")");
  }
  await loadEvents();        // merges fresh recent events into the accumulated list
  await loadGraph();
  // If a detail modal is open, keep it live with the fresh snapshot.
  if (!document.getElementById("detail-modal").hidden && openDetailName) {
    renderDetail(openDetailName);
  }
  setStatus("Updated");
}

async function init() {
  initTheme();
  await loadAuthState();
  refreshAuthUI();
  document.getElementById("loginBtn").addEventListener("click", openLogin);
  document.getElementById("logoutBtn").addEventListener("click", doLogout);
  document.getElementById("login-cancel").addEventListener("click", closeLogin);
  document.getElementById("login-form").addEventListener("submit", doLogin);
  document.getElementById("graph-ups").addEventListener("change", loadGraph);
  document.getElementById("graph-metric").addEventListener("change", loadGraph);
  document.getElementById("graph-range").addEventListener("change", loadGraph);
  document.getElementById("event-source-filter").addEventListener("change", applyEventFilters);
  document.getElementById("event-type-filter").addEventListener("change", applyEventFilters);
  document.getElementById("event-text-filter").addEventListener("input", applyEventFilters);
  document.getElementById("event-range").addEventListener("change", resetEvents);
  document.getElementById("event-load-older").addEventListener("click", loadOlderEvents);
  document.getElementById("event-delete").addEventListener("click", deleteSelected);
  document.getElementById("detail-close").addEventListener("click", closeDetail);
  // Esc closes whichever modal is open.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!document.getElementById("detail-modal").hidden) closeDetail();
    if (!document.getElementById("login-modal").hidden) closeLogin();
  });
  observeGraphResize();
  refresh();
  setInterval(refresh, 10000);
}

document.addEventListener("DOMContentLoaded", init);
