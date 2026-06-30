"use strict";
// Eneru dashboard — a thin client over the REST API. No third-party code; all
// logic stays server-side. The auth token (session or API key) lives in
// sessionStorage and is sent as a Bearer header, so there is no cookie and thus
// no CSRF surface.
//
// v6.1: the UI is a tabbed SPA (Overview / Power / Battery / Energy / Events /
// Control / Config). Tabs are real ARIA tabs (arrow-key nav + hash routing).
// Charts are a reusable vanilla-SVG factory (makeChart) with optional voltage
// threshold bands and power-event overlays. Zero build toolchain.

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
let eventSortDirection = "asc";

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
  let res;
  try {
    res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body });
  } catch (_e) {
    // L14: a network error (daemon down, or connectivity lost during a power
    // event -- exactly when the dashboard matters) rejects the fetch. Return a
    // non-ok result with status 0 so callers show a "connection lost" indicator
    // instead of an unhandled rejection that silently freezes the poll loop.
    return { ok: false, status: 0, data: null };
  }
  if (res.status === 401 && path !== "/api/v1/auth/login") clearAuthState();
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

// ----- inline SVG icons -----------------------------------------------------
// A tiny stroke-icon set drawn with currentColor, so icons inherit text/status
// color and theme automatically — no color-emoji font dependency (the host has
// none, which is why the old emoji rendered as tofu). Each value is one path `d`
// (multiple M-segments allowed).
const ICONS = {
  home:    "M3 11.5 12 4l9 7.5 M5.5 10.5V20h13v-9.5",
  bolt:    "M13 3 6 13h5l-1 8 8-11h-5z",
  battery: "M3 8.5h13v7H3z M18.5 11v3 M5.5 10.5v3",
  chart:   "M4 5v14h16 M8 14l3-3 2.4 2L19 8",
  bell:    "M6 9a6 6 0 0 1 12 0c0 5 2.2 7 2.2 7H3.8S6 14 6 9 M10 21h4",
  sliders: "M5 8h14 M5 16h14 M9 6v4 M15 14v4",
  gear:    "M12 9.4a2.6 2.6 0 1 0 0 5.2 2.6 2.6 0 0 0 0-5.2z M19.3 12a7 7 0 0 0-.1-1.2l1.9-1.5-1.9-3.3-2.3 1a7 7 0 0 0-1.7-1l-.4-2.5h-3.7l-.4 2.5a7 7 0 0 0-1.7 1l-2.3-1L4.8 8.3l1.9 1.5a7 7 0 0 0 0 2.4l-1.9 1.5 1.9 3.3 2.3-1a7 7 0 0 0 1.7 1l.4 2.5h3.7l.4-2.5a7 7 0 0 0 1.7-1l2.3 1 1.9-3.3-1.9-1.5a7 7 0 0 0 .1-1.2z",
  shield:  "M12 3 19 6v5c0 4.5-3 7.6-7 8.6-4-1-7-4.1-7-8.6V6z",
  gauge:   "M4.5 16.5a8 8 0 1 1 15 0 M12 13l3-3",
  check:   "M5 12.5 9.5 17 19 7",
  alert:   "M12 4 21 19H3z M12 10v4 M12 16.6v.4",
  close:   "M6 6l12 12 M18 6 6 18",
  power:   "M12 3v9 M7.8 6.4a7 7 0 1 0 8.4 0",
  vm:      "M3 5h18v11H3z M8 20h8 M11 16v4",
  box:     "M12 3 21 7.5v9L12 21 3 16.5v-9z M3 7.5 12 12l9-4.5 M12 12v9",
  disk:    "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  globe:   "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M3 12h18 M12 3c2.6 2.7 2.6 15.3 0 18 M12 3c-2.6 2.7-2.6 15.3 0 18",
};
const SD_PHASE_ICONS = { vms: "vm", containers: "box", "filesystem-sync": "disk",
  "filesystem-unmount": "disk", remote: "globe", "final-sync": "disk",
  "local-poweroff": "power" };
function icon(name, cls) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", "ic" + (cls ? " " + cls : ""));
  s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", ICONS[name] || "");
  s.appendChild(p);
  return s;
}
const TAB_ICONS = { overview: "home", power: "bolt", battery: "battery",
  energy: "chart", events: "bell", control: "sliders", shutdown: "power",
  config: "gear" };

// ----- floating tooltip + help hints ---------------------------------------
// One reused element. Native SVG <title> / title="" only surface after a ~1s
// browser delay and render as a plain OS tooltip; this appears immediately and
// is themed. Shared by chart event markers and the "?" help hints.
let _tip = null;
function tipEl() {
  if (!_tip) {
    _tip = el("div", { class: "tip" });
    _tip.hidden = true;
    document.body.appendChild(_tip);
  }
  return _tip;
}
function moveTip(x, y) {
  const t = tipEl();
  const r = t.getBoundingClientRect();
  let left = x + 14, top = y + 16;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > window.innerHeight - 8) top = y - r.height - 16;
  t.style.left = Math.max(8, left) + "px";
  t.style.top = Math.max(8, top) + "px";
}
function showTip(content, x, y, accent) {
  const t = tipEl();
  t.className = "tip" + (accent ? " " + accent : "");
  if (typeof content === "string") t.replaceChildren(document.createTextNode(content));
  else t.replaceChildren(...(Array.isArray(content) ? content : [content]));
  t.hidden = false;
  moveTip(x, y);
}
function hideTip() { if (_tip) _tip.hidden = true; }

// Wire instant hover/focus tooltips onto `node`. `build` returns string|DOM|DOM[];
// `accent` (optional) is a CSS class that colors the tip's accent border.
function bindTip(node, build, accent) {
  node.addEventListener("mouseenter", (ev) => showTip(build(), ev.clientX, ev.clientY, accent));
  node.addEventListener("mousemove", (ev) => moveTip(ev.clientX, ev.clientY));
  node.addEventListener("mouseleave", hideTip);
  node.addEventListener("focus", () => {
    const r = node.getBoundingClientRect();
    showTip(build(), r.left, r.bottom, accent);
  });
  node.addEventListener("blur", hideTip);
}

// A small focusable "?" that reveals `text` on hover/focus (replaces verbose
// inline footnotes; keyboard-accessible).
function helpHint(text) {
  const h = el("span", { class: "help", tabindex: "0", role: "button",
    "aria-label": text, text: "?" });
  bindTip(h, () => text);
  return h;
}

// Title-case a NUT state token ("INACTIVE" -> "Inactive") for display.
function titleCase(s) {
  s = String(s == null ? "" : s).toLowerCase();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "—";
}

// "x ago" for remote-health epoch-second timestamps.
function relTime(epoch) {
  if (!epoch) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function showError(msg) {
  const box = document.getElementById("error");
  if (!msg) { box.hidden = true; return; }
  box.textContent = msg; box.hidden = false;
}

function numOrNull(v) {
  if (v === undefined || v === null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
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
  const state = (u.connectionState || "").toUpperCase();
  if (state && state !== "OK" && state !== "CONNECTED") return false;
  const s = (u.status || "").toUpperCase();
  return !(s.includes("OB") || s.includes("LB") || s.includes("RB")
           || s.includes("FSD") || s === "");
}

function groupHealthyCount(g, rows) {
  if (typeof g.healthyCount === "number") return g.healthyCount;
  const byName = {};
  rows.forEach((u) => { byName[u.name] = u; });
  return (g.upsSources || []).filter((n) => byName[n] && upsHealthy(byName[n])).length;
}

function groupQuorumLost(g, rows) {
  if (typeof g.quorumLost === "boolean") return g.quorumLost;
  if (typeof g.minHealthy !== "number") return false;
  return groupHealthyCount(g, rows) < g.minHealthy;
}

function batteryClass(charge) {
  if (isNaN(charge)) return "";
  if (charge < 20) return "crit";
  if (charge < 50) return "warn";
  return "ok";
}

function formatRuntimeSeconds(value) {
  if (value === undefined || value === null || value === "") return "—";
  const seconds = Math.trunc(Number(value));
  if (!Number.isFinite(seconds)) return "—";
  if (seconds >= 3600) {
    return Math.floor(seconds / 3600) + "h " +
      Math.floor((seconds % 3600) / 60) + "m";
  }
  if (seconds >= 60) {
    return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
  }
  return seconds + "s";
}

const CHART_UPS_SELECTS = ["power-ups", "battery-ups", "energy-ups"];

// Populate every per-chart UPS <select> with the current UPS list, keeping the
// selection SHARED across the Power/Battery/Energy tabs (like the Range control)
// so switching tabs doesn't snap back to a different UPS.
function populateChartUpsSelects(rows) {
  // Prefer an existing selection that is still valid; else the first UPS.
  let chosen = "";
  for (const id of CHART_UPS_SELECTS) {
    const s = document.getElementById(id);
    if (s && s.value) { chosen = s.value; break; }
  }
  if (!rows.some((u) => u.name === chosen)) chosen = rows.length ? rows[0].name : "";
  CHART_UPS_SELECTS.forEach((id) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.replaceChildren();
    rows.forEach((u) =>
      sel.appendChild(el("option", { value: u.name, text: u.label || u.name })));
    if (chosen) sel.value = chosen;
  });
}

// ----- Overview hero + KPI summary (rc9) -----------------------------------

// SVG battery ring gauge: a track circle + a value arc (dasharray), centered
// label. Colored by status class. The signature visual of the Overview.
function batteryRing(pct, statusCls) {
  const R = 52, C = 2 * Math.PI * R;
  const p = Math.max(0, Math.min(100, isNaN(pct) ? 0 : pct));
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 120 120");
  svg.setAttribute("class", "ring s-" + (statusCls || "ok"));
  const circle = (cls, extra) => {
    const c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", "60"); c.setAttribute("cy", "60"); c.setAttribute("r", String(R));
    c.setAttribute("class", cls);
    if (extra) for (const k in extra) c.setAttribute(k, extra[k]);
    svg.appendChild(c);
  };
  circle("ring-track");
  circle("ring-arc", {
    transform: "rotate(-90 60 60)", "stroke-linecap": "round",
    "stroke-dasharray": `${(p / 100 * C).toFixed(1)} ${C.toFixed(1)}`,
  });
  const txt = (cls, y, s) => {
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", "60"); t.setAttribute("y", String(y));
    t.setAttribute("text-anchor", "middle"); t.setAttribute("class", cls);
    t.textContent = s; svg.appendChild(t);
  };
  txt("ring-big", 60, isNaN(pct) ? "—" : Math.round(p) + "%");
  txt("ring-cap", 78, p >= 95 ? "CHARGED" : p >= 20 ? "ON LINE" : "LOW");
  return svg;
}

function heroCard(u) {
  const charge = parseFloat(u.batteryCharge);
  const sCls = statusClass(u.status);
  const pq = u.powerQuality || {};
  const wrap = el("div", { class: "hero card-click s-" + sCls, tabindex: "0",
    role: "button", title: "View details" });
  wrap.appendChild(batteryRing(charge, batteryClass(charge) || sCls));
  const vital = (label, value) => el("div", null, [
    el("div", { class: "v-label", text: label }),
    el("div", { class: "v-value", text: value }),
  ]);
  wrap.appendChild(el("div", { class: "hero-main" }, [
    el("div", { class: "hero-title" }, [
      icon("battery"), el("h3", { text: u.label || u.name }),
      el("span", { class: "badge " + sCls, text: u.status || "—" }),
    ]),
    el("div", { class: "hero-vitals" }, [
      vital("Runtime", formatRuntimeSeconds(u.runtime)),
      vital("Load", u.load != null ? u.load + "%" : "—"),
      vital("Input", pq.inputVoltage != null ? pq.inputVoltage + " V" : "—"),
      vital("On battery", u.timeOnBattery != null ? u.timeOnBattery + "s" : "—"),
    ]),
  ]));
  const open = () => openDetail(u.name);
  wrap.addEventListener("click", open);
  wrap.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
  });
  return wrap;
}

