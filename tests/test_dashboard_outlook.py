"""v6.2 UX round: dashboard "what happens next", role, freshness, vocabulary.

The pure helpers in app.js (bannerModel, outlookLine, statusInfo, upsRole,
replacementEstimate, formatAge, pageTitle, ...) are exercised in a no-browser
node harness: the whole app.js is loaded behind a tiny DOM stub (nothing in
these helpers touches the DOM), then each helper is called with /api/v1/ups
shaped rows built per the backend contract (eneru.outlook / eneru.utils).
"""

import json
import shutil
import subprocess
import textwrap

import pytest

from conftest import make_api_handler

NODE = shutil.which("node")


def _asset(config, path):
    return make_api_handler(config, path=path)._serve_static(path)[1].decode("utf-8")


def _run(config, body):
    js = _asset(config, "/app.js")
    stub = textwrap.dedent("""
        const document = { addEventListener() {}, getElementById() { return null; },
          querySelectorAll() { return []; }, body: { classList: { toggle() {} } } };
        const window = { addEventListener() {} };
        const sessionStorage = { getItem() { return ""; } };
        const localStorage = { getItem() { return null; }, setItem() {} };
    """)
    script = stub + js + "\n" + textwrap.dedent(body)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    return json.loads(result.stdout)


def _trigger(tid, label, state, *, eta=None, cond="", text=""):
    return {"id": tid, "label": label, "enabled": True, "state": state,
            "etaSeconds": eta, "condition": cond, "text": text}


ROLE_LOCAL = {"kind": "local", "label": "Powers this host", "hasShutdownActions": True,
              "shutsDownLocalHost": True, "remoteServers": 1, "redundancyGroups": []}
ROLE_MONITOR = {"kind": "monitor-only", "label": "Monitoring only",
                "hasShutdownActions": False, "remoteServers": 0, "redundancyGroups": []}
ROLE_MEMBER = {"kind": "redundancy-member", "label": "Redundancy member (rack-a)",
               "hasShutdownActions": False, "remoteServers": 0,
               "redundancyGroups": ["rack-a"]}
ACTION = {"kind": "local-shutdown", "label": "Shuts down this host and 1 remote server"}


def _row(name, status, *, role, summary, triggers=(), tob_text=None, charge="62",
         trigger_active=False):
    return {"name": name, "label": name, "status": status, "batteryCharge": charge,
            "role": role, "statusSummary": summary, "timeOnBatteryText": tob_text,
            "triggerActive": trigger_active, "triggerReason": "",
            "triggerOutlook": {"triggers": list(triggers), "action": ACTION,
                               "summary": ""}}


OB = {"state": "on_battery", "label": "On battery", "severity": "warn", "blink": False,
      "detail": "Running on battery"}
LB = {"state": "low_battery", "label": "Low battery", "severity": "crit", "blink": False,
      "detail": "Battery low"}
TRIG = {"state": "trigger_active", "label": "Shutdown triggered", "severity": "crit",
        "blink": True, "detail": "Running on battery"}
