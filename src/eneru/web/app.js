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
    card.addEventListener("click", () => openDetail(u.name));
    card.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDetail(u.name); }
    });
    wrap.appendChild(card);
  });
  populateChartUpsSelects(rows);

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

// Build the v6.1 battery-health rows shared by the detail modal and the Battery
// tab. "unknown" is shown honestly rather than a fake high score.
function batteryHealthRows(bh) {
  const rows = [
    detailRow("Score", bh.score != null ? Math.round(bh.score) + "/100" : "unknown"),
    detailRow("Confidence",
      bh.confidence != null ? Math.round(bh.confidence * 100) + "%" : null),
  ];
  if (bh.replacementDaysRemaining != null) {
    rows.push(detailRow("Replace in", "~" + Math.round(bh.replacementDaysRemaining) + " days"));
  }
  if ((bh.availableTerms || []).length) {
    rows.push(detailRow("Terms", bh.availableTerms.join(", ")));
  }
  return rows;
}

// Build the v6.1 energy rows shared by the detail modal and the Energy tab.
function energyCostConfigured(en) {
  // The server includes the cost fields whenever energy.cost_per_kwh is set —
  // even if the computed value is unknown — so their PRESENCE (not truthiness)
  // tells us cost tracking is on.
  return en && ("todayCost" in en || "monthCost" in en);
}