// A compact KPI summary card that drills through to its tab on click.
function kpiCard({ iconName, label, value, unit, cap, valueStatus, tab }) {
  const card = el("div", { class: "card kpi card-click", tabindex: "0", role: "button" });
  card.appendChild(el("div", { class: "k-label" }, [icon(iconName), el("span", { text: label })]));
  const val = el("div", { class: "k-value" + (valueStatus ? " s-" + valueStatus : "") });
  val.appendChild(document.createTextNode(value));
  if (unit) val.appendChild(el("span", { class: "unit", text: unit }));
  card.appendChild(val);
  card.appendChild(el("div", { class: "k-cap", text: cap || "" }));
  const go = () => selectTab(tab, { updateHash: true });
  card.addEventListener("click", go);
  card.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
  });
  return card;
}

function renderOverviewSummary(rows) {
  const hero = document.getElementById("overview-hero");
  const summary = document.getElementById("overview-summary");
  if (!hero || !summary) return;
  hero.replaceChildren();
  summary.replaceChildren();
  if (!rows.length) {
    hero.appendChild(el("p", { class: "chart-note", text: "No UPS data yet." }));
    return;
  }
  // Hero shows the worst-status UPS so a problem surfaces immediately.
  const rank = { crit: 0, warn: 1, ok: 2 };
  const primary = rows.slice().sort(
    (a, b) => rank[statusClass(a.status)] - rank[statusClass(b.status)])[0];
  hero.appendChild(heroCard(primary));

  // Three drill-through KPI cards surfacing the v6.1 data otherwise buried on
  // other tabs: battery health, energy today, last self-test.
  const bh = primary.batteryHealth;
  summary.appendChild(kpiCard({
    iconName: "shield", label: "Battery health",
    value: bh && bh.score != null ? Math.round(bh.score) : "—",
    unit: bh && bh.score != null ? "/100" : "",
    cap: bh && bh.confidence != null ? "confidence " + Math.round(bh.confidence * 100) + "%"
      : "no data yet",
    valueStatus: bh ? scoreClass(bh.score) : null, tab: "battery" }));

  const en = primary.energy;
  summary.appendChild(kpiCard({
    iconName: "chart", label: "Energy today",
    value: en && en.todayKwh != null ? en.todayKwh.toFixed(2) : "—",
    unit: en && en.todayKwh != null ? " kWh" : "",
    cap: en && en.todayCostFormatted ? en.todayCostFormatted + " today"
      : (en && en.monthKwh != null ? en.monthKwh.toFixed(1) + " kWh this month" : "—"),
    tab: "energy" }));

  // Self-test KPI only when a test has actually run — a "never run" box is noise
  // (and most UPSes never get one). The Battery tab still explains the term.
  const st = primary.selfTest;
  if (st && ["passed", "failed", "running"].includes(st.result)) {
    const stStatus = { passed: "ok", failed: "crit", running: "warn" }[st.result] || null;
    summary.appendChild(kpiCard({
      iconName: "check", label: "Last self-test",
      value: titleCase(st.result),
      cap: st.date ? st.date : "", valueStatus: stStatus, tab: "battery" }));
  }
}

function renderUps(payload) {
  const wrap = document.getElementById("ups-cards");
  wrap.replaceChildren();
  const rows = (payload && payload.ups) || [];
  lastUpsRows = rows;
  rows.forEach((u) => {
    const charge = parseFloat(u.batteryCharge);
    const barValue = isNaN(charge) ? 0 : Math.max(0, Math.min(100, charge));
    const card = el("div", { class: "card card-click", tabindex: "0",
      role: "button", title: "View details" }, [
      el("h3", { text: u.label || u.name }),
      el("div", { class: "row" }, [
        el("span", { text: "Status" }),
        el("span", { class: "badge " + statusClass(u.status), text: u.status || "—" }),
      ]),
      el("div", { class: "row" }, [el("span", { text: "Battery" }),
        el("b", { class: batteryClass(charge), text: isNaN(charge) ? "—" : charge + "%" })]),
      el("meter", {
        class: "bar " + batteryClass(charge),
        min: "0", max: "100", value: String(barValue),
        "aria-label": "Battery charge",
      }),
      el("div", { class: "row" }, [el("span", { text: "Runtime" }),
        el("b", { text: formatRuntimeSeconds(u.runtime) })]),
      el("div", { class: "row" }, [el("span", { text: "Load" }),
        el("b", { text: u.load != null ? u.load + "%" : "—" })]),
    ]);
    card.classList.add("s-" + statusClass(u.status));   // status accent rail
    card.addEventListener("click", () => openDetail(u.name));
    card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDetail(u.name); }
    });
    wrap.appendChild(card);
  });
  populateChartUpsSelects(rows);
  // Single UPS → the Overview is the hero + KPI summary; the raw per-UPS card
  // grid only appears for a fleet (multi-UPS).
  document.getElementById("ups-section").hidden = rows.length <= 1;
  renderOverviewSummary(rows);

  const groups = (payload && payload.redundancyGroups) || [];
  lastGroups = groups;
  const gsec = document.getElementById("groups-section");
  const gwrap = document.getElementById("group-cards");
  gwrap.replaceChildren();
  gsec.hidden = groups.length === 0;
  groups.forEach((g) => {
    const sources = g.upsSources || [];
    const healthy = groupHealthyCount(g, rows);
    const min = g.minHealthy;
    const quorumLost = groupQuorumLost(g, rows);
    const cls = quorumLost ? "crit" : (healthy === min ? "warn" : "ok");
    gwrap.appendChild(el("div", { class: "card" }, [
      el("h3", { text: g.name }),
      el("div", { class: "row" }, [el("span", { text: "Healthy" }),
        el("span", { class: "badge " + cls, text: healthy + " / " + min + " required" })]),
      el("div", { class: "row" }, [el("span", { text: "Sources" }),
        el("b", { text: String(sources.length) })]),
    ]));
  });
  renderRemoteHealth();
  updateEventSourceFilter(rows, groups);
}

// ----- UPS detail drill-down (Slice D) -----

let openDetailName = null;
// Where focus was before the modal opened, so closing returns it there instead
// of dropping keyboard / screen-reader users back at the top of the page.
let detailReturnFocus = null;

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

function remoteHealthReachable(row) {
  const status = String((row && row.status) || "").toUpperCase();
  return row && (
    row.healthy === true || row.reachable === true ||
    status === "HEALTHY" || status === "OK"
  );
}

function remoteStatusClass(row) {
  // Check the explicit status FIRST: a DEGRADED row may also carry
  // reachable:true, and it should read amber, not green.
  const s = String((row && row.status) || "").toUpperCase();
  if (s.includes("DEGRADED") || s.includes("WARN")) return "warn";
  if (remoteHealthReachable(row)) return "ok";
  return "crit";
}

// Overview widget: SSH shutdown targets / remote NUT servers and whether the
// daemon can currently reach them (from /api/v1/remote-health). Hidden when none
// are configured.
function renderRemoteHealth() {
  const sec = document.getElementById("remote-section");
  const wrap = document.getElementById("remote-cards");
  if (!sec || !wrap) return;
  const servers = remoteHealthSnapshot || [];
  sec.hidden = servers.length === 0;
  if (!servers.length) { wrap.replaceChildren(); return; }
  wrap.replaceChildren(...servers.map((r) => {
    const cls = remoteStatusClass(r);
    const reachable = remoteHealthReachable(r);
    const name = r.server || r.host || "server";
    // Health is carried by the colored status strip + icon + the Status row; the
    // badge stays out of the head so a long server name isn't clipped.
    const rows = [el("div", { class: "card-head" }, [
      el("span", { class: "card-ico s-" + cls }, [icon("shield")]),
      el("h3", { text: name }),
    ])];
    // Label matches the color: ok→reachable, warn→degraded, crit→unreachable
    // (a DEGRADED server is amber, not a contradictory "unreachable").
    const statusText = cls === "ok" ? "reachable"
      : cls === "warn" ? "degraded" : "unreachable";
    rows.push(el("div", { class: "row" }, [el("span", { text: "Status" }),
      el("span", { class: "badge " + cls, text: statusText })]));
    if (r.host && r.host !== name) rows.push(configKv("Host", r.host));
    if (r.latency_ms != null && reachable) {
      rows.push(configKv("Latency", Math.round(r.latency_ms) + " ms"));
    }
    rows.push(configKv("Checked", relTime(r.last_checked_at)));
    if (r.consecutive_failures) {
      rows.push(el("div", { class: "row" }, [el("span", { text: "Failures" }),
        el("b", { class: "crit", text: String(r.consecutive_failures) })]));
    }
    if (!reachable && r.last_error) {
      rows.push(el("div", { class: "row" },
        [el("span", { class: "energy-note", text: r.last_error })]));
    }
    return el("div", { class: "card s-" + cls }, rows);
  }));
}

function openDetail(name) {
  detailReturnFocus = document.activeElement;
  openDetailName = name;
  renderDetail(name);
  document.getElementById("detail-modal").hidden = false;
  // Pull focus into the dialog so Tab stays among its controls and assistive
  // tech announces it; the close button is the first focusable element.
  document.getElementById("detail-close").focus();
}

function closeDetail() {
  openDetailName = null;
  document.getElementById("detail-modal").hidden = true;
  if (detailReturnFocus && typeof detailReturnFocus.focus === "function") {
    detailReturnFocus.focus();
  }
  detailReturnFocus = null;
}

const BH_TERM_LABELS = {
  capacity: "Capacity trend", runtime: "Runtime vs nominal",
  self_test: "Last self-test", anomaly: "Anomalies", age: "Battery age",
};
const BH_TERM_HELP = {
  capacity: "Runtime trend over time vs the nominal full runtime. Needs about "
    + "two weeks of history before it's trusted — until then it reads n/a rather "
    + "than guessing (a few days of jitter would otherwise look like total loss).",
  runtime: "Current runtime under load vs the expected full runtime. n/a until "
    + "the nominal runtime is configured or learned at a full charge.",
  self_test: "Result of the latest battery self-test — only a pass or fail counts.",
  anomaly: "Confirmed battery anomalies; each one lowers the score.",
  age: "Battery age vs its expected service life (set battery_install_date and "
    + "expected_life_years).",
};

// Build the v6.1 battery-health rows shared by the detail modal and the Battery
// tab. "unknown" is shown honestly rather than a fake high score, and the
// per-term breakdown explains WHY the score is what it is. ``includeScore`` is
// false on the Battery tab, where the card's header badge already shows it.
function batteryHealthRows(bh, opts) {
  const includeScore = !(opts && opts.includeScore === false);
  const rows = [];
  if (includeScore) {
    rows.push(hintedRow("Score",
      bh.score != null ? Math.round(bh.score) + "/100" : "unknown",
      "A weighted average of the available terms below. Terms without enough "
      + "data are left out (never counted as full marks)."));
  }
  if (bh.confidence != null) {
    rows.push(hintedRow("Confidence", Math.round(bh.confidence * 100) + "%",
      "How much of the scoring weight had data behind it. Lower means the score "
      + "rests on fewer terms."));
  }
  if (bh.replacementDaysRemaining != null) {
    rows.push(detailRow("Replace in", "~" + Math.round(bh.replacementDaysRemaining) + " days"));
  }
  // Per-term breakdown: each sub-score (0-100) or n/a when that term has no data.
  const terms = bh.terms || {};
  ["capacity", "runtime", "self_test", "anomaly", "age"].forEach((k) => {
    if (!(k in terms)) return;
    const v = terms[k];
    const val = v == null
      ? el("b", { class: "na", text: "n/a" })
      : el("b", { class: scoreClass(v), text: String(Math.round(v)) });
    rows.push(el("div", { class: "row" }, [
      el("span", { class: "label-tip" },
        [el("span", { text: BH_TERM_LABELS[k] }), helpHint(BH_TERM_HELP[k])]),
      val,
    ]));
  });
  return rows;
}

// Build the v6.1 energy rows shared by the detail modal and the Energy tab.
function energyCostConfigured(en) {
  // The server includes the cost fields whenever energy.cost_per_kwh is set —
  // even if the computed value is unknown — so their PRESENCE (not truthiness)
  // tells us cost tracking is on.
  return en && ("todayCost" in en || "monthCost" in en);
}

// The window/estimated/partial context that used to be verbose footnotes is now
// carried by a "?" hint on the relevant value, so the block stays compact.
const ENERGY_HELP = {
  estimated: "Estimated from load × rated power — the UPS doesn't report real "
    + "watts. Set energy.nominal_power for a closer figure.",
  partial: "Some intervals had data gaps, so this is based on the samples "
    + "available so far.",
};