FSD = {"state": "shutting_down", "label": "Shutting down", "severity": "crit",
       "blink": True, "detail": "Shutdown in progress"}


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_banner_is_role_aware(minimal_config):
    """H1/M2/M10: monitor-only never says imminent; trigger/FSD wording + title."""
    near = [_trigger("criticalRuntime", "Critical runtime", "ok", eta=121,
                     cond="runtime < 5m 0s"),
            _trigger("extendedTime", "Time on battery", "ok", eta=300,
                     cond="on battery > 20m 0s"),
            _trigger("depletionRate", "Fast battery drain", "ok", cond="drain > 15%/min")]
    fired = [_trigger("criticalRuntime", "Critical runtime", "fired", eta=0,
                      cond="runtime < 5m 0s", text="4m 40s now · fires below 5m 0s")]
    rows = {
        "apcLow": [_row("APC", "OB DISCHRG LB", role=ROLE_MONITOR, summary=LB, charge="9")],
        "labNear": [_row("Lab", "OB DISCHRG", role=ROLE_LOCAL, summary=OB,
                         triggers=near, tob_text="15m 0s")],
        "labTrigger": [_row("Lab", "OB DISCHRG", role=ROLE_LOCAL, summary=TRIG,
                            triggers=fired, trigger_active=True)],
        "labFsd": [_row("Lab", "OB FSD", role=ROLE_LOCAL, summary=FSD)],
        "memberOb": [_row("UPS2", "OB", role=ROLE_MEMBER, summary=OB, triggers=near,
                          tob_text="1m 0s")],
        "healthy": [_row("Lab", "OL", role=ROLE_LOCAL,
                         summary={"state": "online", "label": "On mains",
                                  "severity": "ok", "blink": False, "detail": ""})],
    }
    body = "const rows = " + json.dumps(rows) + ";\n" + """
        const group = {name: "rack-a", upsSources: ["UPS1", "UPS2"], minHealthy: 1,
          failingMembers: ["UPS2"], healthyCount: 1,
          outlook: {state: "at-risk", severity: "warn",
                    label: "1 more failure → group shutdown", action: "Shuts down 2 remote servers"}};
        const out = {};
        for (const k of Object.keys(rows)) {
          const m = bannerModel(rows[k], []);
          out[k] = m && {severity: m.severity, text: m.text, progress: m.progress,
                         title: pageTitle(m, false)};
        }
        const g = bannerModel(rows.memberOb, [group]);
        out.group = {severity: g.severity, text: g.text, more: g.more};
        out.staleTitle = pageTitle(null, true);
        out.nearLine = outlookLine(rows.labNear[0]);
        out.monitorLine = outlookLine(rows.apcLow[0]);
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["apcLow"]["severity"] == "warn"
    assert out["apcLow"]["text"] == (
        "APC (monitoring only) is on low battery — no action will be taken here.")
    assert "imminent" not in json.dumps(out)
    assert out["labNear"]["text"] == (
        "On battery — Lab for 15m 0s. Shutdown in ≈2m 1s (runtime < 5m 0s)")
    assert out["labNear"]["title"] == "⚠ On battery · Lab 62% — Eneru"
    assert out["labTrigger"]["severity"] == "crit"
    assert out["labTrigger"]["text"] == (
        "Shutdown triggered — Lab: critical runtime (4m 40s now · fires below 5m 0s)"
        " → Shuts down this host and 1 remote server")
    assert out["labTrigger"]["progress"] is True
    assert out["labFsd"] == {"severity": "crit", "text": "Shutdown in progress — Lab",
                             "progress": True, "title": "⛔ Shutting down · Lab — Eneru"}
    assert out["healthy"] is None
    # A redundancy member's own outage yields to the group's verdict.
    assert out["group"]["text"].startswith("Redundancy at risk — redundancy group rack-a")
    assert out["group"]["more"] == 1
    assert out["staleTitle"] == "(stale) Eneru"
    assert out["nearLine"] == ("Shutdown in ≈2m 1s (runtime < 5m 0s) · or on battery"
                               " > 20m 0s in ≈5m 0s · or drain > 15%/min")
    assert out["monitorLine"] is None


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_status_role_and_freshness_fallbacks(minimal_config):
    """Old daemons (no statusSummary/role/freshness) still get the same words."""
    body = """
        cfgSnapshot = {ups: [
          {name: "remote", isLocal: false, remoteServers: [{host: "nas", enabled: true}]},
          {name: "watch", isLocal: false, remoteServers: []},
        ]};
        const out = {
          ob: statusInfo({status: "OB DISCHRG"}),
          trig: statusInfo({status: "OB", triggerActive: true}).state,
          failsafe: statusInfo({status: "OB", connectionState: "FAILED"}).severity,
          stale: statusInfo({status: "OL", freshness: {stale: true, lastPollAt: 1}}).state,
          neverPolled: statusInfo({status: "OL", freshness: {stale: true, lastPollAt: null}}).state,
          badSeverity: statusInfo({statusSummary: {label: "X", severity: "weird"}}).severity,
          roles: ["local", "remote", "watch"].map((n) =>
            upsRole({name: n, isLocal: n === "local"}).kind),
          ages: [null, 1, 12, 240, 7200, 1209600].map(formatAge),
          worst: [worstSeverity(["ok", "crit", "warn"]), worstSeverity([])],
          tob: [timeOnBatteryText({timeOnBatteryText: "7m 0s", timeOnBattery: 420}),
                timeOnBatteryText({timeOnBattery: 1205})],
        };
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["ob"]["label"] == "On battery" and out["ob"]["severity"] == "warn"
    assert out["ob"]["blink"] is False
    assert out["trig"] == "trigger_active"
    assert out["failsafe"] == "crit"
    assert out["stale"] == "stale"
    assert out["neverPolled"] == "online"
    assert out["badSeverity"] == "warn"
    assert out["roles"] == ["local", "remote-only", "monitor-only"]
    assert out["ages"] == ["unknown", "just now", "12s ago", "4m ago", "2h ago", "14d ago"]
    assert out["worst"] == ["crit", "ok"]
    assert out["tob"] == ["7m 0s", "20m 5s"]


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_replacement_estimate_is_bounded(minimal_config):
    """H6: never "~204288 days"; the daemon's bounded text wins when sent."""
    body = """
        const out = {
          fmt: [0, 0.5, 12, 90, 800, 5000, null].map(formatReplacementEta),
          legacyHuge: replacementEstimate({replacementDaysRemaining: 204288,
                                           ageYears: 0.25, expectedLifeYears: 5}),
          legacyNear: replacementEstimate({replacementDaysRemaining: 40,
                                           ageYears: 1, expectedLifeYears: 5}),
          daemon: replacementEstimate({replacement: {days: 1500, text: "~4 yr",
                                                     source: "age", capped: true}}),
          unknown: replacementEstimate({replacement: {days: null, text: "unknown"}}),
          none: replacementEstimate({}),
        };
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["fmt"] == ["now", "<1 day", "~12 days", "~3 mo", "~2 yr", "> 10 yr",
                          "unknown"]
    assert out["legacyHuge"]["text"] == "~5 yr"
    assert out["legacyHuge"]["source"] == "age" and out["legacyHuge"]["capped"] is True
    assert out["legacyNear"] == {"days": 40, "text": "~40 days", "source": "trend",
                                 "capped": False}
    assert out["daemon"]["text"] == "~4 yr"
    assert out["unknown"] is None
    assert out["none"] is None


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_group_outlook_and_remote_staleness(minimal_config):
    """M9 group wording (daemon + fallback) and L4 old remote checks."""
    body = """
        const rows = [{name: "a", status: "OL", connectionState: "OK"},
                      {name: "b", status: "OB", connectionState: "OK"}];
        cfgSnapshot = {remoteHealth: {interval: 3600}};
        lastUpsRows = rows;
        const now = Date.now() / 1000;
        const out = {
          lost: groupOutlook({upsSources: ["a", "b"], minHealthy: 2}, rows),
          risk: groupOutlook({upsSources: ["a", "b"], minHealthy: 1}, rows).label,
          fine: groupOutlook({upsSources: ["a"], minHealthy: 0}, rows).label,
          daemon: groupOutlook({outlook: {state: "deferred", severity: "nope",
                                          label: "Quorum decision deferred"}}, rows),
          failing: groupFailingMembers({upsSources: ["a", "b", "gone"]}, rows),
          // an outage is on (b is OB): a 10-minute-old check is no longer "now"
          oldDuringOutage: remoteCheckIsOld({last_checked_at: now - 600}),
          fresh: remoteCheckIsOld({last_checked_at: now - 30}),
          never: remoteCheckIsOld({}),
        };
        lastUpsRows = [rows[0]];
        out.quietOld = remoteCheckIsOld({last_checked_at: now - 600});
        out.overdue = remoteCheckIsOld({last_checked_at: now - 4000});
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["lost"]["state"] == "quorum-lost" and out["lost"]["severity"] == "crit"
    assert out["risk"] == "1 more failure → group shutdown"
    assert out["fine"] == "Can lose 1 more member"
    assert out["daemon"]["severity"] == "warn"
    assert out["failing"] == ["b", "gone"]
    assert out["oldDuringOutage"] is True
    assert out["fresh"] is False and out["never"] is False
    assert out["quietOld"] is False and out["overdue"] is True


