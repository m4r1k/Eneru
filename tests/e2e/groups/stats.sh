#!/usr/bin/env bash
#
# E2E group: stats
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

# ======================================================================
# Test 28: SQLite stats persistence
# ======================================================================
(
echo ""
echo ">>> Running: Test 28: SQLite stats persistence"

echo "=== Test 28: SQLite stats persistence ==="
rm -rf /tmp/eneru-e2e-stats /tmp/eneru-e2e-stats-*
mkdir -p /tmp/eneru-e2e-stats

cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Pre-check: NUT must answer for TestUPS@localhost:3493 before we
# start the daemon, otherwise its `_wait_for_initial_connection`
# eats up to 30 seconds of the test timeout.
for i in {1..15}; do
  if upsc TestUPS@localhost:3493 ups.status 2>/dev/null | grep -q .; then
    echo "  NUT responding (attempt $i)"
    break
  fi
  sleep 1
done

# Run for ~50s -- worst case 30s for the connection wait + 10s
# for the writer to flush + headroom. PYTHONUNBUFFERED keeps
# stdout flushing under tee.
PYTHONUNBUFFERED=1 timeout 50s eneru run --config $E2E_DIR/config-e2e-stats.yaml --exit-after-shutdown 2>&1 | tee /tmp/test28.log || true

# 1. DB file must exist (single-UPS uses the "default.db" filename)
DB="/tmp/eneru-e2e-stats/default.db"
if [ ! -f "$DB" ]; then
  echo "FAIL: stats DB not created at $DB"
  ls -la /tmp/eneru-e2e-stats/
  tail -30 /tmp/test28.log
  exit 1
fi

# 2. samples table must have rows
SAMPLES=$(sqlite3 "$DB" "SELECT COUNT(*) FROM samples")
echo "Samples in DB: $SAMPLES"
if [ "$SAMPLES" -lt 1 ]; then
  echo "FAIL: no samples persisted (expected at least 1)"
  sqlite3 "$DB" ".schema"
  tail -30 /tmp/test28.log
  exit 1
fi

# 3. events table must contain the DAEMON_START event
EVENTS=$(sqlite3 "$DB" "SELECT event_type FROM events WHERE event_type='DAEMON_START'")
if [ -z "$EVENTS" ]; then
  echo "FAIL: DAEMON_START event not recorded"
  sqlite3 "$DB" "SELECT * FROM events"
  tail -30 /tmp/test28.log
  exit 1
fi
echo "PASS: stats DB has $SAMPLES sample(s) + DAEMON_START event"
)

# ======================================================================
# Test 29: Stats writer failure is non-fatal
# ======================================================================
(
echo ""
echo ">>> Running: Test 29: Stats writer failure is non-fatal"

echo "=== Test 29: Stats writer failure isolation ==="
rm -rf /tmp/eneru-e2e-stats-broken
# Create a *file* where the daemon expects a *directory*. open()
# will hit OSError; the daemon must keep monitoring without
# crashing.
touch /tmp/eneru-e2e-stats-broken

cat > /tmp/config-e2e-stats-broken.yaml <<'EOF'
ups:
  name: "TestUPS@localhost:3493"
  check_interval: 1
triggers:
  low_battery_threshold: 20
statistics:
  db_directory: "/tmp/eneru-e2e-stats-broken"
behavior:
  dry_run: true
logging:
  file: null
  state_file: "/tmp/eneru-e2e-stats-broken-state"
  battery_history_file: "/tmp/eneru-e2e-stats-broken-history"
  shutdown_flag_file: "/tmp/eneru-e2e-stats-broken-flag"
notifications:
  enabled: false
local_shutdown:
  enabled: false
EOF

cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 2

timeout 10s eneru run --config /tmp/config-e2e-stats-broken.yaml --exit-after-shutdown 2>&1 | tee /tmp/test29.log || true

# The daemon must have logged the warning AND kept polling.
if ! grep -q "stats store open failed" /tmp/test29.log; then
  echo "FAIL: expected 'stats store open failed' warning"
  tail -30 /tmp/test29.log
  exit 1
fi
# And it must NOT have logged a Python traceback / FATAL ERROR.
if grep -q "Traceback\|FATAL ERROR" /tmp/test29.log; then
  echo "FAIL: daemon crashed on stats failure"
  tail -30 /tmp/test29.log
  exit 1
fi
# Cleanup
rm -f /tmp/eneru-e2e-stats-broken /tmp/config-e2e-stats-broken.yaml
echo "PASS: stats failure isolated; daemon kept running"
)