// A key/value row whose label carries a "?" hint (replaces the old footnotes).
function hintedRow(label, value, tipText) {
  const labelNode = tipText
    ? el("span", { class: "label-tip" }, [el("span", { text: label }), helpHint(tipText)])
    : el("span", { text: label });
  return el("div", { class: "row" }, [labelNode, el("b", { text: value })]);
}

function energyRows(en) {
  const rows = [];
  let todayTip = "Window: " + (en.todayLabel || "since midnight") + ".";
  if (en.estimated) todayTip += " " + ENERGY_HELP.estimated;
  if (en.partial) todayTip += " " + ENERGY_HELP.partial;
  rows.push(hintedRow("Today",
    en.todayKwh != null ? en.todayKwh.toFixed(3) + " kWh" : "—", todayTip));
  // Only show the month line once it has data (no value early in the month or on
  // a fresh UPS reads cleaner than a row full of "unknown").
  if (en.monthKwh != null) {
    rows.push(hintedRow("This month", en.monthKwh.toFixed(3) + " kWh",
      "Window: " + (en.monthLabel || "since the 1st") + "."));
  }
  if (en.yearKwh != null) {
    rows.push(hintedRow("This year", en.yearKwh.toFixed(3) + " kWh",
      "Window: " + (en.yearLabel || "since Jan 1") + "."));
  }
  if (energyCostConfigured(en)) {
    // Configured but no kWh yet -> "calculating…", not a blunt "unknown".
    rows.push(detailRow("Today cost", en.todayCostFormatted || "calculating…"));
    if (en.monthKwh != null) {
      rows.push(detailRow("Month cost", en.monthCostFormatted || "calculating…"));
    }
    if (en.yearKwh != null) {
      rows.push(detailRow("Year cost", en.yearCostFormatted || "calculating…"));
    }
  }
  return rows;
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
    detailRow("Runtime", formatRuntimeSeconds(u.runtime)),
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

  // v6.1 Battery health (score/terms/replacement).
  if (u.batteryHealth) {
    sections.push(detailSection("Battery health", batteryHealthRows(u.batteryHealth)));
  }
  // v6.1 Energy (today/month kWh + optional cost; cost hidden when disabled).
  if (u.energy) {
    sections.push(detailSection("Energy", energyRows(u.energy)));
  }
  // v6.1 Self-test (latest normalized result).
  const st = u.selfTest;
  if (st) {
    sections.push(detailSection("Self-test", [
      detailRow("Result", st.result || "unknown"),
      detailRow("When", st.date || null),
    ]));
  }

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
      const healthy = remoteHealthReachable(r);
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
      const groups = lastGroups.filter((g) => (g.upsSources || []).includes(u.name));
      const causesShutdown = groups.length === 0
        || groups.some((g) => groupQuorumLost(g, rows));
      if (causesShutdown) { crit = u; break; }
      if (!warn) warn = u;
    }
    if (s.includes("OB") && !warn) warn = u;
  }
  const setBanner = (cls, iconName, text) => {
    banner.className = "banner " + cls;
    banner.replaceChildren(icon(iconName), el("span", { text: text }));
    banner.hidden = false;
  };
  if (crit) {
    const why = crit.triggerReason ? (": " + crit.triggerReason) : "";
    setBanner("crit", "alert", "Shutdown imminent — " +
      (crit.label || crit.name) + " is on low battery" + why);
  } else if (warn) {
    setBanner("warn", "battery", "On battery — " + (warn.label || warn.name) +
      " is running on battery power");
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
    .sort((a, b) => {
      const as = String(a.source || "");
      const bs = String(b.source || "");
      return (a.ts - b.ts) ||
        (as < bs ? -1 : as > bs ? 1 : 0) ||
        ((a.id || 0) - (b.id || 0));
    });
  if (lastEvents.length > 2000) lastEvents = lastEvents.slice(-2000);
  updateEventTypeFilter(lastEvents);
  applyEventFilters();
}

function eventRangeFrom() {
  const v = document.getElementById("event-range").value;
  if (v === "all") return null;
  return Math.floor(Date.now() / 1000) - parseInt(v, 10);
}

async function loadEvents(beforeEvent, clearSelection = false) {
  let q = "limit=200";
  const from = eventRangeFrom();
  if (from !== null) q += "&from=" + from;
  // Only intentional actions clear the selection; the passive 10s poll passes
  // clearSelection=false so an in-progress selection survives a refresh.
  if (clearSelection) selectedEvents = new Set();
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
  // A range change is a new dataset, so the selection no longer applies.
  loadEvents(undefined, true);
}

function updateEventSourceFilter(upsRows, groups) {
  knownEventSources = [];
  (upsRows || []).forEach((u) => knownEventSources.push({
    value: u.name, label: u.label || u.name,
  }));
  // M8: do NOT offer "redundancy:<name>" as an event source. Redundancy-group
  // power events are written to the text log only -- they never land in any
  // per-UPS stats DB, so /api/v1/events can never return rows for them and the
  // filter would be permanently empty. (`groups` is accepted for signature
  // stability / future use once redundancy events are persisted.)
  void groups;
  const sel = document.getElementById("event-source-filter");
  const prev = sel.value;
  sel.replaceChildren(el("option", { value: "", text: "All sources" }));
  knownEventSources.forEach((source) => {
    sel.appendChild(el("option", { value: source.value, text: source.label }));
  });
  if (knownEventSources.some((s) => s.value === prev)) sel.value = prev;
}

// Rebuild the per-type checkbox list from the loaded events (the Tier dropdown
// is the primary filter; these only narrow within the tier).
function updateEventTypeFilter(rows) {
  const box = document.getElementById("event-type-filter");
  // The Tier dropdown is the primary, window-independent filter; the per-type
  // checkboxes are an OPTIONAL narrowing within the tier and default to none
  // selected (= all types in the tier). Preserve whatever the operator ticked.
  const selected = selectedEventTypes();
  const types = Array.from(new Set((rows || [])
    .map((e) => e.eventType || e.event || "")
    .filter((v) => v))).sort();
  box.replaceChildren();
  const kept = new Set();
  types.forEach((type) => {
    const input = el("input", { type: "checkbox", value: type });
    if (selected.has(type)) {
      input.checked = true;
      kept.add(type);
    }
    box.appendChild(el("label", { class: "event-type-option" }, [
      input,
      el("span", { text: type }),
    ]));
  });
  updateEventTypeSummary(kept);
}

function selectedEventTypes() {
  const box = document.getElementById("event-type-filter");
  if (!box) return new Set();
  return new Set(Array.from(
    box.querySelectorAll('input[type="checkbox"]:checked'),
  ).map((input) => input.value));
}

function updateEventTypeSummary(types) {
  const summary = document.getElementById("event-type-summary");
  if (!summary) return;
  const selected = Array.from(types || selectedEventTypes());
  if (selected.length === 0) summary.textContent = "All types";
  else if (selected.length === 1) summary.textContent = selected[0];
  else summary.textContent = selected.length + " types";
}

function eventMatchesSource(event, source) {
  if (!source) return true;
  return event.ups === source || event.source === source || event.group === source;
}

// Selected event keys ((source,id)). Preserved across passive polling so an
// in-progress selection survives a 10s refresh; cleared only on intentional
// actions — range change, successful delete, sign-out, and server-side session
// invalidation — so a stale destructive selection cannot linger.
let selectedEvents = new Set();

function visibleEvents() {
  const source = document.getElementById("event-source-filter").value;
  const types = selectedEventTypes();
  const text = document.getElementById("event-text-filter").value.trim().toLowerCase();
  const from = eventRangeFrom();
  const rows = lastEvents.filter((e) => {
    const eventType = e.eventType || e.event || "";
    const detail = (e.detail || e.details || "").toLowerCase();
    return (from === null || e.ts >= from)
      && eventMatchesSource(e, source)
      && eventPassesTier(eventType)              // window-independent tier gate
      && (types.size === 0 || types.has(eventType))  // optional advanced narrowing
      && (!text || detail.includes(text));
  });
  if (eventSortDirection === "desc") rows.reverse();
  return rows;
}

function timeSortHeader() {
  const label = eventSortDirection === "asc" ? "Time ↑" : "Time ↓";
  const btn = el("button", {
    id: "event-sort-time",
    type: "button",
    class: "th-sort",
    text: label,
    "aria-label": "Sort events by time",
    "aria-pressed": eventSortDirection === "desc" ? "true" : "false",
  });
  btn.addEventListener("click", toggleEventSort);
  return el("th", null, [btn]);
}

function preserveWindowScroll(fn) {
  const scrollX = window.scrollX;
  const scrollY = window.scrollY;
  fn();
  window.scrollTo(scrollX, scrollY);
}

function toggleEventSort(ev) {
  if (ev) ev.preventDefault();
  eventSortDirection = eventSortDirection === "asc" ? "desc" : "asc";
  preserveWindowScroll(applyEventFilters);
}

// Reflect the live, actionable selection on the Delete button. The count is the
// number of currently-visible rows with a real id — exactly what deleteSelected
// will act on — so the label never promises a delete it won't perform. Disabled
// at zero so the button is never a silent no-op.
function updateDeleteButton() {
  const btn = document.getElementById("event-delete");
  if (!token()) { btn.hidden = true; return; }
  btn.hidden = false;
  const n = visibleEvents().filter(
    (e) => selectedEvents.has(eventKey(e)) && e.id !== undefined && e.id !== null).length;
  btn.disabled = n === 0;
  btn.textContent = n ? ("Delete selected (" + n + ")") : "Delete selected";
}

function applyEventFilters() {
  const body = document.querySelector("#events tbody");
  body.replaceChildren();
  const signedIn = !!token();
  // The selection column + Delete action only exist when signed in; keep the
  // header and empty-state colspan in sync so widths never mismatch.
  document.getElementById("events-head").replaceChildren(...[
    ...(signedIn ? [el("th", { text: "" })] : []),
    timeSortHeader(), el("th", { text: "Type" }),
    el("th", { text: "Detail" }),
  ]);
  updateDeleteButton();
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
        updateDeleteButton();
      });
      const td = el("td"); td.appendChild(cb); cells.push(td);
    }
    cells.push(
      el("td", { text: ts }),
      el("td", null, [eventTypeBadge(e)]),
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
  // /api/v1/ups/{name}/events). Every event carries BOTH a raw `ups`
  // (group.ups.name) and a sanitized `source` (the groupId that eventKey uses
  // for identity); see status.query_events. The server resolves the path name
  // via _resolve_ups_name -> find_status, which matches the raw name OR its
  // sanitized form, so grouping by the raw `ups` reaches the same UPS that the
  // source-keyed identity refers to. Two encodings of one UPS, not a mismatch.
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
    const deleted = res.ok && res.data ? Number(res.data.deleted) : 0;
    if (res.ok && deleted === items.length) {
      evs.forEach((e) => gone.add(eventKey(e)));
    } else {
      failed += evs.length;
    }
  }
  if (gone.size) lastEvents = lastEvents.filter((e) => !gone.has(eventKey(e)));
  selectedEvents = new Set();
  showError(failed ? ("Could not delete " + failed + " event(s).") : "");
  applyEventFilters();
}

// ----- charts (v6.1) -------------------------------------------------------
// A reusable SVG plotter. Each chart instance owns its DOM hosts + cached
// series, so the Power / Battery / Energy tabs draw independently. Optional
// voltage threshold bands and power-event overlays layer on top of the line.

// Marker color class by event type, for the chart overlays. Recovery events are
// green, outage/danger red, warnings amber, everything else a distinct violet
// (NOT the blue of the plot line, so a marker never blends into the curve).
function eventMarkerClass(type) {
  const t = (type || "").toUpperCase();
  if (t.includes("RESTORED") || t.includes("NORMALIZED")
      || t.includes("RESOLVED") || t.includes("RECOVER")
      || t.includes("INACTIVE")) return "ev-ok";  // bypass/AVR left = good news
  if (t.includes("SHUTDOWN") || t.includes("FSD") || t.includes("ON_BATTERY")
      || t.includes("BROWNOUT") || t.includes("BATTERY_LOW")
      || t.includes("CONNECTION_LOST") || t.includes("CRITICAL")) return "ev-crit";
  if (t.includes("OVER_VOLTAGE") || t.includes("OVERLOAD") || t.includes("BYPASS")
      || t.includes("AVR") || t.includes("WARNING") || t.includes("ANOMALY")
      || t.includes("REPLACE")) return "ev-warn";
  return "ev-info";
}