function energyRows(en) {
  const rows = [
    detailRow("Today", en.todayKwh != null ? en.todayKwh.toFixed(3) + " kWh" : "unknown"),
    detailRow("Month", en.monthKwh != null ? en.monthKwh.toFixed(3) + " kWh" : "unknown"),
  ];
  // When cost is configured, always show the cost rows (so "unknown" reads as
  // "tracked, but no power data" rather than silently hiding cost + nagging to
  // set a price that is already set).
  if (energyCostConfigured(en)) {
    rows.push(detailRow("Today cost", en.todayCostFormatted || "unknown"));
    rows.push(detailRow("Month cost", en.monthCostFormatted || "unknown"));
  }
  if (en.estimated) rows.push(detailRow("Note", "estimated (no real-power reading)"));
  if (en.partial) rows.push(detailRow("Coverage", "partial (data gaps in window)"));
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
  if (crit) {
    banner.className = "banner crit";
    const why = crit.triggerReason ? (": " + crit.triggerReason) : "";
    banner.textContent = "⚠️  Shutdown imminent — " +
      (crit.label || crit.name) + " is on low battery" + why;
    banner.hidden = false;
  } else if (warn) {
    banner.className = "banner warn";
    banner.textContent = "🔋  On battery — " + (warn.label || warn.name) +
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

// Set once we've applied the tier-1 default selection, so later polls preserve
// whatever the operator has since chosen.
let _eventTypeDefaultApplied = false;

function updateEventTypeFilter(rows) {
  const box = document.getElementById("event-type-filter");
  let selected = selectedEventTypes();
  const types = Array.from(new Set((rows || [])
    .map((e) => e.eventType || e.event || "")
    .filter((v) => v))).sort();
  // First time we actually have event types, default to selecting only the
  // tier-1 events so the table isn't drowned in routine daemon-start / upgrade
  // rows. The operator can tick the rest on from there.
  if (!_eventTypeDefaultApplied && types.length) {
    const tier1 = types.filter(isTier1Event);
    if (tier1.length) selected = new Set(tier1);
    _eventTypeDefaultApplied = true;
  }
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
      && (types.size === 0 || types.has(eventType))
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

// Marker color class by event type, for the chart overlays.
function eventMarkerClass(type) {
  const t = (type || "").toUpperCase();
  if (t.includes("SHUTDOWN") || t.includes("FSD") || t === "ON_BATTERY"
      || t.includes("BROWNOUT") || t.includes("CRITICAL")) return "ev-crit";
  if (t.includes("OVER_VOLTAGE") || t.includes("OVERLOAD") || t.includes("BYPASS")
      || t.includes("AVR") || t.includes("WARNING") || t.includes("ANOMALY")) return "ev-warn";
  return "ev-info";
}

// "Tier-1" = the power events an operator actually cares about on a chart or in
// the default events view. Routine lifecycle rows (daemon start/stop, upgrades,
// config reloads, AVR cycling, suppressed flaps) are excluded so the markers and
// the default table aren't drowned in noise.
const TIER1_EVENT_PATTERNS = [
  "ON_BATTERY", "POWER_RESTORED", "LOW_BATTERY", "SHUTDOWN", "FSD",
  "OVER_VOLTAGE", "BROWNOUT", "OVERLOAD_ACTIVE", "BYPASS_MODE_ACTIVE",
  "CONNECTION_LOST", "CONNECTION_RESTORED", "REPLACE_BATTERY",
  "BATTERY_REPLACEMENT", "SELF_TEST", "ANOMALY",
];
function isTier1Event(type) {
  const u = (type || "").toUpperCase();
  return TIER1_EVENT_PATTERNS.some((p) => u.includes(p));
}

// Human-readable tooltip for a chart event marker.
function eventDescription(e) {
  const type = e.eventType || e.event || "event";
  const when = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
  const detail = e.detail || e.details || "";
  return type + (when ? (" @ " + when) : "") + (detail ? ("\n" + detail) : "");
}

// Append one event marker (vertical guide + dot) wrapped in a <g> whose <title>
// covers the whole group, plus a wide transparent hit line, so hovering anywhere
// along the guide shows the tooltip (the bare 3px dot was nearly impossible to
// hit). `e` carries the event; cls is the color class.
function appendEventMarker(svg, e, ex, top, bottom) {
  const cls = eventMarkerClass(e.eventType || e.event);
  const g = document.createElementNS(SVG_NS, "g");
  const title = document.createElementNS(SVG_NS, "title");
  title.textContent = eventDescription(e);
  g.appendChild(title);
  const mkline = (cssClass) => {
    const l = document.createElementNS(SVG_NS, "line");
    l.setAttribute("x1", ex); l.setAttribute("y1", top);
    l.setAttribute("x2", ex); l.setAttribute("y2", bottom);
    l.setAttribute("class", cssClass);
    l.setAttribute("vector-effect", "non-scaling-stroke");
    g.appendChild(l);
  };
  mkline("ev-hit");                 // wide transparent hover target
  mkline("ev-line " + cls);         // the visible guide
  const dot = document.createElementNS(SVG_NS, "circle");
  dot.setAttribute("cx", ex); dot.setAttribute("cy", String(top + 3));
  dot.setAttribute("r", "4");
  dot.setAttribute("class", "ev-dot " + cls);
  g.appendChild(dot);
  svg.appendChild(g);
}

function isVoltageMetric(metric) {
  return metric === "voltage" || metric === "output_voltage"
      || metric === "battery_voltage";
}

// Draw `series` (a /history payload) into the host element, with optional
// `bands` (voltage thresholds) and `events` (overlay markers).
function drawChart(hostId, series, options) {
  options = options || {};
  const host = document.getElementById(hostId);
  if (!host) return;
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

  // Voltage threshold band (reference overlay of the CURRENT config, not
  // historical truth — labelled as such by the caller's note line).
  if (wantBand && bands.low != null && bands.high != null) {
    const yHigh = y(bands.high), yLow = y(bands.low);
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", pad); rect.setAttribute("y", yHigh.toFixed(1));
    rect.setAttribute("width", (W - pad - 5).toFixed(1));
    rect.setAttribute("height", Math.max(0, yLow - yHigh).toFixed(1));
    rect.setAttribute("class", "band");
    svg.appendChild(rect);
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
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", d); path.setAttribute("class", "plot");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);

  // Min/max axis labels.
  const fmt = (metric === "runtime") ? formatRuntimeSeconds : (v) => v.toFixed(0);
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
      let eq = "limit=1000";
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
  plot("loadPct", loadS, "plot plot-load");
  plot("watts", wattS, "plot plot-watts");

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
  if (loadS) legend("Load", "plot-load", loadS.mx, "%");
  if (wattS) legend("Power", "plot-watts", wattS.mx, "W");
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
  const head = [el("h3", { text: title })];
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
    const cardRows = [];
    if (bh) {
      const scoreTxt = bh.score != null ? Math.round(bh.score) + "/100" : "unknown";
      cardRows.push(...batteryHealthRows(bh));
      const card = widgetCard(u.label || u.name, cardRows,
        { badge: scoreTxt, badgeClass: scoreClass(bh.score) });
      if (st) {
        card.appendChild(el("div", { class: "row" }, [
          el("span", { text: "Last self-test" }),
          el("b", { text: (st.result || "unknown") + (st.date ? (" · " + st.date) : "") }),
        ]));
      }
      wrap.appendChild(card);
    } else {
      wrap.appendChild(widgetCard(u.label || u.name,
        [el("p", { class: "chart-note", text: "Battery health not available." })]));
    }
  });
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
      wrap.appendChild(widgetCard(u.label || u.name, energyRows(en)));
    } else {
      wrap.appendChild(widgetCard(u.label || u.name,
        [el("p", { class: "chart-note",
          text: "Energy tracking not available (no samples yet)." })]));
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

// ----- config tab ----------------------------------------------------------

function configKv(label, value) {
  return el("div", { class: "row" }, [
    el("span", { text: label }),
    el("b", { text: (value === undefined || value === null || value === "")
      ? "—" : String(value) }),
  ]);
}

function renderConfigTab() {
  const body = document.getElementById("config-body");
  if (!body) return;
  body.replaceChildren();
  const cfg = cfgSnapshot;
  if (!cfg) { body.appendChild(el("p", { class: "chart-note", text: "Loading…" })); return; }
  const cards = [];
  (cfg.ups || []).forEach((c) => {
    cards.push(widgetCard(c.label || c.name, [
      configKv("Name", c.name),
      configKv("Local host", c.isLocal ? "yes" : "no"),
      configKv("Remote servers", (c.remoteServers || []).length),
    ]));
  });
  const features = [];
  if (cfg.nutControl) features.push(configKv("UPS control",
    cfg.nutControl.enabled ? "enabled" : "disabled"));
  if (cfg.detail === "sanitized") {
    features.push(el("p", { class: "chart-note",
      text: "Sign in to see the full configuration." }));
  }
  if (features.length) cards.push(widgetCard("Features", features));
  const grid = el("div", { class: "cards" }, cards);
  body.replaceChildren(grid);
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

const TAB_IDS = ["overview", "power", "battery", "energy", "events", "control", "config"];
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
function onTabActivated(name) {
  if (name === "power" && charts.power) charts.power.load();
  else if (name === "battery") { renderBatteryHealthTab(); if (charts.battery) charts.battery.load(); }
  else if (name === "energy") { renderEnergyTab(); if (charts.energy) charts.energy.load(); }
  else if (name === "config") renderConfigTab();
}

function initTabs() {
  const list = document.getElementById("tabs");
  if (!list) return;
  tabButtons().forEach((btn) => {
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

  document.getElementById("event-source-filter").addEventListener("change", applyEventFilters);
  document.getElementById("event-type-filter").addEventListener("change", () => {
    updateEventTypeSummary();
    applyEventFilters();
  });
  document.getElementById("event-text-filter").addEventListener("input", applyEventFilters);
  document.getElementById("event-range").addEventListener("change", resetEvents);
  document.getElementById("event-load-older").addEventListener("click", loadOlderEvents);
  document.getElementById("event-delete").addEventListener("click", deleteSelected);
  document.getElementById("event-sort-time").addEventListener("click", toggleEventSort);
  document.getElementById("detail-close").addEventListener("click", closeDetail);
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