# ======================================================================
# Test 30: monitor --once --graph renders ASCII graph
# ======================================================================
(
echo ""
echo ">>> Running: Test 30: monitor --once --graph renders ASCII graph"

echo "=== Test 30: monitor --once --graph ==="
# Reuse the DB seeded by Test 28 if it's still on disk; otherwise
# spin a fresh daemon to populate it.
DB="/tmp/eneru-e2e-stats/default.db"
if [ ! -f "$DB" ] || [ "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM samples')" -lt 1 ]; then
  rm -rf /tmp/eneru-e2e-stats /tmp/eneru-e2e-stats-*
  mkdir -p /tmp/eneru-e2e-stats

  cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
  sleep 3

  for i in {1..15}; do
    if upsc TestUPS@localhost:3493 ups.status 2>/dev/null | grep -q .; then
      break
    fi
    sleep 1
  done

  PYTHONUNBUFFERED=1 timeout 50s eneru run --config $E2E_DIR/config-e2e-stats.yaml --exit-after-shutdown > /tmp/test30-daemon.log 2>&1 || true
fi

if [ ! -f "$DB" ]; then
  echo "FAIL: stats DB missing at $DB"
  ls -la /tmp/eneru-e2e-stats/
  exit 1
fi

# Run the TUI in --once mode with the new --graph flag
eneru monitor --once --graph charge --time 1h --config $E2E_DIR/config-e2e-stats.yaml 2>&1 | tee /tmp/test30.log

if ! grep -q "charge -- last 1h" /tmp/test30.log; then
  echo "FAIL: graph header not present"
  tail -20 /tmp/test30.log
  exit 1
fi
if ! grep -q "y-axis: 0-100%" /tmp/test30.log; then
  echo "FAIL: y-axis label not present"
  tail -20 /tmp/test30.log
  exit 1
fi
echo "PASS: monitor --once --graph renders correctly"
)

# ======================================================================
# Test 31: monitor --once --events-only reads from SQLite
# ======================================================================
(
echo ""
echo ">>> Running: Test 31: monitor --once --events-only reads from SQLite"

echo "=== Test 31: events from SQLite ==="
DB="/tmp/eneru-e2e-stats/default.db"

# Reuse the seeded DB from Test 28; if absent, seed manually
# (Test 31 may be the first that needs the DB after a prior
# cleanup step in the same run).
if [ ! -f "$DB" ]; then
  echo "  no DB from earlier tests; seeding fresh..."
  mkdir -p /tmp/eneru-e2e-stats
  cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
  sleep 3
  for i in {1..15}; do
    if upsc TestUPS@localhost:3493 ups.status 2>/dev/null | grep -q .; then
      break
    fi
    sleep 1
  done
  PYTHONUNBUFFERED=1 timeout 50s eneru run --config $E2E_DIR/config-e2e-stats.yaml --exit-after-shutdown > /tmp/test31-daemon.log 2>&1 || true
fi

if [ ! -f "$DB" ]; then
  echo "FAIL: stats DB still missing after seeding attempt"
  ls -la /tmp/eneru-e2e-stats/ 2>&1 || true
  tail -30 /tmp/test31-daemon.log 2>&1 || true
  exit 1
fi

# Inject a known event row directly so we have something to assert.
# TEST31_MARKER is not a priority event type (5.2.2+ default filter), so
# verify with --verbose -- the test scope is "events panel reads from
# SQLite", not "the priority filter is correct".
NOW=$(date +%s)
sqlite3 "$DB" "INSERT INTO events (ts, event_type, detail) VALUES ($NOW, 'TEST31_MARKER', 'e2e injected');"

eneru monitor --once --events-only --verbose --time 1h --config $E2E_DIR/config-e2e-stats.yaml 2>&1 | tee /tmp/test31.log

if ! grep -q "TEST31_MARKER: e2e injected" /tmp/test31.log; then
  echo "FAIL: injected event not surfaced by --events-only --verbose"
  tail -20 /tmp/test31.log
  exit 1
fi
echo "PASS: events panel reads from SQLite"

# ----- 5.2.2: priority filter, --verbose, --full-history -----
# These piggyback on the seeded DB (no extra container churn). Assert:
#   1. Default (no --verbose) hides TEST31_MARKER but shows DAEMON_START.
#   2. --verbose surfaces TEST31_MARKER again.
#   3. --full-history requires --once -- without it the CLI must reject.

echo ""
echo "  --- 5.2.2 events filter checks ---"

eneru monitor --once --events-only --time 1h --config $E2E_DIR/config-e2e-stats.yaml > /tmp/test31-priority.log 2>&1
if grep -q "TEST31_MARKER" /tmp/test31-priority.log; then
  echo "FAIL: priority-only default leaked TEST31_MARKER"
  cat /tmp/test31-priority.log
  exit 1
fi
if ! grep -q "DAEMON_START" /tmp/test31-priority.log; then
  echo "FAIL: priority-only default dropped DAEMON_START"
  cat /tmp/test31-priority.log
  exit 1
fi
echo "  PASS: priority-only filter hides chatter, keeps DAEMON_START"

# --full-history with --once is fine; --full-history without --once must
# reject. We capture stderr because argparse-style errors land there.
if eneru monitor --events-only --full-history --config $E2E_DIR/config-e2e-stats.yaml > /tmp/test31-fhfail.log 2>&1; then
  echo "FAIL: --full-history without --once should have rejected"
  cat /tmp/test31-fhfail.log
  exit 1
fi
if ! grep -q "full-history" /tmp/test31-fhfail.log; then
  echo "FAIL: --full-history rejection message missing the flag name"
  cat /tmp/test31-fhfail.log
  exit 1
fi
echo "  PASS: --full-history without --once rejects"
)