// Events-table Type cell: a colored, icon-led badge whose severity matches the
// chart marker color, so a marker and its row read the same at a glance.
function eventTypeBadge(e) {
  const type = e.eventType || e.event || "";
  const cls = eventMarkerClass(type);
  const tone = { "ev-ok": "ok", "ev-warn": "warn", "ev-crit": "crit" }[cls] || "info";
  const ico = { "ev-ok": "check", "ev-warn": "alert", "ev-crit": "alert" }[cls] || "bell";
  return el("span", { class: "ev-badge " + tone }, [icon(ico), el("span", { text: type })]);
}

// Event tiers (mirror the TUI's Power / Diagnostics / Lifecycle split):
//   power  = the events an operator actually cares about on a chart or in the
//            default events view (outages, voltage excursions, battery alerts).
//   lifecycle = routine daemon start/stop/upgrade/recover rows.
//   diag   = everything else (AVR cycling, suppressed flaps, slow-NUT, etc.).
// Substring patterns match the emitted event-type names. NOTE: the emitted name
// is BATTERY_LOW (not LOW_BATTERY) and battery-health alerts are BATTERY_HEALTH_*
// — both are power-tier and were previously missed.
const TIER1_EVENT_PATTERNS = [
  "ON_BATTERY", "POWER_RESTORED", "BATTERY_LOW", "SHUTDOWN", "FSD",
  "OVER_VOLTAGE", "VOLTAGE_HIGH", "VOLTAGE_LOW", "BROWNOUT",
  "OVERLOAD_ACTIVE", "OVERLOAD_DETECTED", "BYPASS_MODE_ACTIVE",
  "CONNECTION_LOST", "CONNECTION_RESTORED", "REPLACE_BATTERY",
  "BATTERY_REPLACEMENT", "BATTERY_HEALTH", "SELF_TEST", "ANOMALY",
];
const LIFECYCLE_EVENT_PATTERNS = ["DAEMON_"];
function isTier1Event(type) {
  const u = (type || "").toUpperCase();
  return TIER1_EVENT_PATTERNS.some((p) => u.includes(p));
}
function isLifecycleEvent(type) {
  const u = (type || "").toUpperCase();
  return LIFECYCLE_EVENT_PATTERNS.some((p) => u.includes(p));
}
// Which display tier an event belongs to.
function eventTierOf(type) {
  if (isTier1Event(type)) return "power";
  if (isLifecycleEvent(type)) return "lifecycle";
  return "diag";
}
// Current Events-tab tier mode (window-INDEPENDENT): power | diag | all.
function eventTierMode() {
  const sel = document.getElementById("event-tier");
  return (sel && sel.value) || "power";
}
// Does an event pass the selected tier? power -> power only; diag -> power+diag
// (not lifecycle); all -> everything. This is the fix for "widen the window and
// the outage still shows" — it keys off the type, not what's in the window.
function eventPassesTier(type) {
  const mode = eventTierMode();
  if (mode === "all") return true;
  const tier = eventTierOf(type);
  if (mode === "diag") return tier !== "lifecycle";
  return tier === "power";
}

// Human-readable tooltip for a chart event marker.
function eventDescription(e) {
  const type = e.eventType || e.event || "event";
  const when = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
  const detail = e.detail || e.details || "";
  return type + (when ? (" @ " + when) : "") + (detail ? ("\n" + detail) : "");
}

// The same event as a structured tooltip body (type / time / detail) for the
// instant floating tip.
function eventTipNode(e) {
  const kids = [el("div", { class: "tip-head",
    text: e.eventType || e.event || "event" })];
  if (e.ts) kids.push(el("div", { class: "tip-sub",
    text: new Date(e.ts * 1000).toLocaleString() }));
  const detail = e.detail || e.details || "";
  if (detail) kids.push(el("div", { class: "tip-body", text: detail }));
  return kids;
}

// Append one event marker (vertical guide + dot) wrapped in a focusable <g>,
// plus a wide transparent hit line, so hovering/focusing anywhere along the
// guide shows the tooltip (the bare 3px dot was nearly impossible to hit). The
// tooltip is the instant themed tip (the old native <title> only appeared after
// a ~1s delay and looked like a plain OS tooltip). `e` carries the event; cls is
// the color class.
function appendEventMarker(svg, e, ex, top, bottom) {
  const cls = eventMarkerClass(e.eventType || e.event);
  const x = parseFloat(ex), yTop = parseFloat(top), yBot = parseFloat(bottom);
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "ev-marker");
  g.setAttribute("tabindex", "0");
  g.setAttribute("role", "img");
  g.setAttribute("aria-label", eventDescription(e));
  const mkline = (cssClass) => {
    const l = document.createElementNS(SVG_NS, "line");
    l.setAttribute("x1", x); l.setAttribute("y1", yTop);
    l.setAttribute("x2", x); l.setAttribute("y2", yBot);
    l.setAttribute("class", cssClass);
    l.setAttribute("vector-effect", "non-scaling-stroke");
    g.appendChild(l);
  };
  mkline("ev-hit");                 // wide transparent full-height hover target
  mkline("ev-line " + cls);         // faint guide (CSS brightens on hover)
  // The at-a-glance marker is a small colored triangle sitting ON the time axis,
  // not a bold full-height line — reads as an annotation pin, color-coded by type.
  const tri = document.createElementNS(SVG_NS, "polygon");
  tri.setAttribute("points",
    `${(x - 4).toFixed(1)},${yBot} ${(x + 4).toFixed(1)},${yBot} ${x.toFixed(1)},${(yBot - 7).toFixed(1)}`);
  tri.setAttribute("class", "ev-pin " + cls);
  g.appendChild(tri);
  bindTip(g, () => eventTipNode(e), cls);
  svg.appendChild(g);
}

function isVoltageMetric(metric) {
  return metric === "voltage" || metric === "output_voltage"
      || metric === "battery_voltage";
}

// Human metric name + unit so a bare number axis is identifiable (esp. when the
// metric dropdown changes input vs output voltage, frequency, etc.).
const METRIC_LABELS = {
  charge: "Battery charge (%)",
  runtime: "Runtime",
  load: "Load (%)",
  voltage: "Input voltage (V)",
  output_voltage: "Output voltage (V)",
  frequency: "Input frequency (Hz)",
  output_frequency: "Output frequency (Hz)",
  battery_voltage: "Battery voltage (V)",
  temperature: "Temperature (°C)",
  real_power: "Power (W)",
};
function metricLabel(metric) { return METRIC_LABELS[metric] || metric || ""; }

// Draw `series` (a /history payload) into the host element, with optional
// `bands` (voltage thresholds) and `events` (overlay markers).
function drawChart(hostId, series, options) {
  options = options || {};
  const host = document.getElementById(hostId);
  if (!host) return;
  hideTip();  // a redraw detaches any hovered marker without firing mouseleave
  // Size the viewBox to the host's real pixel width so the coordinate system
  // maps 1:1 to screen pixels. When hidden/zero-width the ResizeObserver redraws
  // once it has width.
  const W = host.clientWidth;
  if (!W) return;
  host.replaceChildren();
  const pts = (series && series.data) || [];
  const H = 220, pad = 34;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const line = (x1, y1, x2, y2, cls) => {
    const l = document.createElementNS(SVG_NS, "line");
    l.setAttribute("x1", x1); l.setAttribute("y1", y1);
    l.setAttribute("x2", x2); l.setAttribute("y2", y2);
    l.setAttribute("class", cls);
    l.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(l);
  };
  line(pad, H - pad, W - 5, H - pad, "axis");
  line(pad, 5, pad, H - pad, "axis");

  // Metric name + unit, top-centered, so the numbers on the axis are identifiable.
  if (options.metric) {
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", (W / 2).toFixed(0)); t.setAttribute("y", "12");
    t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "chart-title");
    t.textContent = metricLabel(options.metric);
    svg.appendChild(t);
  }

  const vals = pts.map((p) => p.value).filter((v) => typeof v === "number" && !isNaN(v));
  const bands = options.bands || null;
  const metric = options.metric;
  const wantBand = bands && isVoltageMetric(metric)
    && (bands.low != null || bands.high != null || bands.nominal != null);

  if (vals.length < 2) {
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", W / 2); t.setAttribute("y", H / 2);
    t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "lbl");
    t.textContent = "Not enough data yet"; svg.appendChild(t);
    host.appendChild(svg);
    return;
  }

  // Extend the value range to include the band so shaded thresholds are visible
  // even when readings sit inside them.
  let min = Math.min(...vals), max = Math.max(...vals);
  if (wantBand) {
    [bands.low, bands.high, bands.nominal].forEach((v) => {
      if (v != null) { min = Math.min(min, v); max = Math.max(max, v); }
    });
  }
  const span = (max - min) || 1;
  const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts, tspan = (t1 - t0) || 1;
  const x = (t) => pad + ((t - t0) / tspan) * (W - pad - 5);
  const y = (v) => (H - pad) - ((v - min) / span) * (H - pad - 5);

  // Horizontal gridlines (quarter divisions) — the single biggest "this is a
  // real chart" cue. Drawn first, behind everything.
  for (let i = 1; i <= 3; i++) {
    const gy = (5 + i * (H - pad - 5) / 4).toFixed(1);
    line(pad, gy, W - 5, gy, "grid");
  }

  // Voltage threshold band: a faint zone bounded by dashed edge lines (reads as
  // thresholds, not a slab of page-tint), plus the nominal center line.
  if (wantBand && bands.low != null && bands.high != null) {
    const yHigh = y(bands.high), yLow = y(bands.low);
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", pad); rect.setAttribute("y", yHigh.toFixed(1));
    rect.setAttribute("width", (W - pad - 5).toFixed(1));
    rect.setAttribute("height", Math.max(0, yLow - yHigh).toFixed(1));
    rect.setAttribute("class", "band");
    svg.appendChild(rect);
    line(pad, yHigh.toFixed(1), W - 5, yHigh.toFixed(1), "band-edge");
    line(pad, yLow.toFixed(1), W - 5, yLow.toFixed(1), "band-edge");
  }
  if (wantBand && bands.nominal != null) {
    line(pad, y(bands.nominal).toFixed(1), W - 5, y(bands.nominal).toFixed(1), "band-nominal");
  }

  // Event overlays: vertical guides at each event timestamp inside the range,
  // colored by type. Cap markers so a dense window doesn't drown the SVG.
  const events = options.events || [];
  if (events.length) {
    const inRange = events.filter((e) => e.ts >= t0 && e.ts <= t1);
    const MAX = 100;
    const shown = inRange.length > MAX
      ? inRange.filter((_e, i) => i % Math.ceil(inRange.length / MAX) === 0)
      : inRange;
    shown.forEach((e) => {
      appendEventMarker(svg, e, x(e.ts).toFixed(1), 5, (H - pad).toFixed(1));
    });
    if (inRange.length > shown.length) {
      const note = document.createElementNS(SVG_NS, "text");
      note.setAttribute("x", W - 8); note.setAttribute("y", H - pad - 4);
      note.setAttribute("text-anchor", "end"); note.setAttribute("class", "lbl");
      note.textContent = inRange.length + " events (showing " + shown.length + ")";
      svg.appendChild(note);
    }
  }

  // The data line.
  let d = "";
  pts.forEach((p) => {
    if (typeof p.value !== "number" || isNaN(p.value)) return;
    d += (d ? " L" : "M") + x(p.ts).toFixed(1) + " " + y(p.value).toFixed(1);
  });
  // Subtle gradient area fill under the line (single-series charts without a
  // threshold band) — premium depth, not a flat slab.
  if (!wantBand && d) {
    const gid = hostId + "-areagrad";
    const defs = document.createElementNS(SVG_NS, "defs");
    const grad = document.createElementNS(SVG_NS, "linearGradient");
    grad.setAttribute("id", gid);
    grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0");
    grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
    [["0", "0.22"], ["1", "0"]].forEach(([off, op]) => {
      const s = document.createElementNS(SVG_NS, "stop");
      s.setAttribute("offset", off); s.setAttribute("stop-color", "var(--accent)");
      s.setAttribute("stop-opacity", op); grad.appendChild(s);
    });
    defs.appendChild(grad); svg.appendChild(defs);
    const area = document.createElementNS(SVG_NS, "path");
    const xL = x(pts[pts.length - 1].ts).toFixed(1), xF = x(pts[0].ts).toFixed(1);
    area.setAttribute("d", `${d} L${xL} ${(H - pad).toFixed(1)} L${xF} ${(H - pad).toFixed(1)} Z`);
    area.setAttribute("class", "area"); area.setAttribute("fill", `url(#${gid})`);
    svg.appendChild(area);
  }
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", d); path.setAttribute("class", "plot");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);

  // "Now" marker at the latest reading + min/max axis labels.
  const fmt = (metric === "runtime") ? formatRuntimeSeconds : (v) => v.toFixed(0);
  const lastPt = [...pts].reverse().find(
    (p) => typeof p.value === "number" && !isNaN(p.value));
  if (lastPt) {
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", x(lastPt.ts).toFixed(1));
    dot.setAttribute("cy", y(lastPt.value).toFixed(1));
    dot.setAttribute("r", "3.5"); dot.setAttribute("class", "now-dot");
    svg.appendChild(dot);
  }
  const lab = (txt, yy) => {
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", 2); t.setAttribute("y", yy);
    t.setAttribute("class", "lbl"); t.textContent = txt; svg.appendChild(t);
  };
  lab(fmt(max), 12); lab(fmt(min), H - pad);
  host.appendChild(svg);
}

