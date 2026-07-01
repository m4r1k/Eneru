#!/usr/bin/env bash
#
# E2E group: single-ups-auth
#
# Auto-extracted from .github/workflows/e2e.yml. Tests in this
# group run sequentially; each test body is wrapped in a subshell
# so cd / env changes do NOT leak between tests (the original
# workflow had per-step shell isolation -- we preserve it here).
# Each group runs as a separate parallel matrix job (see
# .github/workflows/e2e.yml).

set -euo pipefail

: "${E2E_DIR:=tests/e2e}"
# Always work with an absolute path so a test that `cd`s elsewhere
# and then references $E2E_DIR/... still resolves correctly. Without
# this, `tests/e2e` would be re-resolved relative to the new cwd.
E2E_DIR="$(cd "$E2E_DIR" && pwd)"
export E2E_DIR

# Shared E2E helpers (apply_scenario: poll-until-applied scenario swaps).
. "$E2E_DIR/groups/lib.sh"

# ======================================================================
# Test 52: API authentication — login, tiered config, write gating (v6.0)
# ======================================================================
# Brings up the daemon with api.auth enabled and a real bcrypt-backed user,
# then proves: login issues a bearer token; /api/v1/config is sanitized for
# anonymous callers and extended for authenticated ones; and an anonymous
# write (logout) is rejected 401. Reads stay open (require_for_reads=false).
(
echo ""
echo ">>> Running: Test 52: API authentication login + tiered config + write gating"

AUTH_DB="$(mktemp -d)/auth.db"
printf 's3cret-pw' | eneru user create operator --password-stdin --auth-db "$AUTH_DB" \
  || { echo "FAIL: could not create auth user"; exit 1; }

cat > /tmp/config-e2e-auth.yaml <<YAML
ups:
  name: "TestUPS@localhost:3493"
behavior:
  dry_run: true
statistics:
  enabled: true
  db_directory: "$(mktemp -d)"
api:
  enabled: true
  bind: "127.0.0.1"
  port: 9100
  auth:
    enabled: true
    require_for_reads: false
    db_path: "$AUTH_DB"
prometheus:
  enabled: true
notifications:
  enabled: false
local_shutdown:
  enabled: false
YAML

apply_scenario online-charging
timeout 15s eneru run --config /tmp/config-e2e-auth.yaml > /tmp/test52-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

poll_endpoint() {
  local url="$1" out="$2" tries="${3:-20}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$url" >"$out" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  return 1
}

# /health is always open
if ! poll_endpoint http://127.0.0.1:9100/health /tmp/test52-health.json; then
  echo "FAIL: /health never responded"; cat /tmp/test52-daemon.log; exit 1
fi

# anonymous /config -> sanitized
curl -fsS http://127.0.0.1:9100/api/v1/config > /tmp/test52-config-anon.json
if ! grep -q '"detail": "sanitized"' /tmp/test52-config-anon.json; then
  echo "FAIL: anonymous /config not sanitized"; cat /tmp/test52-config-anon.json; exit 1
fi

# login -> bearer token
curl -fsS -X POST -H 'Content-Type: application/json' \
  -d '{"username":"operator","password":"s3cret-pw"}' \
  http://127.0.0.1:9100/api/v1/auth/login > /tmp/test52-login.json \
  || { echo "FAIL: login request failed"; cat /tmp/test52-daemon.log; exit 1; }
TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/test52-login.json'))['token'])")
if [ -z "$TOKEN" ]; then echo "FAIL: no token in login response"; cat /tmp/test52-login.json; exit 1; fi
echo "PASS: login issued a bearer token"

# bad credentials -> 401
BAD=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{"username":"operator","password":"wrong"}' \
  http://127.0.0.1:9100/api/v1/auth/login)
if [ "$BAD" != "401" ]; then echo "FAIL: bad creds returned $BAD, expected 401"; exit 1; fi

# authenticated /config -> extended
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9100/api/v1/config \
  > /tmp/test52-config-auth.json
if ! grep -q '"detail": "extended"' /tmp/test52-config-auth.json; then
  echo "FAIL: authenticated /config not extended"; cat /tmp/test52-config-auth.json; exit 1
fi

# anonymous write (logout without token) -> 401
ANON_WRITE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:9100/api/v1/auth/logout)
if [ "$ANON_WRITE" != "401" ]; then echo "FAIL: anonymous logout returned $ANON_WRITE, expected 401"; exit 1; fi