# ======================================================================
# Test 32: Voltage auto-detect re-snaps NUT mis-reported nominal
# (Co-located in the Stats group rather than a dedicated Voltage one
# because it leans on the SQLite events table seeded by Tests 28/31 to
# verify VOLTAGE_AUTODETECT_MISMATCH lands with notification_sent=0.
# A failure under E2E Stats whose log mentions "voltage auto-detect"
# is THIS test, not a stats-writer regression.)
# ======================================================================
(
echo ""
echo ">>> Running: Test 32: Voltage auto-detect re-snaps NUT mis-reported nominal"

echo "=== Test 32: Voltage auto-detect (issue #27) ==="

# Activate the US-grid mis-report scenario: NUT exposes
# input.voltage.nominal=230 but input.voltage stays at ~120V.
cp $E2E_DIR/scenarios/us-grid-misreport.dev \
   $E2E_DIR/scenarios/apply.dev
sleep 3

# Verify the dummy is serving the new scenario.
upsc TestUPS@localhost:3493 input.voltage 2>&1 | head -1
upsc TestUPS@localhost:3493 input.voltage.nominal 2>&1 | head -1

# Single-UPS configs (legacy `ups: { name: ... }` dict form) always
# write the stats DB to `<db_directory>/default.db` -- the
# sanitized-name-based path is reserved for multi-UPS configs.
# Capture the timestamp BEFORE the daemon starts so we can
# filter event-table rows to ones produced by *this* test step
# (default.db is shared with earlier single-UPS tests).
DB="/var/lib/eneru/default.db"
rm -f /tmp/eneru-e2e-voltage-*
T_START=$(date +%s)

# Run for ~15s -- long enough for the autodetect window
# (10 polls at 1 Hz) to fill and the re-snap to fire.
PYTHONUNBUFFERED=1 timeout 15s eneru run \
  --config $E2E_DIR/config-e2e-voltage-autodetect.yaml \
  > /tmp/test32-daemon.log 2>&1 || true

echo "--- daemon log (last 40 lines) ---"
tail -40 /tmp/test32-daemon.log

# Initial threshold log: NUT=230 (snapped only if close enough,
# which 230 already is). Confirms _initialize ran.
if ! grep -q "Voltage Monitoring Active" /tmp/test32-daemon.log; then
  echo "FAIL: voltage init log line missing"
  exit 1
fi
echo "PASS (32a): voltage initialization line emitted"

# Re-snap log: cross-check observed observed-median 120V vs
# NUT's 230V; snap to 120 and announce it.
if ! grep -q "auto-detect re-snap" /tmp/test32-daemon.log; then
  echo "FAIL: auto-detect re-snap line missing"
  exit 1
fi
if ! grep -E "Re-snapped to 120(\\.0)?V" /tmp/test32-daemon.log; then
  echo "FAIL: re-snap target voltage not 120V"
  exit 1
fi
echo "PASS (32b): NUT/observed mismatch re-snapped to 120V"

# SQLite events table must record the auto-detect mismatch
# AND mark notification_sent=0 (the event is in our
# always-silent set; suppression keeps the audit trail).
if [ ! -f "$DB" ]; then
  echo "FAIL: stats DB not created at $DB"
  ls -la /var/lib/eneru/ 2>&1 || true
  exit 1
fi
rows=$(sqlite3 "$DB" "SELECT event_type, notification_sent FROM events WHERE event_type='VOLTAGE_AUTODETECT_MISMATCH' AND ts >= $T_START;")
echo "  fresh events row(s): $rows"
if [ -z "$rows" ]; then
  echo "FAIL: VOLTAGE_AUTODETECT_MISMATCH row not in events table"
  sqlite3 "$DB" "SELECT ts, event_type, notification_sent FROM events ORDER BY ts DESC LIMIT 20;"
  exit 1
fi
if ! echo "$rows" | grep -q "VOLTAGE_AUTODETECT_MISMATCH|0"; then
  echo "FAIL: VOLTAGE_AUTODETECT_MISMATCH should have notification_sent=0"
  exit 1
fi
echo "PASS (32c): events table records mismatch with notification_sent=0"

# Schema version must match SCHEMA_VERSION in src/eneru/stats.py — the
# value bumps on each migration (3 in v5.1.x, 4 in v5.2.0). Read the
# expected value from the package itself rather than hard-coding so this
# assertion doesn't keep breaking on every schema change.
expected_ver=$(python3 -c "from eneru.stats import SCHEMA_VERSION; print(SCHEMA_VERSION)")
ver=$(sqlite3 "$DB" "SELECT value FROM meta WHERE key='schema_version';")
if [ "$ver" != "$expected_ver" ]; then
  echo "FAIL: expected schema_version=$expected_ver, got '$ver'"
  exit 1
fi
echo "PASS (32d): meta.schema_version=$ver"

# Restore for downstream tests
cp $E2E_DIR/scenarios/online-charging.dev \
   $E2E_DIR/scenarios/apply.dev

echo ""
echo "=== Test 32 PASSED: voltage auto-detect verified ==="
)