@pytest.mark.unit
def test_dashboard_v62_surfaces(minimal_config):
    """Static guards: live regions, newest-first events, CSP-safe, only
    shutdown states pulse, self-test is not red, clipped ticks re-anchored."""
    html = _asset(minimal_config, "/")
    js = _asset(minimal_config, "/app.js")
    css = _asset(minimal_config, "/style.css")
    assert 'id="banner-alert" class="sr-only" role="alert"' in html
    assert 'id="banner-status" class="sr-only" role="status"' in html
    assert 'id="stale-mark"' in html
    assert "Time ↓" in html
    assert 'let eventSortDirection = "desc";' in js
    assert 'if (t === "SELF_TEST_ON_BATTERY") return "ev-info";' in js
    assert 'const anchor = tx + half > W - 2 ? "end" : "middle";' in js
    assert "document.title = pageTitle(model, clientDataStale());" in js
    assert 'setStatus(upsOk);' in js and 'setStatus("Updated")' not in js
    # CSP (default-src 'self'): no inline style attributes or inline handlers.
    assert 'setAttribute("style"' not in js and "style:" not in js
    assert " onclick=" not in html
    # Only blink states animate; reduced-motion still neutralises it.
    assert ".badge.pulse { animation: eneru-pulse" in css
    assert css.count("animation:") == 1
    assert "prefers-reduced-motion" in css
    # H2: the fleet-row name column has a floor on a phone.
    assert "grid-template-columns: minmax(5.5rem, 1fr) auto;" in css