// Per-instance chart over the /history series. Owns its DOM hosts + caches and
// is driven by the tab controller (load on activate, redraw on resize).
function makeChart(opts) {
  const state = { series: null, events: [], thresholds: null };
  // Generation guard: overlapping load()s (tab switch + 10s poll + control
  // change) must not let a slow earlier response overwrite fresher data — only
  // the most recent load() is allowed to commit its results.
  let gen = 0;

  function metric() {
    if (opts.metricSelId) {
      const sel = document.getElementById(opts.metricSelId);
      if (sel) return sel.value;
    }
    return opts.fixedMetric;
  }
  function upsName() {
    const sel = document.getElementById(opts.upsSelId);
    return sel ? sel.value : "";
  }
  function rangeSeconds() {
    const sel = document.getElementById(opts.rangeSelId);
    const v = sel ? sel.value : "86400";
    return (v === "all") ? null : parseInt(v, 10);
  }

  async function load() {
    const host = document.getElementById(opts.hostId);
    if (!host) return;
    const myGen = ++gen;
    const ups = upsName();
    if (!ups) { state.series = null; state.events = []; draw(); return; }
    const m = metric();
    let q = "metric=" + encodeURIComponent(m);
    const range = rangeSeconds();
    let from = null, to = null;
    if (range !== null) {
      to = Math.floor(Date.now() / 1000);
      from = to - range;
      q += "&to=" + to + "&from=" + from;
    }
    const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/history?" + q);
    if (myGen !== gen) return;   // a newer load() superseded this one
    const series = res.ok ? res.data : null;
    let events = [];
    if (opts.events) {
      // Fetch up to the server max: markers are filtered to THIS UPS + tier-1
      // client-side, so a busy multi-UPS fleet can't let other UPSes' events
      // consume a small cap and drop this UPS's markers. (Per-UPS server-side
      // filtering is a follow-up; the events endpoint aggregates all DBs.)
      let eq = "limit=10000";
      if (from !== null) eq += "&from=" + from;
      if (to !== null) eq += "&to=" + to;
      const ev = await api("/api/v1/events?" + eq);
      if (myGen !== gen) return;
      const rows = (ev.ok && ev.data && ev.data.events) || [];
      // Chart markers show only tier-1 power events (no daemon starts / upgrades).
      events = rows.filter(
        (e) => eventMatchesSource(e, ups) && isTier1Event(e.eventType || e.event));
    }
    state.series = series;
    state.events = events;
    if (opts.bands) {
      const u = lastUpsRows.find((r) => r.name === ups);
      const pq = (u && u.powerQuality) || {};
      state.thresholds = {
        nominal: numOrNull(pq.nominalVoltage),
        low: numOrNull(pq.warningLow),
        high: numOrNull(pq.warningHigh),
      };
    }
    draw();
  }

  function draw() {
    drawChart(opts.hostId, state.series, {
      metric: metric(),
      bands: opts.bands ? state.thresholds : null,
      events: opts.events ? state.events : null,
    });
    if (opts.noteId) {
      const note = document.getElementById(opts.noteId);
      if (note) {
        const th = state.thresholds;
        const showBand = !!opts.bands && isVoltageMetric(metric()) && th
          && (th.low != null || th.high != null);
        note.hidden = !showBand;
        if (showBand) {
          note.textContent =
            "Shaded band = currently configured voltage warning thresholds "
            + "(reference overlay, not per-sample history).";
        }
      }
    }
  }

  function observe() {
    const host = document.getElementById(opts.hostId);
    if (!host) return;
    let pending = false;
    const redraw = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; draw(); });
    };
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(redraw).observe(host);
    else window.addEventListener("resize", redraw);
  }

  return { load, draw, observe };
}

// Energy chart: a dual-line plot of load% and power (W) over /power, each line
// independently scaled (different units) with a legend, so "what is 17..31?" is
// unambiguous. Watts is realpower when the UPS reports it, else load*nominal.
function drawEnergyChart(hostId, rows, options) {
  options = options || {};
  const host = document.getElementById(hostId);
  if (!host) return;
  hideTip();  // a redraw detaches any hovered marker without firing mouseleave
  const W = host.clientWidth;
  if (!W) return;
  host.replaceChildren();
  const H = 220, pad = 38;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const mkline = (x1, y1, x2, y2, cls) => {
    const l = document.createElementNS(SVG_NS, "line");
    l.setAttribute("x1", x1); l.setAttribute("y1", y1);
    l.setAttribute("x2", x2); l.setAttribute("y2", y2);
    l.setAttribute("class", cls);
    l.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(l);
  };
  mkline(pad, H - pad, W - 5, H - pad, "axis");
  mkline(pad, 5, pad, H - pad, "axis");

  const pts = rows || [];
  const loads = pts.map((p) => p.loadPct).filter((v) => typeof v === "number" && !isNaN(v));
  const watts = pts.map((p) => p.watts).filter((v) => typeof v === "number" && !isNaN(v));
  if (pts.length < 2 || (loads.length < 2 && watts.length < 2)) {
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", W / 2); t.setAttribute("y", H / 2);
    t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "lbl");
    t.textContent = "Not enough data yet"; svg.appendChild(t);
    host.appendChild(svg);
    return;
  }
  const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts, tspan = (t1 - t0) || 1;
  const x = (t) => pad + ((t - t0) / tspan) * (W - pad - 5);

  function scale(vals, floorZero) {
    if (vals.length < 2) return null;
    let mn = Math.min(...vals), mx = Math.max(...vals);
    if (floorZero) mn = Math.min(mn, 0);
    const span = (mx - mn) || 1;
    return { mn, mx, y: (v) => (H - pad) - ((v - mn) / span) * (H - pad - 5) };
  }
  const loadS = scale(loads, true);
  const wattS = scale(watts, true);

  // tier-1 event markers (already filtered upstream).
  (options.events || []).filter((e) => e.ts >= t0 && e.ts <= t1).slice(0, 100)
    .forEach((e) => {
      appendEventMarker(svg, e, x(e.ts).toFixed(1), 5, (H - pad).toFixed(1));
    });

  function plot(key, sc, cls) {
    if (!sc) return;
    let d = "";
    pts.forEach((p) => {
      const v = p[key];
      if (typeof v !== "number" || isNaN(v)) return;
      d += (d ? " L" : "M") + x(p.ts).toFixed(1) + " " + sc.y(v).toFixed(1);
    });
    if (!d) return;
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d); path.setAttribute("class", cls);
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(path);
  }
  // When watts is ESTIMATED from load (no real-power sensor) the two lines have
  // an identical shape — plotting both is redundant noise, so show only Power (W),
  // the figure operators actually want. Show Load% alongside ONLY when watts is a
  // real measurement (then they genuinely differ and the comparison is useful).
  const realWatts = pts.some((p) => typeof p.watts === "number" && p.estimated === false);
  const showLoad = !wattS || realWatts;
  if (wattS) plot("watts", wattS, "plot plot-watts");
  if (showLoad) plot("loadPct", loadS, "plot plot-load");

  // Legend + per-line max labels (each line has its own unit/scale).
  let lx = pad + 4;
  const legend = (label, cls, max, unit) => {
    const sw = document.createElementNS(SVG_NS, "rect");
    sw.setAttribute("x", lx); sw.setAttribute("y", 6);
    sw.setAttribute("width", 10); sw.setAttribute("height", 3);
    sw.setAttribute("class", cls); svg.appendChild(sw);
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", lx + 14); t.setAttribute("y", 11);
    t.setAttribute("class", "lbl");
    t.textContent = label + (max != null ? (" (max " + max.toFixed(0) + unit + ")") : "");
    svg.appendChild(t);
    lx += 150;
  };
  if (wattS) legend("Power", "plot-watts", wattS.mx, "W");
  if (showLoad && loadS) legend("Load", "plot-load", loadS.mx, "%");
  // Min/max axis numbers for the primary series (watts if present, else load).
  const prim = wattS || loadS;
  if (prim) {
    const unit = wattS ? " W" : " %";
    const lab = (txt, yy) => {
      const t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("x", "2"); t.setAttribute("y", yy);
      t.setAttribute("class", "lbl"); t.textContent = txt; svg.appendChild(t);
    };
    lab(prim.mx.toFixed(0) + unit, 12); lab(prim.mn.toFixed(0) + unit, H - pad);
  }
  host.appendChild(svg);
}

// A compact single-series line plot (`[{ts, value}]`) with fixed y-bounds.
// Lighter than drawChart (no metric dropdown / events / threshold bands) — used
// for the battery-health score trend. Host must carry class "graph" so the
// shared chart chrome (grid / plot / area / now-dot / labels) applies.
function drawSimpleSeries(host, pts, opts) {
  opts = opts || {};
  hideTip();
  host.replaceChildren();
  const W = host.clientWidth || 600, H = 220, pad = 30, padR = 12, padT = 18;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts;
  const span = Math.max(1, t1 - t0);
  const lo = opts.min != null ? opts.min : Math.min(...pts.map((p) => p.value));
  const hi = opts.max != null ? opts.max : Math.max(...pts.map((p) => p.value));
  const rng = Math.max(1e-9, hi - lo);
  const x = (ts) => pad + (ts - t0) / span * (W - pad - padR);
  const y = (v) => padT + (1 - (v - lo) / rng) * (H - padT - pad);
  for (let i = 0; i <= 4; i++) {
    const yy = (padT + i / 4 * (H - padT - pad)).toFixed(1);
    const ln = document.createElementNS(SVG_NS, "line");
    ln.setAttribute("x1", pad); ln.setAttribute("x2", W - padR);
    ln.setAttribute("y1", yy); ln.setAttribute("y2", yy);
    ln.setAttribute("class", "grid"); svg.appendChild(ln);
  }
  const txt = (s, xx, yy, cls, anchor) => {
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", xx); t.setAttribute("y", yy); t.setAttribute("class", cls);
    if (anchor) t.setAttribute("text-anchor", anchor);
    t.textContent = s; svg.appendChild(t);
  };
  if (opts.title) txt(opts.title, (W / 2).toFixed(0), 11, "chart-title", "middle");
  txt(Math.round(hi) + (opts.unit || ""), 2, padT + 4, "lbl");
  txt(Math.round(lo) + (opts.unit || ""), 2, H - pad, "lbl");
  let d = "";
  pts.forEach((p, i) => {
    d += (i ? " L" : "M") + x(p.ts).toFixed(1) + " " + y(p.value).toFixed(1);
  });
  const area = document.createElementNS(SVG_NS, "path");
  area.setAttribute("d", d + " L" + x(t1).toFixed(1) + " " + (H - pad).toFixed(1)
    + " L" + x(t0).toFixed(1) + " " + (H - pad).toFixed(1) + " Z");
  area.setAttribute("class", "area bh"); svg.appendChild(area);
  const line = document.createElementNS(SVG_NS, "path");
  line.setAttribute("d", d); line.setAttribute("class", "plot");
  line.setAttribute("vector-effect", "non-scaling-stroke"); svg.appendChild(line);
  const last = pts[pts.length - 1];
  const dot = document.createElementNS(SVG_NS, "circle");
  dot.setAttribute("cx", x(last.ts).toFixed(1));
  dot.setAttribute("cy", y(last.value).toFixed(1));
  dot.setAttribute("r", "3"); dot.setAttribute("class", "now-dot");
  svg.appendChild(dot);
  host.appendChild(svg);
}