# ======================================================================
# Test 34: v5.2 panic-attack coalescing (Slice 4)
#
# Cycle ON_BATTERY → POWER_RESTORED with notifications pointed at an
# unreachable Apprise endpoint (RFC 5737 TEST-NET-1). Both rows stay
# 'pending' in SQLite, the worker's coalescer folds them into one
# "Brief Power Outage" summary, and the originals get cancel_reason=
# 'coalesced'. This is the only direct E2E coverage of the slice 4
# coalescer; per-rule unit tests carry the rest.
# ======================================================================
(
echo ""
echo ">>> Running: Test 34: panic-attack coalescing"

echo "=== Test 34: panic-attack notification coalescing ==="

# Clean slate.
rm -rf /tmp/eneru-e2e-coalesce /tmp/eneru-e2e-coalesce-*
mkdir -p /tmp/eneru-e2e-coalesce
rm -f /tmp/eneru-e2e-coalesce-shutdown-flag

# Start from on-line.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Run eneru in the background — we drive the NUT scenario from the
# foreground while the worker thread cycles through events.
eneru run --config $E2E_DIR/config-e2e-coalesce.yaml > /tmp/test34.log 2>&1 &
ENERU_PID=$!
sleep 5  # let _initialize finish + register store + reach steady state

# Trigger the outage.
cp "$E2E_DIR/scenarios/on-battery.dev" "$E2E_DIR/scenarios/apply.dev"
sleep 5  # ON_BATTERY event fires + lands in DB pending
# Restore power.
cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply.dev"
sleep 2  # POWER_RESTORED fires + lands in DB

# Poll for the coalesced state instead of a fixed sleep — we don't
# want to race the worker thread's iteration cadence (it's blocked
# attempting Apprise calls to TEST-NET-1, but with timeout=1 in the
# config it cycles every second). 20 s ceiling; well below the SIGTERM
# kill path's flush(timeout=5) so we never starve that.
DB_DIR=/tmp/eneru-e2e-coalesce
for i in $(seq 1 20); do
  DB_PROBE=$(find "$DB_DIR" -maxdepth 1 -name '*.db' 2>/dev/null | head -1)
  if [ -n "$DB_PROBE" ]; then
    c=$(sqlite3 "$DB_PROBE" \
      "SELECT COUNT(*) FROM notifications \
       WHERE category IN ('power_event_on_battery','power_event_on_line') \
         AND status='cancelled' AND cancel_reason='coalesced';" \
      2>/dev/null || echo 0)
    [ "$c" = "2" ] && break
  fi
  sleep 1
done

# Stop eneru cleanly (SIGTERM → _cleanup_and_exit → flush(5)).
kill -TERM $ENERU_PID 2>/dev/null || true
wait $ENERU_PID 2>/dev/null || true

DB=$(find "$DB_DIR" -maxdepth 1 -name '*.db' 2>/dev/null | head -1)
if [ -z "$DB" ]; then
  echo "FAIL: no SQLite stats DB created at $DB_DIR/"
  cat /tmp/test34.log
  exit 1
fi

# Originals: 2 rows cancelled with reason='coalesced'. Production tags
# the originals with sub-typed categories (power_event_on_battery /
# power_event_on_line) so the coalescer can pair them by exact match
# instead of grepping body text — see the ddbd886 hotfix.
coalesced=$(sqlite3 "$DB" \
  "SELECT COUNT(*) FROM notifications \
   WHERE category IN ('power_event_on_battery','power_event_on_line') \
     AND status='cancelled' \
     AND cancel_reason='coalesced';")
if [ "$coalesced" -ne 2 ]; then
  echo "FAIL: expected 2 cancelled (coalesced) power_event rows, got $coalesced"
  # Dump ALL notification rows so a category-naming regression is visible
  # (was hiding a real issue earlier when the filter was still scoped to
  # category='power_event').
  echo "--- ALL notifications rows: ---"
  sqlite3 "$DB" "SELECT id, ts, category, status, cancel_reason, substr(body,1,80) FROM notifications ORDER BY id;"
  echo "--- /tmp/test34.log: ---"
  cat /tmp/test34.log
  exit 1
fi
echo "PASS (34a): 2 power_event rows cancelled with reason='coalesced'"

# Summary: 1 pending row whose body says "Brief Power Outage".
summary=$(sqlite3 "$DB" \
  "SELECT body FROM notifications \
   WHERE category='power_event' AND status='pending' \
     AND body LIKE '%Brief Power Outage%';")
if [ -z "$summary" ]; then
  echo "FAIL: no pending 'Brief Power Outage' summary row found"
  sqlite3 "$DB" "SELECT id, ts, category, status, cancel_reason, substr(body,1,80) FROM notifications ORDER BY id;"
  cat /tmp/test34.log
  exit 1
fi
echo "PASS (34b): coalesced summary row present and pending"

echo ""
echo "=== Test 34 PASSED: panic-attack coalescing verified ==="
)


