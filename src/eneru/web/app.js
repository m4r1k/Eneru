"use strict";
// Eneru dashboard — a thin client over the REST API. No third-party code; all
// logic stays server-side. The auth token (session or API key) lives in
// sessionStorage and is sent as a Bearer header, so there is no cookie and thus
// no CSRF surface.

const TOKEN_KEY = "eneru_token";
const SVG_NS = "http://www.w3.org/2000/svg";

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
}

function renderEvents(payload) {
  const body = document.querySelector("#events tbody");
  body.replaceChildren();
  const rows = (payload && payload.events) || [];
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

function renderGraph(series) {
  const host = document.getElementById("graph");
  host.replaceChildren();
  const pts = (series && series.data) || [];
  const W = 800, H = 220, pad = 30;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const axis = (x1, y1, x2, y2) => {
    const l = document.createElementNS(SVG_NS, "line");
    l.setAttribute("x1", x1); l.setAttribute("y1", y1);
    l.setAttribute("x2", x2); l.setAttribute("y2", y2);
    l.setAttribute("class", "axis"); svg.appendChild(l);
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
    const cmds = el("div", { class: "cmds" });
    const res = await api("/api/v1/ups/" + encodeURIComponent(u.name) + "/commands");
    ((res.data && res.data.commands) || []).forEach((c) => {
      const btn = el("button", { type: "button", text: c });
      btn.addEventListener("click", () => runCommand(u.name, c));
      cmds.appendChild(btn);
    });
    if (!cmds.childNodes.length) cmds.appendChild(el("span", { class: "who", text: "No allowlisted commands." }));
    box.appendChild(cmds);
    panel.appendChild(box);
  }
}

async function runCommand(ups, command) {
  showError("");
  const res = await api("/api/v1/ups/" + encodeURIComponent(ups) + "/command",
    { method: "POST", body: JSON.stringify({ command }) });
  if (!res.ok) showError("Command failed: " + ((res.data && res.data.error && res.data.error.message) || res.status));
  else setStatus("Ran " + command + " on " + ups);
}

// ----- auth UI -----

function refreshAuthUI() {
  const authed = !!token();
  document.getElementById("loginBtn").hidden = authed;
  document.getElementById("logoutBtn").hidden = !authed;
  const who = document.getElementById("who");
  who.hidden = !authed;
  if (authed) who.textContent = "Signed in";
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
    const e = document.getElementById("login-error");
    e.textContent = "Sign in failed."; e.hidden = false;
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
  const res = await api("/api/v1/ups/" + encodeURIComponent(ups) +
    "/history?metric=" + encodeURIComponent(metric));
  renderGraph(res.ok ? res.data : null);
}

async function refresh() {
  const ups = await api("/api/v1/ups");
  if (ups.ok) { renderUps(ups.data); renderControl(ups.data); showError(""); }
  else if (ups.status !== 401) showError("Could not load UPS status (HTTP " + ups.status + ")");
  const events = await api("/api/v1/events?limit=50");
  if (events.ok) renderEvents(events.data);
  await loadGraph();
  setStatus("Updated");
}

function init() {
  refreshAuthUI();
  document.getElementById("loginBtn").addEventListener("click", openLogin);
  document.getElementById("logoutBtn").addEventListener("click", doLogout);
  document.getElementById("login-cancel").addEventListener("click", closeLogin);
  document.getElementById("login-form").addEventListener("submit", doLogin);
  document.getElementById("graph-ups").addEventListener("change", loadGraph);
  document.getElementById("graph-metric").addEventListener("change", loadGraph);
  refresh();
  setInterval(refresh, 10000);
}

document.addEventListener("DOMContentLoaded", init);