function makeEnergyChart(opts) {
  const state = { rows: [], events: [] };
  let gen = 0;
  function upsName() {
    const sel = document.getElementById(opts.upsSelId);
    return sel ? sel.value : "";
  }
  function rangeSeconds() {
    const sel = document.getElementById(opts.rangeSelId);
    const v = sel ? sel.value : "86400";
    return (v === "all") ? null : parseInt(v, 10);
  }
  async function load() {
    const host = document.getElementById(opts.hostId);
    if (!host) return;
    const myGen = ++gen;
    const ups = upsName();
    if (!ups) { state.rows = []; state.events = []; draw(); return; }
    let q = "";
    const range = rangeSeconds();
    let from = null, to = null;
    if (range !== null) {
      to = Math.floor(Date.now() / 1000);
      from = to - range;
      q = "?from=" + from + "&to=" + to;
    }
    const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/power" + q);
    if (myGen !== gen) return;
    const rows = (res.ok && res.data && res.data.data) || [];
    let eq = "limit=1000";
    if (from !== null) eq += "&from=" + from;
    if (to !== null) eq += "&to=" + to;
    const ev = await api("/api/v1/events?" + eq);
    if (myGen !== gen) return;
    const erows = (ev.ok && ev.data && ev.data.events) || [];
    state.rows = rows;
    state.events = erows.filter(
      (e) => eventMatchesSource(e, ups) && isTier1Event(e.eventType || e.event));
    draw();
  }
  function draw() {
    drawEnergyChart(opts.hostId, state.rows, { events: state.events });
  }
  function observe() {
    const host = document.getElementById(opts.hostId);
    if (!host) return;
    let pending = false;
    const redraw = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; draw(); });
    };
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(redraw).observe(host);
    else window.addEventListener("resize", redraw);
  }
  return { load, draw, observe };
}

// Chart instances, created in init(). Keyed by the tab they live on.
const charts = {};

// ----- battery-health + energy tab widgets (v6.1) --------------------------

function widgetCard(title, rows, opts) {
  opts = opts || {};
  const head = [];
  // Optional icon chip (same treatment as the Line-quality / Remote-server
  // cards), so every widget card shares one title style.
  if (opts.icon) {
    head.push(el("span", { class: "card-ico" + (opts.iconClass ? " s-" + opts.iconClass : "") },
      [icon(opts.icon)]));
  }
  head.push(el("h3", { text: title }));
  if (opts.badge) head.push(el("span", { class: "badge " + (opts.badgeClass || "ok"),
    text: opts.badge }));
  return el("div", { class: "card" }, [
    el("div", { class: "card-head" }, head),
    ...rows,
  ]);
}

function scoreClass(score) {
  if (score == null) return "warn";        // unknown -> caution, never "ok"
  if (score < 40) return "crit";
  if (score < 70) return "warn";
  return "ok";
}

function renderBatteryHealthTab() {
  const wrap = document.getElementById("battery-health");
  if (!wrap) return;
  wrap.replaceChildren();
  const rows = lastUpsRows;
  if (!rows.length) {
    wrap.appendChild(el("p", { class: "chart-note", text: "No UPS data yet." }));
    return;
  }
  rows.forEach((u) => {
    const bh = u.batteryHealth;
    const st = u.selfTest;
    if (bh) {
      const scoreTxt = bh.score != null ? Math.round(bh.score) + "/100" : "unknown";
      // Header badge carries the score, so the rows omit it (no duplicate).
      const cardRows = batteryHealthRows(bh, { includeScore: false });
      // Only show a self-test row once a test has actually run.
      if (st && st.result) {
        cardRows.push(el("div", { class: "row" }, [
          el("span", { text: "Last self-test" }),
          el("b", { class: { passed: "ok", failed: "crit", running: "warn" }[st.result] || "",
            text: titleCase(st.result) + (st.date ? (" · " + st.date) : "") }),
        ]));
      }
      wrap.appendChild(widgetCard(u.label || u.name, cardRows,
        { badge: scoreTxt, badgeClass: scoreClass(bh.score),
          icon: "shield", iconClass: scoreClass(bh.score) }));
    } else {
      wrap.appendChild(widgetCard(u.label || u.name,
        [el("p", { class: "chart-note", text: "Battery health not available." })],
        { icon: "shield", iconClass: "warn" }));
    }
  });
  renderBatteryHealthGraph();
}

// Battery-health score trend (v6.1). Sparse rows from the dedicated
// battery_health table (one per update_interval), so a wide default window.
let _bhGraphGen = 0;
async function renderBatteryHealthGraph() {
  const host = document.getElementById("bh-graph");
  if (!host) return;
  const sel = document.getElementById("battery-ups");
  const name = (sel && sel.value) || (lastUpsRows[0] && lastUpsRows[0].name);
  if (!name) { host.replaceChildren(); return; }
  const myGen = ++_bhGraphGen;
  const res = await api("/api/v1/ups/" + encodeURIComponent(name)
    + "/battery-health-history");
  if (myGen !== _bhGraphGen) return;  // a newer call superseded this one
  const data = (res.ok && res.data && res.data.data) || [];
  const pts = data.filter((r) => r.score != null)
    .map((r) => ({ ts: r.ts, value: r.score }));
  const note = document.getElementById("bh-graph-note");
  if (pts.length < 2) {
    host.replaceChildren();
    if (note) {
      note.hidden = false;
      note.textContent = "Health-score trend appears once a couple of readings "
        + "have been recorded (one per battery_health.update_interval).";
    }
    return;
  }
  if (note) note.hidden = true;
  drawSimpleSeries(host, pts, { title: "Health score", unit: "", min: 0, max: 100 });
}

function renderEnergyTab() {
  const wrap = document.getElementById("energy-cards");
  if (!wrap) return;
  wrap.replaceChildren();
  const rows = lastUpsRows;
  if (!rows.length) {
    wrap.appendChild(el("p", { class: "chart-note", text: "No UPS data yet." }));
    return;
  }
  let costConfigured = false;
  rows.forEach((u) => {
    const en = u.energy;
    if (en) {
      if (energyCostConfigured(en)) costConfigured = true;
      wrap.appendChild(widgetCard(u.label || u.name, energyRows(en), { icon: "chart" }));
    } else {
      wrap.appendChild(widgetCard(u.label || u.name,
        [el("p", { class: "chart-note",
          text: "Energy tracking not available (no samples yet)." })],
        { icon: "chart" }));
    }
  });
  // Only nudge about cost when it isn't already configured — once cost_per_kwh
  // is set the cost rows render (as a value or "unknown"), so the hint would be
  // wrong and confusing.
  if (!costConfigured) {
    wrap.appendChild(el("p", { class: "chart-note",
      text: "Tip: set energy.cost_per_kwh in the config to also track cost." }));
  }
}

// ----- line quality (Power tab) --------------------------------------------
// A glanceable read on incoming mains quality from the live power-quality block:
// is the voltage in tolerance, the frequency near nominal, and are the UPS
// regulation states (AVR / bypass / overload) quiet. Summarized as Good/Fair/Poor.

function nearNominalFreq(hz) {
  if (hz == null) return true;
  return Math.abs(hz - 50) <= 1.5 || Math.abs(hz - 60) <= 1.5;
}

function lineQuality(pq) {
  const inV = numOrNull(pq.inputVoltage);
  const lo = numOrNull(pq.warningLow), hi = numOrNull(pq.warningHigh);
  const banded = inV != null && lo != null && hi != null;
  const active = (v) => String(v || "").toUpperCase() === "ACTIVE";
  const vState = String(pq.voltageState || "").toUpperCase();
  if ((banded && (inV < lo || inV > hi)) || active(pq.overloadState)
      || active(pq.bypassState) || (vState && vState !== "NORMAL")) {
    return { cls: "crit", label: "Poor" };
  }
  const nearEdge = banded && (inV < lo + (hi - lo) * 0.1 || inV > hi - (hi - lo) * 0.1);
  if (active(pq.avrState) || nearEdge || !nearNominalFreq(numOrNull(pq.inputFrequency))) {
    return { cls: "warn", label: "Fair" };
  }
  return { cls: "ok", label: "Good" };
}

// A regulation-state row whose badge is green when quiet (Normal/Inactive) and
// amber when Active.
function stateRow(label, value) {
  const v = String(value || "").toUpperCase();
  const cls = v === "ACTIVE" ? "warn" : (v === "NORMAL" || v === "INACTIVE" ? "ok" : "");
  return el("div", { class: "row" }, [el("span", { text: label }),
    el("span", { class: "badge " + cls, text: titleCase(value) })]);
}

function renderLineQuality() {
  const wrap = document.getElementById("line-quality");
  if (!wrap) return;
  wrap.replaceChildren();
  const sel = document.getElementById("power-ups");
  const name = sel && sel.value;
  const u = lastUpsRows.find((r) => r.name === name) || lastUpsRows[0];
  const pq = u && u.powerQuality;
  if (!pq) return;   // nothing to summarize; the chart below still renders
  const q = lineQuality(pq);
  const inV = numOrNull(pq.inputVoltage);
  const lo = numOrNull(pq.warningLow), hi = numOrNull(pq.warningHigh);
  const banded = inV != null && lo != null && hi != null;
  const rows = [el("div", { class: "card-head" }, [
    el("span", { class: "card-ico s-" + q.cls }, [icon("gauge")]),
    el("h3", { text: "Line quality" }),
    el("span", { class: "badge " + q.cls, text: q.label }),
  ])];
  rows.push(el("div", { class: "row" }, [
    el("span", { class: "label-tip" }, [el("span", { text: "Input" }),
      helpHint(banded ? "Acceptable band " + lo + "–" + hi + " V (nominal "
        + (pq.nominalVoltage != null ? pq.nominalVoltage : "—") + " V)."
        : "Incoming mains voltage.")]),
    el("b", { class: banded && (inV < lo || inV > hi) ? "crit" : "ok",
      text: inV != null ? inV + " V" : "—" }),
  ]));
  if (pq.outputVoltage != null) rows.push(configKv("Output", pq.outputVoltage + " V"));
  if (pq.inputFrequency != null) {
    rows.push(el("div", { class: "row" }, [el("span", { text: "Frequency" }),
      el("b", { class: nearNominalFreq(numOrNull(pq.inputFrequency)) ? "ok" : "warn",
        text: pq.inputFrequency + " Hz" })]));
  }
  [["Voltage", pq.voltageState], ["AVR", pq.avrState], ["Bypass", pq.bypassState],
   ["Overload", pq.overloadState]].forEach(([lab, val]) => {
    if (val != null && val !== "") rows.push(stateRow(lab, val));
  });
  if (pq.temperature != null) rows.push(configKv("Temperature", pq.temperature + " °C"));
  wrap.appendChild(el("div", { class: "card s-" + q.cls }, rows));
}

// ----- config tab ----------------------------------------------------------

function configKv(label, value) {
  return el("div", { class: "row" }, [
    el("span", { text: label }),
    el("b", { text: (value === undefined || value === null || value === "")
      ? "—" : String(value) }),
  ]);
}

// Colored, collapsible JSON tree. Objects/arrays render as <details> so each
// section expands/collapses on its own; leaves are syntax-colored by type.
function jsonValueSpan(v) {
  let cls = "j-num", txt = String(v);
  if (typeof v === "string") { cls = "j-str"; txt = JSON.stringify(v); }
  else if (typeof v === "boolean") cls = "j-bool";
  else if (v === null) { cls = "j-null"; txt = "null"; }
  const s = el("span", { class: cls });
  s.textContent = txt;
  return s;
}