# ======================================================================
# Test 35: v5.2.1 single-restart-notification
#
# Contract: a `systemctl restart eneru` (i.e. SIGTERM → start) produces
# exactly ONE pending lifecycle row at any time, never two. The old
# daemon enqueues "Service Stopped" AFTER flush() so the row stays
# pending; the new daemon's classify_startup runs cancel_notification on
# pending lifecycle rows ('superseded') before emitting the new
# "Restarted" message. Reproduces the v5.2.0 bug from TODO.md.
# ======================================================================
(
echo ""
echo ">>> Running: Test 35: single-restart-notification (v5.2.1)"

echo "=== Test 35: v5.2.1 single-restart-notification ==="

# Clean slate.
rm -rf /tmp/eneru-e2e-restart /tmp/eneru-e2e-restart-*
mkdir -p /tmp/eneru-e2e-restart
rm -f /tmp/eneru-e2e-restart-shutdown-flag

# Start from on-line so the daemon doesn't try to trigger anything
# unrelated while we're testing the lifecycle path.
cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply.dev"
sleep 3

# --- First run: clean start, then SIGTERM. ---
eneru run --config "$E2E_DIR/config-e2e-restart.yaml" > /tmp/test35-run1.log 2>&1 &
ENERU_PID=$!
sleep 5  # _initialize finishes, lifecycle classifier emits DAEMON_START
kill -TERM $ENERU_PID 2>/dev/null || true
wait $ENERU_PID 2>/dev/null || true

DB=$(find /tmp/eneru-e2e-restart -maxdepth 1 -name '*.db' 2>/dev/null | head -1)
if [ -z "$DB" ]; then
  echo "FAIL: no SQLite stats DB created at /tmp/eneru-e2e-restart/"
  cat /tmp/test35-run1.log
  exit 1
fi

# After first SIGTERM: exactly one pending lifecycle row containing
# "Service Stopped" (the unreachable Apprise endpoint guarantees the
# worker can't deliver it within flush(timeout=5), so it stays pending).
pending_stop=$(sqlite3 "$DB" \
  "SELECT COUNT(*) FROM notifications \
   WHERE category='lifecycle' AND status='pending' \
     AND body LIKE '%Service Stopped%';")
if [ "$pending_stop" != "1" ]; then
  echo "FAIL (35a): expected 1 pending lifecycle 'Service Stopped' row, got $pending_stop"
  echo "--- ALL notifications rows: ---"
  sqlite3 "$DB" "SELECT id, ts, category, status, cancel_reason, substr(body,1,80) FROM notifications ORDER BY id;"
  echo "--- /tmp/test35-run1.log: ---"
  cat /tmp/test35-run1.log
  exit 1
fi
echo "PASS (35a): old daemon left exactly 1 pending lifecycle 'Service Stopped' row"

# --- Second run: same config, simulating `systemctl restart`. ---
eneru run --config "$E2E_DIR/config-e2e-restart.yaml" > /tmp/test35-run2.log 2>&1 &
ENERU_PID=$!
sleep 6  # _initialize → classify_startup → cancel pending + send Restarted

# Stop cleanly so the test environment isn't left with a runaway daemon.
kill -TERM $ENERU_PID 2>/dev/null || true
wait $ENERU_PID 2>/dev/null || true

# After the second daemon's classify_startup fires:
#   - The first run's "Service Stopped" row is now status='cancelled'
#     with cancel_reason='superseded' (set by _emit_lifecycle_startup_
#     notification BEFORE the new lifecycle row is enqueued).
#   - A new lifecycle row containing "Restarted" is in the table.
superseded=$(sqlite3 "$DB" \
  "SELECT COUNT(*) FROM notifications \
   WHERE category='lifecycle' AND status='cancelled' \
     AND cancel_reason='superseded' \
     AND body LIKE '%Service Stopped%';")
if [ "$superseded" != "1" ]; then
  echo "FAIL (35b): expected 1 cancelled (superseded) 'Service Stopped' row, got $superseded"
  echo "--- ALL notifications rows: ---"
  sqlite3 "$DB" "SELECT id, ts, category, status, cancel_reason, substr(body,1,80) FROM notifications ORDER BY id;"
  echo "--- /tmp/test35-run2.log: ---"
  cat /tmp/test35-run2.log
  exit 1
fi
echo "PASS (35b): prior 'Service Stopped' row is cancelled with reason='superseded'"

restarted=$(sqlite3 "$DB" \
  "SELECT COUNT(*) FROM notifications \
   WHERE category='lifecycle' \
     AND body LIKE '%Restarted%';")
if [ "$restarted" -lt "1" ]; then
  echo "FAIL (35c): expected at least 1 'Restarted' row, got $restarted"
  echo "--- ALL notifications rows: ---"
  sqlite3 "$DB" "SELECT id, ts, category, status, cancel_reason, substr(body,1,80) FROM notifications ORDER BY id;"
  cat /tmp/test35-run2.log
  exit 1
fi
echo "PASS (35c): new daemon emitted a 'Restarted' lifecycle row"

echo ""
echo "=== Test 35 PASSED: single notification per restart verified ==="
)

echo ""
echo "=== Group 'stats' completed successfully ==="
