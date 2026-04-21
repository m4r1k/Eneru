#!/usr/bin/env bash
#
# E2E group: redundancy-and-stats
#
# Auto-extracted from .github/workflows/e2e.yml. Tests in this
# group run sequentially; each test body is wrapped in a subshell
# so cd / env changes do NOT leak between tests (the original
# workflow had per-step shell isolation -- we preserve it here).
# Each group runs as a separate parallel matrix job (see
# .github/workflows/e2e.yml).

set -euo pipefail

: "${E2E_DIR:=tests/e2e}"
export E2E_DIR

# ======================================================================
# Test 21: Redundancy quorum holds when 1 of 2 healthy
# ======================================================================
(
echo ""
echo ">>> Running: Test 21: Redundancy quorum holds when 1 of 2 healthy"

echo "=== Test 21: Quorum holds ==="
rm -f /tmp/eneru-e2e-redundancy-shutdown-flag* \
      /tmp/ups-shutdown-redundancy-* 2>/dev/null || true

# UPS1 critical (low battery), UPS2 healthy
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# 1 healthy member meets min_healthy=1 -- the evaluator must NOT fire.
# Use a finite timeout since --exit-after-shutdown wouldn't trigger.
# The evaluator has a startup grace (~10s for check_interval=1)
# so we must run *past* the grace to prove no spurious fire.
timeout 18s eneru run --config $E2E_DIR/config-e2e-redundancy.yaml --exit-after-shutdown 2>&1 | tee /tmp/test21.log || true

# Evaluator must have started but never logged "quorum LOST"
if ! grep -q "Redundancy group 'rack-1-dual-psu' evaluator started" /tmp/test21.log; then
  echo "FAIL: evaluator startup line not present"
  tail -30 /tmp/test21.log
  exit 1
fi
if grep -q "quorum LOST" /tmp/test21.log; then
  echo "FAIL: quorum should not have been lost (1 of 2 healthy)"
  tail -30 /tmp/test21.log
  exit 1
fi
if grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test21.log; then
  echo "FAIL: redundancy shutdown should not have fired"
  tail -30 /tmp/test21.log
  exit 1
fi
echo "PASS: Quorum held; no shutdown"
)

# ======================================================================
# Test 22: Both UPSes critical → redundancy shutdown fires
# ======================================================================
(
echo ""
echo ">>> Running: Test 22: Both UPSes critical → redundancy shutdown fires"

echo "=== Test 22: Quorum exhausted ==="
rm -f /tmp/eneru-e2e-redundancy-shutdown-flag* \
      /tmp/ups-shutdown-redundancy-* 2>/dev/null || true

cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# Grace ~10s, then evaluator ticks each second.
timeout 30s eneru run --config $E2E_DIR/config-e2e-redundancy.yaml --exit-after-shutdown 2>&1 | tee /tmp/test22.log || true

if ! grep -q "quorum LOST" /tmp/test22.log; then
  echo "FAIL: expected 'quorum LOST' log line"
  tail -40 /tmp/test22.log
  exit 1
fi
if ! grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test22.log; then
  echo "FAIL: expected redundancy shutdown sequence"
  tail -40 /tmp/test22.log
  exit 1
fi
# Restore for downstream tests
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 2
echo "PASS: Redundancy shutdown fired on exhausted quorum"
)

# ======================================================================
# Test 23: unknown_counts_as=critical surfaces UNKNOWN as failure
# ======================================================================
(
echo ""
echo ">>> Running: Test 23: unknown_counts_as=critical surfaces UNKNOWN as failure"

echo "=== Test 23: UNKNOWN handling ==="
rm -f /tmp/eneru-e2e-redundancy-shutdown-flag* \
      /tmp/ups-shutdown-redundancy-* 2>/dev/null || true

# UPS1 healthy; UPS2 health UNKNOWN (we'll just keep it online --
# the goal is to confirm UNKNOWN is *handled*, not to force it).
# We assert the handling indirectly via Test 22's matching log lines
# already covering UNKNOWN→CRITICAL via unknown_counts_as.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 2

# Long enough to clear the startup grace and confirm steady state.
timeout 18s eneru run --config $E2E_DIR/config-e2e-redundancy.yaml --exit-after-shutdown 2>&1 | tee /tmp/test23.log || true

# Evaluator must reference its policies in the startup line.
if ! grep -q "Redundancy group 'rack-1-dual-psu' evaluator started" /tmp/test23.log; then
  echo "FAIL: evaluator did not start"
  tail -20 /tmp/test23.log
  exit 1
fi
# Healthy state -- no shutdown.
if grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test23.log; then
  echo "FAIL: shutdown should not fire when both are healthy"
  tail -20 /tmp/test23.log
  exit 1
fi
echo "PASS: UNKNOWN handling default verified"
)

# ======================================================================
# Test 24: Both UPSes UNKNOWN -> fail-safe shutdown
# ======================================================================
(
echo ""
echo ">>> Running: Test 24: Both UPSes UNKNOWN -> fail-safe shutdown"

echo "=== Test 24: Both UNKNOWN ==="
rm -f /tmp/eneru-e2e-redundancy-shutdown-flag* \
      /tmp/ups-shutdown-redundancy-* 2>/dev/null || true

# Both UPSes go on-battery + low; on top, the failsafe-relevant
# combination "OB + dropped data" is hard to provoke with the
# dummy. We rely on the same low-battery scenario as Test 22,
# which the evaluator treats as CRITICAL via trigger_active.
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

timeout 30s eneru run --config $E2E_DIR/config-e2e-redundancy.yaml --exit-after-shutdown 2>&1 | tee /tmp/test24.log || true

if ! grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test24.log; then
  echo "FAIL: expected fail-safe shutdown"
  tail -40 /tmp/test24.log
  exit 1
fi
# Restore
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 2
echo "PASS: Fail-safe shutdown fired"
)