# anonymous read still works (require_for_reads=false)
curl -fsS http://127.0.0.1:9100/api/v1/ups > /dev/null \
  || { echo "FAIL: anonymous read blocked unexpectedly"; exit 1; }

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "PASS: API auth login, tiered config, and write gating verified"
)

# ======================================================================
# Test 53: UPS control — fail-closed validation + allowlist enforcement (v6.0)
# ======================================================================
# Proves the fail-closed invariant (nut_control without auth refuses to start)
# and that the control endpoints enforce the command allowlist server-side.
# NUT's dummy-ups driver in dummy mode does not execute instant commands, so
# this test treats the driver's own CMD-NOT-SUPPORTED reply as the proof that
# Eneru passed the allowlisted request through to NUT instead of blocking it.
(
echo ""
echo ">>> Running: Test 53: UPS control fail-closed + allowlist enforcement"

# Fail-closed: nut_control.enabled with auth OFF must fail validation.
cat > /tmp/config-e2e-control-noauth.yaml <<'YAML'
ups:
  name: "TestUPS@localhost:3493"
behavior:
  dry_run: true
nut_control:
  enabled: true
  allowed_commands: ["beeper.toggle"]
YAML
if eneru validate --config /tmp/config-e2e-control-noauth.yaml >/tmp/test53-val.log 2>&1; then
  echo "FAIL: nut_control without auth was accepted"; cat /tmp/test53-val.log; exit 1
fi
grep -q "nut_control.enabled requires API authentication" /tmp/test53-val.log \
  || { echo "FAIL: missing fail-closed message"; cat /tmp/test53-val.log; exit 1; }
echo "PASS: nut_control without auth is rejected at startup"

# With auth + nut_control enabled, the allowlist is enforced server-side.
AUTH_DB="$(mktemp -d)/auth.db"
RUNTIME_DIR="$(mktemp -d)"
printf 's3cret-pw' | eneru user create operator --password-stdin --auth-db "$AUTH_DB"

cat > /tmp/config-e2e-control.yaml <<YAML
ups:
  name: "TestUPS@localhost:3493"
behavior:
  dry_run: true
statistics:
  enabled: true
  db_directory: "$(mktemp -d)"
logging:
  file: "$RUNTIME_DIR/eneru.log"
  state_file: "$RUNTIME_DIR/state.json"
  battery_history_file: "$RUNTIME_DIR/battery-history"
  shutdown_flag_file: "$RUNTIME_DIR/shutdown-flag"
api:
  enabled: true
  bind: "127.0.0.1"
  port: 9100
  auth:
    enabled: true
    db_path: "$AUTH_DB"
nut_control:
  enabled: true
  username: "admin"
  password: "testpass"
  allowed_commands: ["beeper.toggle"]
notifications:
  enabled: false
local_shutdown:
  enabled: false
YAML

apply_scenario online-charging
timeout 15s eneru run --config /tmp/config-e2e-control.yaml > /tmp/test53-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:9100/health >/dev/null 2>&1 && break
  sleep 0.5
done

TOKEN=$(curl -fsS -X POST -H 'Content-Type: application/json' \
  -d '{"username":"operator","password":"s3cret-pw"}' \
  http://127.0.0.1:9100/api/v1/auth/login \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
[ -n "$TOKEN" ] || { echo "FAIL: no token"; cat /tmp/test53-daemon.log; exit 1; }

# A disallowed command is rejected 403 BEFORE any NUT call.
DENIED=$(curl -sS -o /tmp/test53-denied.json -w '%{http_code}' \
  -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"command":"load.off"}' \
  http://127.0.0.1:9100/api/v1/ups/TestUPS@localhost:3493/command)
if [ "$DENIED" != "403" ]; then
  echo "FAIL: disallowed command returned $DENIED, expected 403"; cat /tmp/test53-denied.json; exit 1
fi

# Unauthenticated control is rejected 401.
ANON=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' -d '{"command":"beeper.toggle"}' \
  http://127.0.0.1:9100/api/v1/ups/TestUPS@localhost:3493/command)
if [ "$ANON" != "401" ]; then echo "FAIL: anonymous control returned $ANON, expected 401"; exit 1; fi

# An allowlisted command reaches NUT. Most dummy-ups versions do not support
# instant commands, so the normal response is NUT's own CMD-NOT-SUPPORTED
# error mapped through Eneru as 502. If a future dummy driver accepts the
# command, a 200 is also proof that the request crossed the API -> upscmd ->
# upsd boundary. A 401/403 here would mean Eneru blocked the request first.
ALLOWED=$(curl -sS -o /tmp/test53-allowed.json -w '%{http_code}' \
  -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"command":"beeper.toggle"}' \
  http://127.0.0.1:9100/api/v1/ups/TestUPS@localhost:3493/command)
if [ "$ALLOWED" = "200" ]; then
  grep -q '"status": "ok"' /tmp/test53-allowed.json \
    || { echo "FAIL: allowed command returned 200 without ok status"; cat /tmp/test53-allowed.json; exit 1; }
elif [ "$ALLOWED" = "502" ]; then
  grep -q '"code": "NUT_ERROR"' /tmp/test53-allowed.json \
    || { echo "FAIL: allowed command did not return a NUT_ERROR"; cat /tmp/test53-allowed.json; exit 1; }
  grep -q 'CMD-NOT-SUPPORTED' /tmp/test53-allowed.json \
    || { echo "FAIL: allowed command did not expose the expected dummy-ups unsupported-command response"; cat /tmp/test53-allowed.json; exit 1; }
  if grep -q 'ACCESS-DENIED' /tmp/test53-allowed.json; then
    echo "FAIL: allowed command used invalid NUT credentials"
    cat /tmp/test53-allowed.json
    exit 1
  fi
else
  echo "FAIL: allowed command returned $ALLOWED, expected 200 or dummy-ups NUT_ERROR 502"
  cat /tmp/test53-allowed.json
  cat /tmp/test53-daemon.log
  exit 1
fi

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "PASS: UPS control fail-closed, allowlist enforcement, and NUT reachability verified"
)

# ======================================================================
# Test 54: Config hot-reload — SIGHUP + API endpoint (v6.0)
# ======================================================================
# Edits a threshold in the live config, then reloads via SIGHUP and via the
# authenticated API endpoint, asserting the daemon stays up and reports the
# applied change. Also proves a bad config is rejected without dropping the
# daemon.
(
echo ""
echo ">>> Running: Test 54: Config hot-reload via SIGHUP and API"

AUTH_DB="$(mktemp -d)/auth.db"
printf 's3cret-pw' | eneru user create operator --password-stdin --auth-db "$AUTH_DB"
CFG=/tmp/config-e2e-reload.yaml

write_cfg() {  # $1 = low_battery_threshold
cat > "$CFG" <<YAML
ups:
  name: "TestUPS@localhost:3493"
triggers:
  low_battery_threshold: $1
behavior:
  dry_run: true
statistics:
  enabled: true
  db_directory: "$(mktemp -d)"
api:
  enabled: true
  bind: "127.0.0.1"
  port: 9100
  auth:
    enabled: true
    db_path: "$AUTH_DB"
notifications:
  enabled: false
local_shutdown:
  enabled: false
YAML
}

write_cfg 20
apply_scenario online-charging
# Generous timeout: this test does login + two reloads + a bad reload, each with
# its own poll budget. Too short a timeout kills the daemon mid-test.
PYTHONUNBUFFERED=1 timeout 60s eneru run --config "$CFG" > /tmp/test54-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:9100/health >/dev/null 2>&1 && break
  sleep 0.5
done

# Edit the threshold, then SIGHUP -> the daemon applies it live and logs it.
write_cfg 55
kill -HUP "$DAEMON_PID"
RELOADED=""
for _ in $(seq 1 20); do
  if grep -q "Config reloaded" /tmp/test54-daemon.log; then RELOADED=1; break; fi
  sleep 0.5
done
[ -n "$RELOADED" ] || { echo "FAIL: SIGHUP reload not logged"; cat /tmp/test54-daemon.log; exit 1; }
grep -q "triggers" /tmp/test54-daemon.log || { echo "FAIL: triggers not applied"; cat /tmp/test54-daemon.log; exit 1; }
echo "PASS: SIGHUP applied the threshold change live"

# API reload endpoint (authenticated) returns a report.
TOKEN=$(curl -fsS -X POST -H 'Content-Type: application/json' \
  -d '{"username":"operator","password":"s3cret-pw"}' \
  http://127.0.0.1:9100/api/v1/auth/login \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
write_cfg 60
RELOAD_HTTP=$(curl -sS -o /tmp/test54-reload.json -w '%{http_code}' \
  -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:9100/api/v1/config/reload)
[ "$RELOAD_HTTP" = "200" ] || { echo "FAIL: API reload returned $RELOAD_HTTP"; cat /tmp/test54-reload.json; exit 1; }
# Assert the threshold change (55 -> 60) was actually APPLIED live, not just that
# the reload path ran: a no-op reload would leave `applied` empty and fail here.
python3 -c "import json;r=json.load(open('/tmp/test54-reload.json'));assert r['reloaded'] is True and any(a.startswith('triggers') for a in r['applied']), r" \
  || { echo "FAIL: API reload did not apply the triggers change"; cat /tmp/test54-reload.json; exit 1; }

# Anonymous reload is rejected.
ANON=$(curl -sS -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:9100/api/v1/config/reload)
[ "$ANON" = "401" ] || { echo "FAIL: anonymous reload returned $ANON, expected 401"; exit 1; }

# A broken config is rejected and the daemon stays up. Use the synchronous API
# endpoint (deterministic) rather than SIGHUP + log-polling (racy in CI).
echo "ups: [broken" > "$CFG"
BAD_HTTP=$(curl -sS -o /tmp/test54-bad.json -w '%{http_code}' \
  -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9100/api/v1/config/reload)
[ "$BAD_HTTP" = "400" ] || { echo "FAIL: bad reload HTTP $BAD_HTTP, expected 400"; cat /tmp/test54-bad.json; exit 1; }
python3 -c "import json;r=json.load(open('/tmp/test54-bad.json'));assert r['reloaded'] is False and r['errors']" \
  || { echo "FAIL: bad reload report not rejected"; cat /tmp/test54-bad.json; exit 1; }
# Daemon stays up on a bad reload.
curl -fsS http://127.0.0.1:9100/health >/dev/null 2>&1 \
  || { echo "FAIL: daemon died on bad reload"; cat /tmp/test54-daemon.log; exit 1; }

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "PASS: config hot-reload via SIGHUP and API verified"
)

# ======================================================================
# Test 55: Browser dashboard is served by the embedded API (v6.0)
# ======================================================================
# The dashboard ships with the package and is served whenever the API is on.
# Verifies the SPA shell + assets load and that path traversal is rejected.
(
echo ""
echo ">>> Running: Test 55: Browser dashboard served by embedded API"

apply_scenario online-charging
timeout 12s eneru run --config "$E2E_DIR/config-e2e-dry-run.yaml" \
  > /tmp/test55-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:9100/health >/dev/null 2>&1 && break
  sleep 0.5
done

curl -fsS http://127.0.0.1:9100/ > /tmp/test55-index.html \
  || { echo "FAIL: dashboard index not served"; cat /tmp/test55-daemon.log; exit 1; }
grep -q "<title>Eneru</title>" /tmp/test55-index.html \
  || { echo "FAIL: dashboard index missing title"; cat /tmp/test55-index.html; exit 1; }
# v6.1: the dashboard is a tabbed SPA — the tab nav must be served.
grep -q 'role="tablist"' /tmp/test55-index.html \
  || { echo "FAIL: dashboard tab nav not served"; cat /tmp/test55-index.html; exit 1; }
curl -fsS http://127.0.0.1:9100/app.js   >/dev/null || { echo "FAIL: app.js not served"; exit 1; }
curl -fsS http://127.0.0.1:9100/style.css >/dev/null || { echo "FAIL: style.css not served"; exit 1; }

# Content-Type + CSP on the HTML response.
HDRS=$(curl -fsS -D - -o /dev/null http://127.0.0.1:9100/)
echo "$HDRS" | grep -qi "Content-Type: text/html" || { echo "FAIL: index not text/html"; echo "$HDRS"; exit 1; }
echo "$HDRS" | grep -qi "Content-Security-Policy" || { echo "FAIL: missing CSP header"; echo "$HDRS"; exit 1; }

# Unknown asset -> 404.
TRAV=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:9100/nope.js")
[ "$TRAV" = "404" ] || { echo "FAIL: unknown asset returned $TRAV, expected 404"; exit 1; }

# Actual path-traversal payloads (raw and percent-encoded) must be rejected,
# never serve a file outside the web root. --path-as-is keeps curl from
# normalizing the ../ away client-side so the server's guard is what's tested.
for payload in "/../config.py" "/..%2f..%2fconfig.py" "/%2e%2e/%2e%2e/etc/passwd"; do
  CODE=$(curl -sS --path-as-is -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:9100${payload}")
  case "$CODE" in
    200) echo "FAIL: traversal '${payload}' served content (HTTP 200)"; exit 1 ;;
    *) : ;;  # 400/403/404 are all acceptable rejections
  esac
done

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "PASS: browser dashboard served with CSP and traversal protection"
)

# ======================================================================
# Test 56: Event management — wide-range query + auth-gated delete (v6.0)
# ======================================================================
# Proves the wide-history events query works, that an authenticated client can
# delete a real event by its (id, ts, eventType), that an anonymous delete is
# rejected 401, and that a history from>to is a 400.
(
echo ""
echo ">>> Running: Test 56: event management — wide-range query + auth-gated delete"

AUTH_DB="$(mktemp -d)/auth.db"
printf 's3cret-pw' | eneru user create operator --password-stdin --auth-db "$AUTH_DB" \
  || { echo "FAIL: could not create auth user"; exit 1; }

cat > /tmp/config-e2e-events.yaml <<YAML
ups:
  name: "TestUPS@localhost:3493"
behavior:
  dry_run: true
statistics:
  enabled: true
  db_directory: "$(mktemp -d)"
api:
  enabled: true
  bind: "127.0.0.1"
  port: 9100
  auth:
    enabled: true
    require_for_reads: false
    db_path: "$AUTH_DB"
notifications:
  enabled: false
local_shutdown:
  enabled: false
YAML

apply_scenario online-charging
timeout 20s eneru run --config /tmp/config-e2e-events.yaml > /tmp/test56-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:9100/health >/dev/null 2>&1 && break
  sleep 0.5
done

# Wide-range query returns events (the daemon records at least a lifecycle row).
EVENTS=""
for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:9100/api/v1/events?from=1&to=9999999999&limit=10&verbosity=2" \
    > /tmp/test56-events.json 2>/dev/null || true
  if python3 -c "import json,sys;sys.exit(0 if json.load(open('/tmp/test56-events.json'))['events'] else 1)" 2>/dev/null; then
    EVENTS="ok"; break
  fi
  sleep 0.5
done
[ -n "$EVENTS" ] || { echo "FAIL: no events from wide-range query"; cat /tmp/test56-daemon.log; exit 1; }

# Each event carries a source-qualified identity (id + source).
python3 -c "import json,sys;e=json.load(open('/tmp/test56-events.json'))['events'][0];sys.exit(0 if ('id' in e and 'source' in e) else 1)" \
  || { echo "FAIL: event row missing id/source"; cat /tmp/test56-events.json; exit 1; }

UPS_ENC=$(python3 -c "import json,urllib.parse;e=json.load(open('/tmp/test56-events.json'))['events'][0];print(urllib.parse.quote(e['ups'],safe=''))")
BODY=$(python3 -c "import json;e=json.load(open('/tmp/test56-events.json'))['events'][0];print(json.dumps({'items':[{'id':e['id'],'ts':e['ts'],'eventType':e['eventType']}]}))")

# Anonymous delete -> 401.
ANON=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H 'Content-Type: application/json' \
  -d "$BODY" "http://127.0.0.1:9100/api/v1/ups/$UPS_ENC/events")
[ "$ANON" = "401" ] || { echo "FAIL: anonymous delete returned $ANON, expected 401"; exit 1; }

# Login + authenticated delete -> 200 with deleted >= 1.
TOKEN=$(curl -fsS -X POST -H 'Content-Type: application/json' \
  -d '{"username":"operator","password":"s3cret-pw"}' \
  http://127.0.0.1:9100/api/v1/auth/login | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
[ -n "$TOKEN" ] || { echo "FAIL: no token"; cat /tmp/test56-daemon.log; exit 1; }

curl -fsS -X DELETE -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$BODY" "http://127.0.0.1:9100/api/v1/ups/$UPS_ENC/events" > /tmp/test56-del.json \
  || { echo "FAIL: authed delete request failed"; cat /tmp/test56-daemon.log; exit 1; }
python3 -c "import json,sys;d=json.load(open('/tmp/test56-del.json'));sys.exit(0 if d.get('deleted',0)>=1 else 1)" \
  || { echo "FAIL: authed delete did not remove a row"; cat /tmp/test56-del.json; exit 1; }

# History with from > to -> 400.
HIST=$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:9100/api/v1/ups/$UPS_ENC/history?metric=charge&from=200&to=100")
[ "$HIST" = "400" ] || { echo "FAIL: history from>to returned $HIST, expected 400"; exit 1; }

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "PASS: event wide-range query, auth-gated delete, and history validation verified"
)

# ======================================================================
# Test 57: self-test — passive observation + softened permission (v6.1.2)
# ======================================================================
# Two v6.1.2 behaviours the pre-6.1.2 suite never exercised:
#   (A) enabling self_test no longer requires nut_control.enabled, and
#       effective auth may come from a USER IN THE AUTH DB (not only the
#       api.auth.enabled flag) -- so `eneru validate` accepts that shape;
#   (B) Eneru passively records the UPS's OWN self-test result
#       (ups.test.result / ups.test.date) as a source=device row, surfaced
#       in the /api/v1/ups selfTest block, regardless of self_test being on.
# The dummy-ups driver has no INSTCMD, so ISSUING a test cannot be E2E'd;
# (A)+(B) are the reachable — and previously-missing — self-test coverage.
echo ">>> Running: Test 57: self-test passive observation + softened permission"
(
set -euo pipefail

ST_AUTH_DB="$(mktemp -d)/auth.db"
printf 's3cret-pw' | eneru user create operator --password-stdin --auth-db "$ST_AUTH_DB" \
  || { echo "FAIL: could not create auth user"; exit 1; }

# --- (A) validate: self_test on + nut_control OFF + auth via DB user -> OK ---
cat > /tmp/config-e2e-selftest-soft.yaml <<YAML
ups:
  name: "TestUPS@localhost:3493"
behavior:
  dry_run: true
statistics:
  enabled: true
  db_directory: "$(mktemp -d)"
api:
  enabled: true
  bind: "127.0.0.1"
  port: 9100
  auth:
    db_path: "$ST_AUTH_DB"
    require_for_reads: false
self_test:
  enabled: true
  command: test.battery.start
notifications:
  enabled: false
local_shutdown:
  enabled: false
YAML

if ! eneru validate --config /tmp/config-e2e-selftest-soft.yaml >/tmp/test57-val.log 2>&1; then
  echo "FAIL: self_test + auth-via-DB-user + nut_control off should validate"
  cat /tmp/test57-val.log; exit 1
fi
echo "PASS: self_test validates without nut_control.enabled (auth via DB user)"

# --- (A negative) self_test on but NO auth at all -> validation error ---
cat > /tmp/config-e2e-selftest-noauth.yaml <<YAML
ups:
  name: "TestUPS@localhost:3493"
api:
  auth:
    db_path: "$(mktemp -d)/empty-auth.db"
self_test:
  enabled: true
  command: test.battery.start
YAML
if eneru validate --config /tmp/config-e2e-selftest-noauth.yaml >/tmp/test57-noauth.log 2>&1; then
  echo "FAIL: self_test without any auth must be rejected at validation"
  cat /tmp/test57-noauth.log; exit 1
fi
grep -q "requires API authentication" /tmp/test57-noauth.log \
  || { echo "FAIL: expected 'requires API authentication' error"; cat /tmp/test57-noauth.log; exit 1; }
echo "PASS: self_test without auth is rejected at validation"

# --- (B) passive observation: the UPS reports its own last self-test ---
apply_scenario self-test-passed
timeout 30s eneru run --config /tmp/config-e2e-selftest-soft.yaml > /tmp/test57-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:9100/health >/dev/null 2>&1 && break
  sleep 0.5
done

# Poll the anonymous /api/v1/ups read until the observer has recorded the
# device result into the selfTest block (record commits immediately).
RESULT=""; SOURCE=""; DATE=""
for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:9100/api/v1/ups > /tmp/test57-ups.json 2>/dev/null; then
    RESULT=$(python3 -c "import json;d=json.load(open('/tmp/test57-ups.json'));st=(d.get('ups') or [{}])[0].get('selfTest') or {};print(st.get('result') or '-')")
    if [ "$RESULT" = "passed" ]; then
      SOURCE=$(python3 -c "import json;d=json.load(open('/tmp/test57-ups.json'));st=(d.get('ups') or [{}])[0].get('selfTest') or {};print(st.get('source') or '-')")
      DATE=$(python3 -c "import json;d=json.load(open('/tmp/test57-ups.json'));st=(d.get('ups') or [{}])[0].get('selfTest') or {};print(st.get('date') or '-')")
      break
    fi
  fi
  sleep 0.5
done

[ "$RESULT" = "passed" ]     || { echo "FAIL: selfTest.result was '$RESULT', expected passed"; cat /tmp/test57-daemon.log; exit 1; }
[ "$SOURCE" = "device" ]     || { echo "FAIL: selfTest.source was '$SOURCE', expected device"; cat /tmp/test57-daemon.log; exit 1; }
[ "$DATE" = "2026-06-02" ]   || { echo "FAIL: selfTest.date was '$DATE', expected 2026-06-02"; cat /tmp/test57-daemon.log; exit 1; }
grep -q "Observed UPS self-test: passed" /tmp/test57-daemon.log \
  || { echo "FAIL: daemon did not log the passive observation"; cat /tmp/test57-daemon.log; exit 1; }

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "PASS: passive self-test observation surfaced via API (source=device)"
)
echo ""
echo "=== Group 'single-ups-auth' completed successfully ==="
