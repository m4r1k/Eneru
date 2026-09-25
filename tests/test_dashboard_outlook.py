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


# ---------------------------------------------------------------------------
# F-184 / F-186 (release-review cycle 3): pin every banner branch, the
# client-side freshness math, the next-trigger line and the old-daemon role
# fallback, so a changed priority or wording fails here instead of shipping.
# ---------------------------------------------------------------------------

CONN_CRIT = {"state": "connection_lost", "label": "Connection lost", "severity": "crit",
             "blink": False, "detail": ""}
CONN_WARN = dict(CONN_CRIT, severity="warn")
STALE = {"state": "stale", "label": "Stale data", "severity": "warn", "blink": False,
         "detail": ""}
# A member whose own UPS entry has local resources (is_local: true): the
# plan says it "acts", but the group decides (F-184).
ROLE_MEMBER_ACTS = dict(ROLE_MEMBER, hasShutdownActions=True, shutsDownLocalHost=True)


def _banner_cases():
    near = [_trigger("criticalRuntime", "Critical runtime", "ok", eta=121,
                     cond="runtime < 5m 0s")]
    fired = [_trigger("lowBattery", "Low battery", "fired", eta=0,
                      cond="charge < 20%", text="12% now")]
    lab_ob = _row("Lab", "OB DISCHRG", role=ROLE_LOCAL, summary=OB, triggers=near,
                  tob_text="3m 0s")
    watched_ob = _row("APC", "OB DISCHRG", role=ROLE_MONITOR, summary=OB,
                      tob_text="3m 0s")
    stale = dict(_row("Lab", "OL", role=ROLE_LOCAL, summary=STALE),
                 freshness={"ageSeconds": 120, "stale": True, "lastPollAt": 1})
    member_trig = _row("UPS2", "OB LB", role=ROLE_MEMBER_ACTS, summary=TRIG,
                       triggers=fired, trigger_active=True)
    return {
        "connCrit": [_row("Lab", "OB", role=ROLE_LOCAL, summary=CONN_CRIT), watched_ob],
        "connWarn": [_row("Lab", "OL", role=ROLE_LOCAL, summary=CONN_WARN)],
        "connWarnVsWatched": [_row("Lab", "OL", role=ROLE_LOCAL, summary=CONN_WARN),
                              watched_ob],
        "memberConn": [_row("UPS2", "OB", role=ROLE_MEMBER_ACTS, summary=CONN_CRIT)],
        "staleAlone": [stale],
        "staleVsOb": [stale, lab_ob],
        "memberOb": [_row("UPS2", "OB", role=ROLE_MEMBER, summary=OB, triggers=near,
                          tob_text="1m 0s")],
        "memberLb": [_row("UPS2", "OB LB", role=ROLE_MEMBER, summary=LB, charge="9")],
        "memberActsLb": [_row("UPS2", "OB LB", role=ROLE_MEMBER_ACTS, summary=LB,
                              charge="9")],
        "memberActsTrig": [member_trig],
        "memberActsFsd": [_row("UPS2", "OB FSD", role=ROLE_MEMBER_ACTS, summary=FSD)],
        "quorum": [member_trig],
        "atRiskNoFailing": [],
    }


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_banner_branch_priorities(minimal_config):
    """Every bannerModel branch: exact text, severity and rank (F-184/F-186)."""
    body = "const cases = " + json.dumps(_banner_cases()) + ";\n" + """
        const quorum = {name: "rack-a", upsSources: ["UPS1", "UPS2"],
          outlook: {state: "quorum-lost", severity: "crit",
                    label: "Quorum lost → group shutdown runs",
                    action: "Shuts down 2 remote servers"}};
        const atRisk = {name: "rack-a", upsSources: ["UPS1", "UPS2"], failingMembers: [],
          outlook: {state: "at-risk", severity: "warn",
                    label: "1 more failure → group shutdown", action: ""}};
        const groups = {quorum: [quorum], atRiskNoFailing: [atRisk]};
        const out = {};
        for (const k of Object.keys(cases)) {
          const m = bannerModel(cases[k], groups[k] || []);
          out[k] = m && {prio: m.prio, severity: m.severity, text: m.text,
                         progress: m.progress, more: m.more};
        }
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    # Losing NUT on battery is the failsafe path: it outranks a watched outage.
    assert out["connCrit"] == {
        "prio": 75, "severity": "crit", "progress": False, "more": 1,
        "text": "Connection lost — Lab was on battery: failsafe shutdown applies"}
    # On mains it is a quiet amber note, below any real outage.
    assert out["connWarn"]["prio"] == 15 and out["connWarn"]["severity"] == "warn"
    assert out["connWarn"]["text"] == "Connection lost — Lab: no fresh readings"
    assert out["connWarnVsWatched"]["text"].startswith("On battery — APC (monitoring only)")
    # A member never claims the failsafe: the group decides.
    assert out["memberConn"]["text"] == "Connection lost — UPS2 was on battery"
    assert out["staleAlone"] == {"prio": 10, "severity": "warn", "progress": False,
                                 "more": 0,
                                 "text": "Stale data — Lab: last reading 2m ago"}
    assert out["staleVsOb"]["text"] == (
        "On battery — Lab for 3m 0s. Shutdown in ≈2m 1s (runtime < 5m 0s)")
    assert out["staleVsOb"]["prio"] == 50
    # A member on battery is not "monitoring only": it counts for its group.
    assert out["memberOb"]["prio"] == 35
    assert out["memberOb"]["text"] == (
        "On battery — UPS2 for 1m 0s. Counts as failed for its group in ≈2m 1s"
        " (runtime < 5m 0s)")
    assert out["memberLb"]["severity"] == "warn" and out["memberLb"]["prio"] == 70
    # F-184: local resources on the member's entry do not override the group.
    assert out["memberActsLb"]["severity"] == "warn"
    assert out["memberActsTrig"] == {
        "prio": 45, "severity": "warn", "progress": False, "more": 0,
        "text": "UPS2 is critical for redundancy group rack-a: low battery (12% now)"
                " — the group decides"}
    assert out["memberActsFsd"] == {
        "prio": 45, "severity": "warn", "progress": False, "more": 0,
        "text": "UPS2 signals a forced shutdown (FSD) — redundancy group rack-a decides"}
    # The group's verdict outranks its members' own alarms.
    assert out["quorum"]["prio"] == 90 and out["quorum"]["severity"] == "crit"
    assert out["quorum"]["text"] == (
        "Quorum lost — redundancy group rack-a: group shutdown runs"
        " (Shuts down 2 remote servers)")
    assert out["quorum"]["more"] == 1
    # "At risk" with every member healthy is a sizing fact, not an alarm.
    assert out["atRiskNoFailing"] is None


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_client_freshness_math(minimal_config):
    """dataAgeSeconds keeps counting while the daemon is silent; dataIsStale
    trusts only a polled stale flag and falls back to 30 s (F-186)."""
    body = """
        Date.now = () => 1e12;
        const out = {
          neverPolled: dataIsStale({freshness: {stale: true, lastPollAt: null,
                                                ageSeconds: 5, staleAfterSeconds: 30}}),
          polledStale: dataIsStale({freshness: {stale: true, lastPollAt: 7,
                                                ageSeconds: 5, staleAfterSeconds: 30}}),
          fallback30: dataIsStale({freshness: {ageSeconds: 60}}),
          fresh30: dataIsStale({freshness: {ageSeconds: 20}}),
          customWindow: dataIsStale({freshness: {ageSeconds: 60, staleAfterSeconds: 90}}),
          noData: [dataAgeSeconds({}), dataIsStale({})],
        };
        lastGoodFetchAt = 1e12 - 100000;   // the daemon went quiet 100 s ago
        out.silentAge = dataAgeSeconds({freshness: {ageSeconds: 5}});
        out.silentStale = dataIsStale({freshness: {ageSeconds: 5, staleAfterSeconds: 30}});
        lastGeneratedAt = 1000;
        out.legacyAge = dataAgeSeconds({lastUpdateTime: 940});
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["neverPolled"] is False
    assert out["polledStale"] is True
    assert out["fallback30"] is True and out["fresh30"] is False
    assert out["customWindow"] is False
    assert out["noData"] == [None, False]
    assert out["silentAge"] == 105
    assert out["silentStale"] is True
    assert out["legacyAge"] == 160


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_outlook_line_states(minimal_config):
    """outlookLine: fired leads, held wording, disabled skipped, monitor-only
    silent, member verb; heroOutlook severity follows the role (F-184/F-186)."""
    disabled = dict(_trigger("extendedTime", "Time on battery", "ok", eta=10,
                             cond="on battery > 5m 0s"), enabled=False)
    rows = {
        "fired": _row("Lab", "OB", role=ROLE_LOCAL, summary=TRIG, triggers=[
            _trigger("criticalRuntime", "Critical runtime", "ok", eta=10,
                     cond="runtime < 5m 0s"),
            _trigger("lowBattery", "Low battery", "fired", cond="charge < 20%")]),
        "held": _row("Lab", "OB", role=ROLE_LOCAL, summary=OB, triggers=[
            _trigger("lowBattery", "Low battery", "held", eta=30, cond="charge < 20%")]),
        "disabled": _row("Lab", "OB", role=ROLE_LOCAL, summary=OB, triggers=[
            disabled, _trigger("criticalRuntime", "Critical runtime", "ok", eta=100,
                               cond="runtime < 5m 0s")]),
        "noEta": _row("Lab", "OB", role=ROLE_LOCAL, summary=OB, triggers=[
            _trigger("depletionRate", "Fast battery drain", "ok",
                     cond="drain > 15%/min")]),
        "monitor": _row("APC", "OB", role=ROLE_MONITOR, summary=OB, triggers=[
            _trigger("criticalRuntime", "Critical runtime", "ok", eta=100,
                     cond="runtime < 5m 0s")]),
        "member": _row("UPS2", "OB", role=ROLE_MEMBER_ACTS, summary=TRIG, triggers=[
            _trigger("lowBattery", "Low battery", "fired", cond="charge < 20%")]),
    }
    body = "const rows = " + json.dumps(rows) + ";\n" + """
        document.createElement = (tag) => ({tag, className: "", textContent: "",
          children: [], setAttribute() {}, appendChild(c) { this.children.push(c); }});
        const out = {};
        for (const k of Object.keys(rows)) out[k] = outlookLine(rows[k]);
        out.heroLocal = heroOutlook(rows.fired).className;
        out.heroMember = heroOutlook(rows.member).className;
        out.heroMonitor = heroOutlook(rows.monitor).className;
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["fired"] == "Shutdown now: charge < 20%"
    assert out["held"] == "Shutdown after stabilization, in ≈30s (charge < 20%)"
    assert out["disabled"] == "Shutdown in ≈1m 40s (runtime < 5m 0s)"
    assert out["noEta"] == "Shutdown when drain > 15%/min"
    assert out["monitor"] is None
    assert out["member"] == "Counts as failed for its group now: charge < 20%"
    assert out["heroLocal"] == "hero-outlook s-crit"
    assert out["heroMember"] == "hero-outlook s-warn"
    assert out["heroMonitor"] == "hero-outlook s-muted"


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_failsafe_arming_renders_amber(minimal_config):
    """F-180: the FAILSAFE failed-poll countdown (``arming``) is a next-trigger
    candidate, reads its own text, renders amber and lifts the banner above a
    plain on-battery notice (but below low battery)."""
    arming = _trigger("failsafe", "Connection lost on battery", "arming", eta=10,
                      cond="connection to NUT lost while on battery",
                      text="1 of 3 NUT polls failed · fires at 3")
    later = _trigger("criticalRuntime", "Critical runtime", "ok", eta=600,
                     cond="runtime < 5m 0s")
    row = _row("Lab", "OB", role=ROLE_LOCAL, summary=OB, tob_text="1m 0s",
               triggers=[later, arming])
    body = "const row = " + json.dumps(row) + ";\n" + """
        document.createElement = (tag) => ({tag, className: "", textContent: "",
          children: [], setAttribute() {}, appendChild(c) { this.children.push(c); }});
        const out = {};
        out.line = outlookLine(row);
        out.first = upcomingTriggers(row)[0].state;
        const m = bannerModel([row], []);
        out.banner = {prio: m.prio, severity: m.severity, text: m.text};
        out.hero = heroOutlook(row).className;
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["first"] == "arming"
    assert out["line"] == (
        "Shutdown in ≈10s unless NUT answers (1 of 3 NUT polls failed · fires at 3)"
        " · or runtime < 5m 0s in ≈10m 0s")
    assert out["banner"]["prio"] == 60 and out["banner"]["severity"] == "warn"
    assert out["banner"]["text"].startswith("On battery — Lab for 1m 0s. Shutdown in ≈10s")
    assert out["hero"] == "hero-outlook s-warn"
    js = _asset(minimal_config, "/app.js")
    assert '(t.state === "held" || t.state === "arming") ? "warn"' in js
    css = _asset(minimal_config, "/style.css")
    assert "li.ho-arming .ho-eta { color: var(--warn); }" in css


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_old_daemon_role_fallback(minimal_config):
    """A pre-6.2 row (no role, no isLocal) powers this host, like the loader's
    default; an explicit isLocal:false without remotes only watches."""
    body = """
        const out = {
          unset: upsRole({name: "x"}).kind,
          explicit: upsRole({name: "x", isLocal: true}).kind,
          off: upsRole({name: "x", isLocal: false}).kind,
          action: actionLabel({name: "x"}),
        };
        lastGroups = [{name: "rack-a", upsSources: ["m"]}];
        out.member = upsRole({name: "m"});
        process.stdout.write(JSON.stringify(out));
    """
    out = _run(minimal_config, body)
    assert out["unset"] == "local" and out["explicit"] == "local"
    assert out["off"] == "monitor-only"
    assert out["action"] == "Shuts down this host"
    assert out["member"]["kind"] == "redundancy-member"
    assert out["member"]["label"] == "Redundancy member (rack-a)"
    assert out["member"]["hasShutdownActions"] is False


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_fleet_rows_keep_redundancy_group_names(minimal_config, tmp_path):
    """A08: /api/v1/ups rows built by collect_status name their group, and the
    dashboard shows that name instead of "its redundancy group"."""
    from unittest.mock import MagicMock

    from eneru import MonitorState, UPSGroupMonitor
    from eneru.config import RedundancyGroupConfig
    from eneru.status import collect_status

    minimal_config.logging.battery_history_file = str(tmp_path / "bh")
    minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
    minimal_config.logging.state_file = str(tmp_path / "state")
    minimal_config.redundancy_groups = [RedundancyGroupConfig(
        name="rack-a", ups_sources=["TestUPS@localhost", "Other@h"])]
    monitor = UPSGroupMonitor(minimal_config)
    monitor.state = MonitorState()
    monitor.logger = MagicMock()
    monitor._in_redundancy_group = True
    source = MagicMock()
    source.config = minimal_config
    source._monitors = [monitor]
    source._redundancy_remote_health_managers = []
    row = collect_status(source)["ups"][0]
    assert row["role"]["kind"] == "redundancy-member"
    assert row["role"]["redundancyGroups"] == ["rack-a"]
    body = "const row = " + json.dumps(row) + ";\n" + """
        process.stdout.write(JSON.stringify({role: upsRole(row).label,
                                             action: actionLabel(row)}));
    """
    out = _run(minimal_config, body)
    assert out["role"] == "Redundancy member (rack-a)"
    assert out["action"] == (
        "Marks this UPS critical for redundancy group rack-a (group decides) (dry-run)")