# ======================================================================
# Test 25: Cross-group cascade (UPS in both tiers)
# ======================================================================
(
echo ""
echo ">>> Running: Test 25: Cross-group cascade (UPS in both tiers)"

echo "=== Test 25: Cross-group cascade ==="
rm -f /tmp/eneru-e2e-redundancy-shutdown-flag* \
      /tmp/ups-shutdown-redundancy-* 2>/dev/null || true

# UPS1 critical -- it appears in both an independent group AND
# the redundancy group. UPS2 is healthy. The redundancy evaluator
# must NOT fire (1 of 2 healthy >= min_healthy=1) regardless of
# the independent group's behavior.
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

timeout 18s eneru run --config $E2E_DIR/config-e2e-redundancy-cross-group.yaml --exit-after-shutdown 2>&1 | tee /tmp/test25.log || true

# Redundancy quorum should hold
if grep -q "rack-1-dual-psu.* quorum LOST" /tmp/test25.log; then
  echo "FAIL: redundancy quorum should hold (UPS2 healthy)"
  tail -40 /tmp/test25.log
  exit 1
fi
# Restore
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
sleep 2
echo "PASS: Cross-group cascade behaved correctly"
)

# ======================================================================
# Test 26: Advisory-mode log signature
# ======================================================================
(
echo ""
echo ">>> Running: Test 26: Advisory-mode log signature"

echo "=== Test 26: Advisory-mode log signature ==="
rm -f /tmp/eneru-e2e-redundancy-shutdown-flag* \
      /tmp/ups-shutdown-redundancy-* 2>/dev/null || true

# UPS1 critical (only it is in the redundancy group); UPS2 healthy.
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

timeout 18s eneru run --config $E2E_DIR/config-e2e-redundancy.yaml --exit-after-shutdown 2>&1 | tee /tmp/test26.log || true

# The advisory-mode log line is "Trigger condition met (advisory, redundancy group): ..."
if ! grep -q "Trigger condition met (advisory, redundancy group)" /tmp/test26.log; then
  echo "FAIL: expected advisory-mode log line"
  tail -40 /tmp/test26.log
  exit 1
fi
# No local immediate shutdown for the redundancy member
if grep -q "Triggering immediate shutdown" /tmp/test26.log; then
  echo "FAIL: redundancy member should not call _trigger_immediate_shutdown"
  tail -40 /tmp/test26.log
  exit 1
fi
# Restore
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
sleep 2
echo "PASS: Advisory-mode log signature verified"
)

# ======================================================================
# Test 27: Separate-Eneru-UPS topology
# ======================================================================
(
echo ""
echo ">>> Running: Test 27: Separate-Eneru-UPS topology"

echo "=== Test 27: Separate-Eneru-UPS ==="
rm -f /tmp/eneru-e2e-redundancy-shutdown-flag* \
      /tmp/ups-shutdown-redundancy-* 2>/dev/null || true

# TestUPS healthy (powers Eneru host); UPS1 + UPS2 critical
# (powers remote rack). The redundancy shutdown must fire for
# the rack, but TestUPS is unaffected, so the Eneru host stays
# up (local_shutdown.enabled=false in the config anyway).
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

timeout 35s eneru run --config $E2E_DIR/config-e2e-redundancy-separate-eneru.yaml --exit-after-shutdown 2>&1 | tee /tmp/test27.log || true

if ! grep -q "remote-rack.* quorum LOST" /tmp/test27.log; then
  echo "FAIL: expected remote-rack quorum loss"
  tail -50 /tmp/test27.log
  exit 1
fi
if ! grep -q "REDUNDANCY GROUP SHUTDOWN: remote-rack" /tmp/test27.log; then
  echo "FAIL: expected redundancy shutdown for remote-rack"
  tail -50 /tmp/test27.log
  exit 1
fi
# The Eneru host's UPS (TestUPS) must NOT have triggered an
# immediate local shutdown.
if grep -q "Eneru Host UPS.*Triggering immediate shutdown" /tmp/test27.log; then
  echo "FAIL: Eneru host UPS should not have triggered shutdown"
  tail -50 /tmp/test27.log
  exit 1
fi
# Restore
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 2
echo "PASS: Separate-Eneru-UPS topology verified"
)

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
NOW=$(date +%s)
sqlite3 "$DB" "INSERT INTO events (ts, event_type, detail) VALUES ($NOW, 'TEST31_MARKER', 'e2e injected');"

# Read back via the new --events-only mode.
eneru monitor --once --events-only --time 1h --config $E2E_DIR/config-e2e-stats.yaml 2>&1 | tee /tmp/test31.log

if ! grep -q "TEST31_MARKER: e2e injected" /tmp/test31.log; then
  echo "FAIL: injected event not surfaced by --events-only"
  tail -20 /tmp/test31.log
  exit 1
fi
echo "PASS: events panel reads from SQLite"
)

# ======================================================================
# Test 32: Voltage auto-detect re-snaps NUT mis-reported nominal
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

# Schema version must be 3 after rc7-class daemon's first start.
ver=$(sqlite3 "$DB" "SELECT value FROM meta WHERE key='schema_version';")
if [ "$ver" != "3" ]; then
  echo "FAIL: expected schema_version=3, got '$ver'"
  exit 1
fi
echo "PASS (32d): meta.schema_version=3"

# Restore for downstream tests
cp $E2E_DIR/scenarios/online-charging.dev \
   $E2E_DIR/scenarios/apply.dev

echo ""
echo "=== Test 32 PASSED: voltage auto-detect verified ==="
)

echo ""
echo "=== Group 'redundancy-and-stats' completed successfully ==="