function jsonNode(key, value, topLevel) {
  if (value !== null && typeof value === "object") {
    const isArr = Array.isArray(value);
    const entries = isArr ? value.map((v, i) => [i, v]) : Object.entries(value);
    const det = el("details", { class: "json-node" });
    if (topLevel) det.setAttribute("open", "");   // top level open; sections start collapsed
    const sum = el("summary");
    if (key !== undefined) {
      const k = el("span", { class: "j-key" }); k.textContent = String(key);
      sum.appendChild(k); sum.appendChild(document.createTextNode(" "));
    }
    // Collapsed reads `key {…}`; expanded reads `key {` (the indented left
    // border conveys the grouping) — cleaner than a running item count.
    sum.appendChild(el("span", { class: "j-punct j-open", text: isArr ? "[" : "{" }));
    sum.appendChild(el("span", { class: "j-ellipsis", text: "…" }));
    sum.appendChild(el("span", { class: "j-punct j-close", text: isArr ? "]" : "}" }));
    det.appendChild(sum);
    const kids = el("div", { class: "json-kids" });
    entries.forEach(([k, v]) => kids.appendChild(jsonNode(k, v, false)));
    det.appendChild(kids);
    return det;
  }
  const row = el("div", { class: "json-leaf" });
  if (key !== undefined) {
    const k = el("span", { class: "j-key" }); k.textContent = String(key);
    row.appendChild(k); row.appendChild(document.createTextNode(": "));
  }
  row.appendChild(jsonValueSpan(value));
  return row;
}

let _lastConfigJson = null;
function renderConfigTab() {
  const body = document.getElementById("config-body");
  if (!body) return;
  const cfg = cfgSnapshot;
  if (!cfg) {
    _lastConfigJson = null;
    body.replaceChildren(el("p", { class: "chart-note", text: "Loading…" }));
    return;
  }
  // The 10s poll re-activates this tab; rebuilding the tree every time would
  // collapse every <details> the operator expanded. Rebuild only when the config
  // actually changed (rare — hot reload / sign-in), so expand state is preserved.
  const json = JSON.stringify(cfg);
  if (json === _lastConfigJson && body.childElementCount) return;
  _lastConfigJson = json;
  body.replaceChildren();
  const cards = [];
  (cfg.ups || []).forEach((c) => {
    cards.push(widgetCard(c.label || c.name, [
      configKv("Name", c.name),
      configKv("Local host", c.isLocal ? "yes" : "no"),
      configKv("Remote servers", (c.remoteServers || []).length),
    ], { icon: "battery" }));
  });
  // Only surface what's ON — a list of "disabled" rows is just noise.
  const enabled = [];
  if (cfg.nutControl && cfg.nutControl.enabled) enabled.push("UPS control");
  if (enabled.length) {
    cards.push(widgetCard("Enabled features",
      enabled.map((f) => el("div", { class: "row" },
        [el("span", { text: f }), el("b", { class: "ok", text: "on" })])),
      { icon: "check" }));
  }
  body.appendChild(el("div", { class: "cards" }, cards));

  if (cfg.detail === "sanitized") {
    body.appendChild(el("p", { class: "chart-note",
      text: "Sign in to see the full configuration (secrets are never shown)." }));
  }
  // The (sanitized) config as a colored, collapsible tree — each section
  // expands/collapses on its own.
  body.appendChild(el("h2", { text: "Configuration (JSON)" }));
  body.appendChild(el("div", { class: "json-tree" }, [jsonNode(undefined, cfg, true)]));
}

// ----- control panel (5c) -----

// L15: cache key for the built control panel. The command/variable lists are
// config-static, so rebuilding the panel every poll was pure waste -- 2 extra
// requests per UPS each cycle AND it wiped any half-typed variable value. We
// rebuild only when the auth token or the set of UPS names actually changes.
let _controlBuiltKey = null;

async function renderControl(payload) {
  const sec = document.getElementById("control-section");
  const empty = document.getElementById("control-empty");
  const panel = document.getElementById("control-panel");
  // Control is only meaningful when authenticated and nut_control is enabled.
  const nutEnabled = cfgSnapshot && cfgSnapshot.nutControl &&
    cfgSnapshot.nutControl.enabled;
  // The Control TAB visibility tracks availability too, so it isn't an empty
  // panel for anonymous / read-only users.
  const available = !!token() && !!nutEnabled;
  const tab = document.getElementById("tab-control");
  if (tab) tab.hidden = !available;
  if (!available) {
    sec.hidden = true;
    if (empty) empty.hidden = false;
    _controlBuiltKey = null;  // rebuild when control becomes available again
    return;
  }
  sec.hidden = false;
  if (empty) empty.hidden = true;
  const rows = (payload && payload.ups) || [];
  // Key on token + UPS set AND the allowlists, so a live config reload that
  // changes allowed commands/variables (without changing token or UPS set)
  // still busts the cache and rebuilds (CodeRabbit). /api/v1/config exposes the
  // allowlists when authenticated.
  const nc = (cfgSnapshot && cfgSnapshot.nutControl) || {};
  const key = JSON.stringify({
    token: token(),
    ups: rows.map((u) => u.name),
    commands: nc.allowedCommands || [],
    variables: nc.allowedVariables || [],
  });
  if (key === _controlBuiltKey) return;  // already built for this token + UPS set + allowlists
  // Build into a detached fragment and commit the cache key only once EVERY
  // fetch succeeded (cubic P2). Setting the key up-front meant a transient
  // commands/variables fetch failure built an empty panel that then never
  // rebuilt for the rest of the session.
  let builtOk = true;
  const frag = document.createDocumentFragment();
  for (const u of rows) {
    const box = el("div", { class: "control-ups" }, [el("h3", { text: u.label || u.name })]);
    box.appendChild(el("h4", { text: "Commands" }));
    const cmds = el("div", { class: "cmds" });
    const res = await api("/api/v1/ups/" + encodeURIComponent(u.name) + "/commands");
    if (!res.ok) builtOk = false;
    ((res.data && res.data.commands) || []).forEach((c) => {
      const btn = el("button", { type: "button", text: c });
      btn.addEventListener("click", () => runCommand(u.name, c));
      cmds.appendChild(btn);
    });
    if (!cmds.childNodes.length) cmds.appendChild(el("span", { class: "who", text: "No allowlisted commands." }));
    box.appendChild(cmds);
    box.appendChild(el("h4", { text: "Variables" }));
    const vres = await renderVariableForms(u.name);
    if (!vres.ok) builtOk = false;
    box.appendChild(vres.node);
    // v6.1: self-test trigger (auth-gated; goes through the control allowlist).
    box.appendChild(el("h4", { text: "Self-test" }));
    const stBox = el("div", { class: "cmds" });
    const stBtn = el("button", { type: "button", text: "Run self-test" });
    stBtn.addEventListener("click", () => runSelfTest(u.name, stBtn));
    stBox.appendChild(stBtn);
    box.appendChild(stBox);
    frag.appendChild(box);
  }
  panel.replaceChildren(frag);
  _controlBuiltKey = builtOk ? key : null;  // retry next poll if anything failed
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
  return { node: vars, ok: res.ok };  // ok feeds renderControl's cache-key commit
}

async function runCommand(ups, command) {
  showError("");
  const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/command",
    { method: "POST", body: JSON.stringify({ command }) });
  if (!res.ok) showError("Command failed: " + ((res.data && res.data.error && res.data.error.message) || res.status));
  else setStatus("Ran " + command + " on " + ups);
}

