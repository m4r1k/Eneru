"use strict";
// Eneru dashboard — a thin client over the REST API. No third-party code; all
// logic stays server-side. The auth token (session or API key) lives in
// sessionStorage and is sent as a Bearer header, so there is no cookie and thus
// no CSRF surface.

const TOKEN_KEY = "eneru_token";
const SVG_NS = "http://www.w3.org/2000/svg";
let lastEvents = [];
let knownEventSources = [];
// Whether the server has API auth enabled. Learned from /api/v1/config at start;
// when false there is nothing to sign into, so the Sign-in button stays hidden.
let authEnabled = false;

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

function renderUps(payload) {
  const wrap = document.getElementById("ups-cards");
  wrap.replaceChildren();
  const rows = (payload && payload.ups) || [];
  const sel = document.getElementById("graph-ups");
  const prev = sel.value;
  sel.replaceChildren();
  rows.forEach((u) => {
    const charge = parseFloat(u.batteryCharge);
    const card = el("div", { class: "card" }, [
      el("h3", { text: u.label || u.name }),
      el("div", { class: "row" }, [
        el("span", { text: "Status" }),
        el("span", { class: "badge " + statusClass(u.status), text: u.status || "—" }),
      ]),
      el("div", { class: "row" }, [el("span", { text: "Battery" }),
        el("b", { text: isNaN(charge) ? "—" : charge + "%" })]),
      el("div", { class: "bar" }, [
        el("span", null, []),
      ]),
      el("div", { class: "row" }, [el("span", { text: "Runtime" }),
        el("b", { text: u.runtime != null ? u.runtime + "s" : "—" })]),
      el("div", { class: "row" }, [el("span", { text: "Load" }),
        el("b", { text: u.load != null ? u.load + "%" : "—" })]),
    ]);
    if (!isNaN(charge)) card.querySelector(".bar > span").style.width =
      Math.max(0, Math.min(100, charge)) + "%";
    wrap.appendChild(card);
    sel.appendChild(el("option", { value: u.name, text: u.label || u.name }));
  });
  if (prev) sel.value = prev;

  const groups = (payload && payload.redundancyGroups) || [];
  const gsec = document.getElementById("groups-section");
  const gwrap = document.getElementById("group-cards");
  gwrap.replaceChildren();
  gsec.hidden = groups.length === 0;
  groups.forEach((g) => {
    gwrap.appendChild(el("div", { class: "card" }, [
      el("h3", { text: g.name }),
      el("div", { class: "row" }, [el("span", { text: "Sources" }),
        el("b", { text: String((g.upsSources || []).length) })]),
      el("div", { class: "row" }, [el("span", { text: "Min healthy" }),
        el("b", { text: String(g.minHealthy) })]),
    ]));
  });
  updateEventSourceFilter(rows, groups);
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

async function loadEvents(beforeCursor) {
  let q = "limit=200";
  const from = eventRangeFrom();
  if (from !== null && !beforeCursor) q += "&from=" + from;
  if (beforeCursor) q += "&before=" + encodeURIComponent(beforeCursor);
  const res = await api("/api/v1/events?" + q);
  if (res.ok && res.data) mergeEvents(res.data.events);
}

async function loadOlderEvents() {
  const oldest = lastEvents[0];   // ascending sort -> [0] is the oldest shown
  if (!oldest) { await loadEvents(); return; }
  await loadEvents(oldest.ts + "_" + (oldest.id || 0));
}

function resetEvents() {
  lastEvents = [];
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

function applyEventFilters() {
  const body = document.querySelector("#events tbody");
  body.replaceChildren();
  const source = document.getElementById("event-source-filter").value;
  const type = document.getElementById("event-type-filter").value;
  const text = document.getElementById("event-text-filter").value.trim().toLowerCase();
  const rows = lastEvents.filter((e) => {
    const eventType = e.eventType || e.event || "";
    const detail = (e.detail || e.details || "").toLowerCase();
    return eventMatchesSource(e, source)
      && (!type || eventType === type)
      && (!text || detail.includes(text));
  });
  if (rows.length === 0) {
    body.appendChild(el("tr", null, [el("td", { colspan: "3", text: "No events." })]));
    return;
  }
  rows.forEach((e) => {
    const ts = e.ts ? new Date(e.ts * 1000).toLocaleString() : "—";
    body.appendChild(el("tr", null, [
      el("td", { text: ts }),
      el("td", { text: e.eventType || e.event || "" }),
      el("td", { text: e.detail || e.details || "" }),
    ]));
  });
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
  const cfg = await api("/api/v1/config");
  const nutEnabled = cfg.data && cfg.data.nutControl && cfg.data.nutControl.enabled;
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
  setToken(""); refreshAuthUI(); refresh();
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
  const ups = await api("/api/v1/ups");
  if (ups.ok) { renderUps(ups.data); renderControl(ups.data); showError(""); }
  else if (ups.status !== 401) showError("Could not load UPS status (HTTP " + ups.status + ")");
  await loadEvents();        // merges fresh recent events into the accumulated list
  await loadGraph();
  setStatus("Updated");
}

async function init() {
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
  observeGraphResize();
  refresh();
  setInterval(refresh, 10000);
}

document.addEventListener("DOMContentLoaded", init);