async function runSelfTest(ups, btn) {
  // A self-test is a non-idempotent hardware action; disable the button while
  // the POST is in flight so a double-click can't enqueue several tests.
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  showError("");
  try {
    const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/self-test",
      { method: "POST", body: "{}" });
    if (!res.ok) {
      showError("Self-test failed: " +
        ((res.data && res.data.error && res.data.error.message) || res.status));
    } else {
      setStatus("Self-test issued on " + ups);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
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
  const authed = !!token() && authEnabled;
  // Hide Sign-in when already signed in OR when the server has auth disabled
  // (login would just 404 with "Authentication is disabled").
  document.getElementById("loginBtn").hidden = authed || !authEnabled;
  document.getElementById("logoutBtn").hidden = !authed;
  const who = document.getElementById("who");
  who.hidden = !authed;
  if (authed) who.textContent = "Signed in";
  else who.textContent = "";
}

function clearAuthState() {
  setToken("");
  selectedEvents = new Set();
  _controlBuiltKey = null;
  const control = document.getElementById("control-section");
  if (control) control.hidden = true;
  const panel = document.getElementById("control-panel");
  if (panel) panel.replaceChildren();
  const tab = document.getElementById("tab-control");
  if (tab) tab.hidden = true;
  // If the operator was on the now-hidden Control tab, fall back to Overview
  // (and re-sync the hash so the URL doesn't keep pointing at #control).
  if (activeTab === "control") selectTab("overview", { updateHash: true });
  refreshAuthUI();
  applyEventFilters();
}

// Learn whether auth is enabled server-side. This route stays open even when
// read endpoints require credentials, so the login form remains reachable.
async function loadAuthState() {
  const res = await api("/api/v1/auth/state");
  authEnabled = !!(res.ok && res.data && res.data.enabled);
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
  clearAuthState(); refresh();
}

// ----- tabs (v6.1) ---------------------------------------------------------
// Real ARIA tabs: roving tabindex, arrow-key nav, and URL-hash routing so views
// are linkable. The 10s refresh redraws only the active tab's chart/widgets.

const TAB_IDS = ["overview", "power", "battery", "energy", "events", "control", "shutdown", "config"];
let activeTab = "overview";

function tabButtons() {
  return TAB_IDS.map((id) => document.getElementById("tab-" + id)).filter(Boolean);
}

function selectTab(name, opts) {
  opts = opts || {};
  const requested = name;
  if (!TAB_IDS.includes(name)) name = "overview";
  let btn = document.getElementById("tab-" + name);
  // A hidden tab (e.g. Control when signed out) is not selectable.
  if (!btn || btn.hidden) { name = "overview"; btn = document.getElementById("tab-overview"); }
  const fellBack = name !== requested;
  activeTab = name;
  TAB_IDS.forEach((id) => {
    const b = document.getElementById("tab-" + id);
    const panel = document.getElementById("panel-" + id);
    const selected = id === name;
    if (b) {
      b.setAttribute("aria-selected", selected ? "true" : "false");
      b.tabIndex = selected ? 0 : -1;
    }
    if (panel) panel.hidden = !selected;
  });
  if (opts.focus && btn) btn.focus();
  // Keep the hash in sync. Poll-driven calls don't pass updateHash, but a
  // FALLBACK (requested a hidden tab, landed on overview) must always re-sync
  // the hash + focus so the URL and focused tab match the visible panel.
  if ((opts.updateHash || fellBack) && ("#" + name) !== location.hash) {
    location.hash = name;
  }
  if (fellBack && btn && document.activeElement
      && document.activeElement.getAttribute
      && document.activeElement.getAttribute("role") === "tab") {
    btn.focus();
  }
  // An explicit (user) switch lands at the top of the freshly-shown panel
  // instead of inheriting the previous tab's scroll position.
  if (opts.updateHash) window.scrollTo(0, 0);
  onTabActivated(name);
}

// Draw/refresh whatever the freshly-activated tab needs from the latest data.
// ----- shutdown plan (DAG view) --------------------------------------------
// Render the read-only structured shutdown plan as an ordered flow. Enabled
// phases are prominent; skipped phases are muted with their reason. Parallel
// phases lay their steps out side by side.
// Populate the Shutdown-tab UPS selector from the live UPS list (remote UPSes
// flagged), keeping the current pick. Hidden for a single-UPS deployment. Each
// UPS/group has its own plan, so this is how remote-only + multi-UPS plans
// become reachable. Returns the selected UPS name.
function populateShutdownUpsSelect() {
  const sel = document.getElementById("shutdown-ups");
  const ctl = document.getElementById("shutdown-controls");
  const rows = lastUpsRows;
  if (!sel) return rows[0] && rows[0].name;
  const prev = sel.value;
  const isLocal = {};
  ((cfgSnapshot && cfgSnapshot.ups) || []).forEach((c) => { isLocal[c.name] = c.isLocal; });
  sel.replaceChildren();
  rows.forEach((u) => sel.appendChild(el("option", { value: u.name,
    text: (u.label || u.name) + (isLocal[u.name] === false ? " (remote)" : "") })));
  if (prev && rows.some((u) => u.name === prev)) sel.value = prev;
  if (ctl) ctl.hidden = rows.length <= 1;
  return sel.value || (rows[0] && rows[0].name);
}

// "What triggers this sequence" — a redundancy-group quorum loss (coordinated),
// or a standalone UPS's own low-battery / forced shutdown. Built from the
// redundancy-group data the dashboard already holds (lastGroups).
function shutdownTriggerNodes(name, plan) {
  const groups = lastGroups.filter((g) => (g.upsSources || []).includes(name));
  const coord = plan && plan.coordinatorMode ? " — coordinator-run" : "";
  if (groups.length) {
    return groups.map((g) => el("div", { class: "sd-trigger" }, [
      icon("alert"),
      el("span", { text: "Triggers when group “" + g.name + "” drops below "
        + g.minHealthy + " of " + (g.upsSources || []).length + " healthy" + coord }),
    ]));
  }
  return [el("div", { class: "sd-trigger" }, [icon("alert"),
    el("span", { text: "Triggers when this UPS reaches low battery or a forced "
      + "shutdown (FSD)" + coord })])];
}

let _sdGen = 0;
async function renderShutdownPlan() {
  const host = document.getElementById("shutdown-plan");
  if (!host) return;
  const name = populateShutdownUpsSelect();
  if (!name) {
    host.replaceChildren(el("p", { class: "chart-note", text: "No UPS data yet." }));
    return;
  }
  const myGen = ++_sdGen;
  const res = await api("/api/v1/ups/" + encodeURIComponent(name) + "/shutdown-plan");
  if (myGen !== _sdGen) return;
  const plan = res.ok && res.data && res.data.plan;
  if (!plan) {
    host.replaceChildren(el("p", { class: "chart-note",
      text: "Shutdown plan unavailable." }));
    return;
  }
  host.replaceChildren();
  shutdownTriggerNodes(name, plan).forEach((nd) => host.appendChild(nd));
  const intro = el("p", { class: "chart-note" },
    [el("span", { text: "What runs when a power-loss shutdown is triggered, top "
      + "to bottom. " })]);
  if (plan.totalEstimateS) intro.appendChild(
    el("span", { class: "badge", text: "~" + Math.round(plan.totalEstimateS) + "s est." }));
  if (plan.dryRun) intro.appendChild(el("span", { class: "badge warn", text: "dry-run" }));
  if (plan.delegated) intro.appendChild(el("span", { class: "badge info", text: "delegated" }));
  if (plan.coordinatorMode) intro.appendChild(el("span", { class: "badge info", text: "coordinator" }));
  host.appendChild(intro);
  if (plan.note) host.appendChild(el("p", { class: "chart-note", text: plan.note }));

  const flow = el("div", { class: "sd-flow" });
  let n = 0;
  (plan.phases || []).forEach((p) => {
    n += 1;
    const node = el("div", { class: "sd-node" + (p.enabled ? "" : " sd-skip") });
    const head = el("div", { class: "sd-head" }, [
      el("span", { class: "sd-num", text: String(n) }),
      el("span", { class: "sd-ico" + (p.enabled ? "" : " off") },
        [icon(SD_PHASE_ICONS[p.id] || "power")]),
      el("span", { class: "sd-title", text: p.title }),
    ]);
    if (p.enabled && p.mode === "parallel") {
      head.appendChild(el("span", { class: "sd-mode", text: "⇉ parallel" }));
    }
    if (p.enabled && p.estimateS != null) {
      head.appendChild(el("span", { class: "sd-est", text: "~" + Math.round(p.estimateS) + "s" }));
    }
    if (!p.enabled) {
      // "delegated to host" / "non-local group" read as an accent tag, not flat
      // gray — they're meaningful routing, not just "off".
      const routed = p.skipped === "delegated to host" || p.skipped === "non-local group";
      head.appendChild(el("span", { class: "badge" + (routed ? " info" : ""),
        text: p.skipped || "skipped" }));
    }
    node.appendChild(head);
    if (p.enabled && (p.steps || []).length) {
      const steps = el("div", { class: "sd-steps" + (p.mode === "parallel" ? " sd-parallel" : "") });
      p.steps.forEach((s) => {
        const st = el("div", { class: "sd-step" },
          [el("div", { class: "sd-step-label", text: s.label })]);
        if (s.detail) st.appendChild(el("div", { class: "sd-step-detail", text: s.detail }));
        steps.appendChild(st);
      });
      node.appendChild(steps);
    }
    flow.appendChild(node);
  });
  host.appendChild(flow);
}

function onTabActivated(name) {
  if (name === "power") { renderLineQuality(); if (charts.power) charts.power.load(); }
  else if (name === "battery") { renderBatteryHealthTab(); if (charts.battery) charts.battery.load(); }
  else if (name === "energy") { renderEnergyTab(); if (charts.energy) charts.energy.load(); }
  else if (name === "shutdown") renderShutdownPlan();
  else if (name === "config") renderConfigTab();
}

function initTabs() {
  const list = document.getElementById("tabs");
  if (!list) return;
  tabButtons().forEach((btn) => {
    // Prepend each tab's inline-SVG icon (single source of truth in TAB_ICONS).
    const name = TAB_ICONS[btn.dataset.tab];
    if (name) btn.insertBefore(icon(name), btn.firstChild);
    btn.addEventListener("click", () =>
      selectTab(btn.dataset.tab, { updateHash: true }));
  });
  // Arrow-key navigation across the visible tabs (WAI-ARIA tabs pattern).
  list.addEventListener("keydown", (ev) => {
    const visible = tabButtons().filter((b) => !b.hidden);
    const idx = visible.findIndex((b) => b.dataset.tab === activeTab);
    if (idx < 0) return;
    let next = null;
    if (ev.key === "ArrowRight" || ev.key === "ArrowDown") next = visible[(idx + 1) % visible.length];
    else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") next = visible[(idx - 1 + visible.length) % visible.length];
    else if (ev.key === "Home") next = visible[0];
    else if (ev.key === "End") next = visible[visible.length - 1];
    if (next) {
      ev.preventDefault();
      selectTab(next.dataset.tab, { updateHash: true, focus: true });
    }
  });
  // Hash routing: load the requested tab, and follow back/forward navigation.
  const fromHash = () => {
    const name = (location.hash || "").replace(/^#/, "");
    selectTab(TAB_IDS.includes(name) ? name : "overview");
  };
  window.addEventListener("hashchange", fromHash);
  fromHash();
}

// ----- polling -----

function setStatus(msg) {
  document.getElementById("status-line").textContent =
    msg + " · " + new Date().toLocaleTimeString();
}

async function refresh() {
  // One shared config + remote-health snapshot per cycle so the drill-down reads
  // from memory instead of firing per-card requests.
  const [authState, cfg, rh] = await Promise.all([
    api("/api/v1/auth/state"), api("/api/v1/config"), api("/api/v1/remote-health"),
  ]);
  authEnabled = !!(authState.ok && authState.data && authState.data.enabled);
  // Only sign out when the server explicitly reports auth is OFF. A transient
  // /api/v1/auth/state failure leaves authState.ok false (and authEnabled
  // false), which must NOT be mistaken for "auth disabled server-side" — that
  // would log the operator out on every network blip.
  if (authState.ok && authState.data && authState.data.enabled === false && token()) {
    clearAuthState();
  }
  if (cfg.ok && cfg.data) {
    cfgSnapshot = cfg.data;
    // If we hold a token but the server treats us as anonymous (sanitized
    // config), the session was invalidated server-side — e.g. the account was
    // deleted. Reads stay open (no 401 to trip the api() handler), so detect it
    // here and sign out locally instead of showing a stale "Signed in".
    if (token() && cfg.data.detail === "sanitized") {
      clearAuthState();
    }
    refreshAuthUI();
  }
  if (rh.ok) remoteHealthSnapshot = (rh.data && rh.data.servers) || [];

  const ups = await api("/api/v1/ups");
  if (ups.ok) {
    renderUps(ups.data); renderControl(ups.data); renderBanner(); showError("");
  } else if (ups.status === 0) {
    showError("⚠️  Connection lost — retrying…");  // L14: network/daemon down
  } else if (ups.status !== 401) {
    showError("Could not load UPS status (HTTP " + ups.status + ")");
  }
  await loadEvents();        // merges fresh recent events into the accumulated list
  // Redraw only the active tab's chart/widgets (the others redraw on activate).
  onTabActivated(activeTab);
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

  // Charts (one per data-bearing tab). Power carries voltage bands + event
  // overlays; Battery + Energy carry event overlays.
  charts.power = makeChart({ hostId: "power-graph", upsSelId: "power-ups",
    metricSelId: "power-metric", rangeSelId: "power-range", bands: true,
    events: true, noteId: "power-note" });
  charts.battery = makeChart({ hostId: "battery-graph", upsSelId: "battery-ups",
    metricSelId: "battery-metric", rangeSelId: "battery-range", events: true });
  charts.energy = makeEnergyChart({ hostId: "energy-graph", upsSelId: "energy-ups",
    rangeSelId: "energy-range" });
  Object.values(charts).forEach((c) => c.observe());

  // Shutdown-tab UPS selector: re-render the plan for the chosen UPS/group.
  const sdSel = document.getElementById("shutdown-ups");
  if (sdSel) sdSel.addEventListener("change", renderShutdownPlan);

  // Shared Range across Power/Battery/Energy: changing one applies to all three
  // (they carry identical options) and reloads the active chart, so switching
  // tabs keeps the chosen window instead of snapping back to the default.
  const RANGE_SELECTS = ["power-range", "battery-range", "energy-range"];
  RANGE_SELECTS.forEach((id) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.addEventListener("change", () => {
      RANGE_SELECTS.forEach((other) => {
        const s = document.getElementById(other);
        if (s && s.value !== node.value) s.value = node.value;
      });
      onTabActivated(activeTab);  // redraw whichever chart is showing
    });
  });
  // Shared UPS selection across Power/Battery/Energy (mirrors the Range sync) so
  // switching tabs keeps the same UPS in view.
  CHART_UPS_SELECTS.forEach((id) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.addEventListener("change", () => {
      CHART_UPS_SELECTS.forEach((other) => {
        const s = document.getElementById(other);
        if (s && s.value !== node.value) s.value = node.value;
      });
      onTabActivated(activeTab);   // redraw whichever chart is showing
    });
  });
  // Metric selects reload only their own chart.
  [["power-metric", "power"], ["battery-metric", "battery"]].forEach(([id, chart]) => {
    const node = document.getElementById(id);
    if (node) node.addEventListener("change", () => charts[chart] && charts[chart].load());
  });

  // Discrete filter changes can resize the table a lot; reset to the top of the
  // page so the operator isn't left stranded mid-table. (Text input + the 10s
  // poll deliberately do NOT scroll.)
  const scrollTop = () => window.scrollTo(0, 0);
  document.getElementById("event-source-filter").addEventListener("change", () => {
    applyEventFilters(); scrollTop();
  });
  // Tier is the primary, window-independent event filter.
  document.getElementById("event-tier").addEventListener("change", () => {
    applyEventFilters(); scrollTop();
  });
  document.getElementById("event-type-filter").addEventListener("change", () => {
    updateEventTypeSummary();
    applyEventFilters();
    scrollTop();
  });
  document.getElementById("event-text-filter").addEventListener("input", applyEventFilters);
  document.getElementById("event-range").addEventListener("change", () => {
    resetEvents(); scrollTop();
  });
  document.getElementById("event-load-older").addEventListener("click", loadOlderEvents);
  document.getElementById("event-delete").addEventListener("click", deleteSelected);
  document.getElementById("event-sort-time").addEventListener("click", toggleEventSort);
  document.getElementById("detail-close").addEventListener("click", closeDetail);
  // Click anywhere on the backdrop (outside the card) closes the detail modal,
  // not just the ✕.
  document.getElementById("detail-modal").addEventListener("click", (ev) => {
    if (ev.target.id === "detail-modal") closeDetail();
  });
  // Esc closes whichever modal is open.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!document.getElementById("detail-modal").hidden) closeDetail();
    if (!document.getElementById("login-modal").hidden) closeLogin();
  });
  // Close the event-type dropdown (<details>) when clicking anywhere outside it,
  // instead of forcing a second click on the summary.
  document.addEventListener("click", (ev) => {
    document.querySelectorAll("details.event-type-picker[open]").forEach((d) => {
      if (!d.contains(ev.target)) d.removeAttribute("open");
    });
  });
  initTabs();
  refresh();
  setInterval(refresh, 10000);
}

document.addEventListener("DOMContentLoaded", init);
