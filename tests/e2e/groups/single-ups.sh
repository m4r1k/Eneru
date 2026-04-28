#!/usr/bin/env bash
#
# E2E group: single-ups
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
# Test 2: Monitor normal state (no shutdown triggered)
# ======================================================================
(
echo ""
echo ">>> Running: Test 2: Monitor normal state (no shutdown triggered)"

echo "=== Test 2: Normal State Monitoring ==="

# Ensure UPS is in online state
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Run Eneru for 5 seconds — should NOT trigger shutdown.
# Capture eneru's exit code explicitly; the previous `|| true` masked
# any crash (e.g. exit 1) and let the test pass even when eneru never
# actually ran. The ONLY acceptable exit is 124 (SIGTERM from timeout),
# which proves the daemon was still running when the timer hit. A
# clean 0 here would mean the daemon exited on its own — premature
# termination during a "monitor normal state" check is itself a bug
# this test must surface.
set +e
timeout 5 eneru run --config $E2E_DIR/config-e2e-dry-run.yaml 2>&1 | tee /tmp/test2.log
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 124 ]; then
  echo "FAIL: eneru exited with code $RC (expected 124 = killed by timeout)"
  cat /tmp/test2.log
  exit 1
fi

# Verify no shutdown was triggered
if grep -q "SHUTDOWN SEQUENCE" /tmp/test2.log; then
  echo "FAIL: Shutdown was triggered during normal operation!"
  exit 1
fi

echo "PASS: No shutdown triggered during normal operation"
)

# ======================================================================
# Test 3: Detect power failure (dry-run)
# ======================================================================
(
echo ""
echo ">>> Running: Test 3: Detect power failure (dry-run)"

echo "=== Test 3: Power Failure Detection ==="

# Clean up any previous shutdown flags
rm -f /tmp/eneru-e2e-shutdown-flag

# Switch to low battery scenario
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Run Eneru in dry-run mode. With --exit-after-shutdown, eneru should
# exit 0 once the dry-run shutdown sequence completes; anything else
# is a real failure that the previous `|| true` was masking.
set +e
eneru run --config $E2E_DIR/config-e2e-dry-run.yaml --exit-after-shutdown 2>&1 | tee /tmp/test3.log
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 0 ]; then
  echo "FAIL: eneru exited with code $RC (expected 0)"
  cat /tmp/test3.log
  exit 1
fi

# Verify shutdown was triggered (in dry-run)
if ! grep -q "SHUTDOWN SEQUENCE" /tmp/test3.log; then
  echo "FAIL: Shutdown was NOT triggered for low battery!"
  cat /tmp/test3.log
  exit 1
fi

if ! grep -q "DRY-RUN" /tmp/test3.log; then
  echo "FAIL: Dry-run mode not indicated!"
  exit 1
fi

echo "PASS: Low battery correctly triggered shutdown (dry-run)"
)

# ======================================================================
# Test 4: SSH remote shutdown
# ======================================================================
(
echo ""
echo ">>> Running: Test 4: SSH remote shutdown"

echo "=== Test 4: SSH Remote Shutdown ==="

cd $E2E_DIR

# Reset SSH target state
docker compose exec -T ssh-target sh -c "rm -f /var/run/shutdown-triggered && touch /var/run/server-alive"

# Clean up shutdown flag
rm -f /tmp/eneru-e2e-shutdown-flag

# Switch to low battery scenario
cp scenarios/low-battery.dev scenarios/apply.dev
sleep 3

# Run Eneru briefly - will trigger shutdown and send SSH command
eneru run --config config-e2e.yaml --exit-after-shutdown 2>&1 | tee /tmp/test4.log || true

echo ""
echo "=== Verifying SSH shutdown ==="

# Verify SSH shutdown command was sent
if docker compose exec -T ssh-target test -f /var/run/shutdown-triggered; then
  echo "PASS: SSH shutdown command was received"
else
  echo "FAIL: SSH shutdown command was NOT received"
  docker compose logs ssh-target
  exit 1
fi

# Check the shutdown log
echo "SSH target shutdown log:"
docker compose exec -T ssh-target cat /var/log/shutdown.log || true

echo ""
echo "=== Test 4 PASSED: SSH remote shutdown executed successfully ==="
)

# ======================================================================
# Test 5: FSD flag triggers immediate shutdown
# ======================================================================
(
echo ""
echo ">>> Running: Test 5: FSD flag triggers immediate shutdown"

echo "=== Test 5: FSD Trigger ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag

# Switch to FSD scenario
cp $E2E_DIR/scenarios/fsd.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Run Eneru in dry-run mode
eneru run --config $E2E_DIR/config-e2e-dry-run.yaml --exit-after-shutdown 2>&1 | tee /tmp/test5.log || true

# Verify FSD triggered shutdown
if ! grep -q "FSD" /tmp/test5.log; then
  echo "FAIL: FSD was not detected!"
  cat /tmp/test5.log
  exit 1
fi

echo "PASS: FSD correctly triggered shutdown"
)

# ======================================================================
# Test 6: Voltage event detection
# ======================================================================
(
echo ""
echo ">>> Running: Test 6: Voltage event detection"

echo "=== Test 6: Voltage Events ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag

# Start with normal state
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 2

# Switch to brownout scenario
cp $E2E_DIR/scenarios/brownout.dev $E2E_DIR/scenarios/apply.dev
sleep 2

# Run briefly to detect brownout
timeout 8 eneru run --config $E2E_DIR/config-e2e-dry-run.yaml 2>&1 | tee /tmp/test6.log || true

# Verify brownout was specifically detected -- not just any voltage
# log line. The startup `Voltage Monitoring Active` line would match
# `voltage` and let a regression slip through; require the actual
# BROWNOUT_DETECTED event marker.
if grep -q "BROWNOUT_DETECTED" /tmp/test6.log; then
  echo "PASS (6a): BROWNOUT_DETECTED event fired"
else
  echo "FAIL (6a): brownout scenario did not produce BROWNOUT_DETECTED log"
  exit 1
fi

# rc9: startup log line should expose BOTH the grid-quality warning
# thresholds AND (when NUT reports them) the UPS battery-switch points.
# Operators rely on both lines to understand whether a notification
# means "grid is wobbly" vs "UPS is about to switch".
if grep -q "Grid-quality warnings:" /tmp/test6.log; then
  echo "PASS (6b): startup log shows Grid-quality warnings line"
else
  echo "FAIL (6b): startup log missing 'Grid-quality warnings:' line"
  exit 1
fi

# Conditionally hard-assert the UPS battery-switch-points line: if NUT
# actually reports input.transfer.{low,high} for this UPS, the line MUST
# be in the log -- otherwise rc9's startup-summary regression would
# slip through silently. Probe upsc directly to decide.
if upsc TestUPS@localhost:3493 2>/dev/null | grep -qE "^input\.transfer\.(low|high):"; then
  if grep -q "UPS battery-switch points:" /tmp/test6.log; then
    echo "PASS (6c): startup log shows UPS battery-switch points line"
  else
    echo "FAIL (6c): NUT reports input.transfer.{low,high} but startup"
    echo "  log is missing the 'UPS battery-switch points:' line."
    exit 1
  fi
else
  echo "Note (6c): NUT does not expose input.transfer.{low,high} for this"
  echo "  driver -- skipping the UPS battery-switch points assertion."
fi

# rc9: the BROWNOUT detail must include the % deviation framing.
# Notification dispatch is gated by hysteresis (default 30s) so it
# may not fire in our 8s window, but the immediate BROWNOUT_DETECTED
# log row carries the same detail string -- so this MUST be present.
if grep -q "below.*nominal" /tmp/test6.log; then
  echo "PASS (6d): brownout log carries 'below nominal' framing"
else
  echo "FAIL (6d): brownout log missing rc9 '<X>% below <Y>V nominal' framing"
  exit 1
fi
)

# ======================================================================
# Test 7: Notification delivery
# ======================================================================
(
echo ""
echo ">>> Running: Test 7: Notification delivery"

echo "=== Test 7: Notification Delivery ==="

# Use ${VAR:-} default expansion so set -u doesn't abort on PR runs
# where the secret isn't injected (forks, first-run PRs, etc.).
if [ -z "${E2E_NOTIFICATION_URL:-}" ]; then
  echo "SKIP: E2E_NOTIFICATION_URL secret not configured"
  exit 0
fi

# Substitute the URL via env-var + python rather than sed, since sed's
# replacement string treats `&` as backref and would corrupt URLs that
# contain `&`. python's str.replace is literal.
URL="$E2E_NOTIFICATION_URL" python3 - <<'PY' > /tmp/config-notif.yaml
import os, sys, pathlib
src = pathlib.Path(os.environ["E2E_DIR"]) / "config-e2e-notifications.yaml"
sys.stdout.write(src.read_text().replace("${E2E_NOTIFICATION_URL}", os.environ["URL"]))
PY

# Test notification delivery
eneru test-notifications --config /tmp/config-notif.yaml

echo "PASS: Notification sent successfully"
)

# ======================================================================
# Test 33: Issue #4 -- voltage_sensitivity preset prevents Chris's
# false-alarm flood on a US 120V grid running slightly hot.
# ======================================================================
(
echo ""
echo ">>> Running: Test 33: voltage_sensitivity preset (issue #4)"

echo "=== Test 33: voltage_sensitivity preset ==="

rm -f /tmp/eneru-e2e-shutdown-flag

# Apply Chris's exact NUT data: 120V nominal, transfer 106/127, input
# voltage at a routine 122.4V. v5.1.1 would have set warning_high=122
# and false-alarmed; v5.1.2 default 'normal' (10%) sets it to 132.
#
# Unlike Test 6 (where every scenario shares input.voltage.nominal=230),
# this test CHANGES the nominal between the prior scenario and Chris's
# 120V scenario. The dummy NUT scenario watcher polls /scenarios every
# 1s, then dummy-ups has its own pollinterval before upsd serves the new
# value -- a blind `sleep 2` races. Active-poll `upsc` until it returns
# input.voltage.nominal=120 before launching the daemon, so the daemon's
# one-shot _initialize_voltage_thresholds reads the right nominal.
cp $E2E_DIR/scenarios/us-grid-hot.dev $E2E_DIR/scenarios/apply.dev
for i in $(seq 1 20); do
  nominal=$(upsc TestUPS@localhost:3493 input.voltage.nominal 2>/dev/null || true)
  if [ "$nominal" = "120" ]; then
    echo "  NUT serving nominal=120 after ${i}s"
    break
  fi
  sleep 1
done
if [ "$nominal" != "120" ]; then
  echo "FAIL (8-setup): NUT never reported input.voltage.nominal=120 (last=${nominal:-empty})"
  exit 1
fi

timeout 12 eneru run --config $E2E_DIR/config-e2e-dry-run.yaml 2>&1 | tee /tmp/test33.log || true

# (8a) Startup log must report the percentage-band threshold honestly.
if grep -q "Grid-quality warnings: 108.0V / 132.0V" /tmp/test33.log \
   && grep -q "sensitivity=normal" /tmp/test33.log; then
  echo "PASS (8a): startup log honest -- 108/132 at sensitivity=normal"
else
  echo "FAIL (8a): startup log missing 108/132 or sensitivity=normal"
  grep -E "Grid-quality|sensitivity" /tmp/test33.log || true
  exit 1
fi

# (8b) NO false OVER_VOLTAGE_DETECTED at 122.4V.
if grep -q "OVER_VOLTAGE_DETECTED" /tmp/test33.log; then
  echo "FAIL (8b): 122.4V on a 120V/106/127 UPS must NOT fire OVER_VOLTAGE"
  grep "OVER_VOLTAGE" /tmp/test33.log || true
  exit 1
else
  echo "PASS (8b): no false OVER_VOLTAGE_DETECTED at 122.4V"
fi

# (8c) Drop to 107V (real brownout, just under the 108V warning_low).
# Brownout MUST fire even though false alarms are gone. Active-poll on
# input.voltage so we don't race the dummy NUT's reload cycle.
cp $E2E_DIR/scenarios/us-grid-brownout.dev $E2E_DIR/scenarios/apply.dev
for i in $(seq 1 20); do
  voltage=$(upsc TestUPS@localhost:3493 input.voltage 2>/dev/null || true)
  if [ "$voltage" = "107.0" ] || [ "$voltage" = "107" ]; then
    echo "  NUT serving input.voltage=${voltage} after ${i}s"
    break
  fi
  sleep 1
done
if [ "$voltage" != "107.0" ] && [ "$voltage" != "107" ]; then
  echo "FAIL (8c-setup): NUT never reported input.voltage=107 (last=${voltage:-empty})"
  exit 1
fi
timeout 8 eneru run --config $E2E_DIR/config-e2e-dry-run.yaml 2>&1 | tee /tmp/test33b.log || true

if grep -q "BROWNOUT_DETECTED" /tmp/test33b.log; then
  echo "PASS (8c): real brownout (107V) still fires BROWNOUT_DETECTED"
else
  echo "FAIL (8c): brownout at 107V must still fire on the v5.1.2 formula"
  tail -30 /tmp/test33b.log
  exit 1
fi

# (8d) Migration warning fires for narrow-firmware UPSes. v5.1.1 would
# have produced 111/122 on this UPS; v5.1.2 produces 108/132. The
# warning lists the per-side delta and exposes the legacy values so an
# upgrading operator can spot the change end-to-end.
if grep -q "Voltage warning band changed from v5.1.1" /tmp/test33.log \
   && grep -q "low 111.0V" /tmp/test33.log \
   && grep -q "high 122.0V" /tmp/test33.log; then
  echo "PASS (8d): migration warning surfaces with per-side delta against v5.1.1 numbers"
else
  echo "FAIL (8d): migration warning missing or per-side delta absent"
  grep -E "Voltage warning band|widened|tightened" /tmp/test33.log || true
  exit 1
fi

# Restore baseline scenario for any downstream tests added later.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
)

# ======================================================================
# Test 34: Shutdown re-arm on POWER_RESTORED (5.2.2 / bug #4)
#
# The shutdown sequence creates a flag file as a re-entry guard. In the
# local_shutdown.enabled=true real-mode path the daemon doesn't clear
# it, because it expects the OS reboot to take it down within seconds.
# On a healthy production install systemd reaps it before this matters,
# but on edge installs (custom shutdown command, sandboxed env, dummy
# UPS test rig) the host stays up and the second outage's trigger
# silently no-ops. _handle_on_line now clears the flag on OB->OL.
#
# We exercise this with local_shutdown.enabled=true + dry_run=false +
# shutdown_command=/bin/true so the daemon "shuts down" the host
# (no-op) but keeps polling. Any regression here would silently drop
# the second EMERGENCY_SHUTDOWN_INITIATED row.
# ======================================================================
(
echo ""
echo ">>> Running: Test 34: Shutdown re-arm on POWER_RESTORED (bug #4)"
echo "=== Test 34: re-arm after POWER_RESTORED ==="

REARM_DIR=/tmp/eneru-e2e-rearm
rm -rf $REARM_DIR
mkdir -p $REARM_DIR

# Inline config: local shutdown "enabled" but the command is a no-op,
# so the daemon issues "shutdown" and keeps running. The flag file
# would persist forever pre-5.2.2, blocking the second trigger.
cat > $REARM_DIR/config.yaml <<EOF
ups:
  name: "TestUPS@localhost:3493"
  check_interval: 1
  max_stale_data_tolerance: 3
triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600
  depletion:
    grace_period: 5
  extended_time:
    enabled: false
behavior:
  dry_run: false
logging:
  file: null
  state_file: $REARM_DIR/state
  battery_history_file: $REARM_DIR/history
  shutdown_flag_file: $REARM_DIR/shutdown-flag
statistics:
  db_directory: $REARM_DIR
notifications:
  enabled: false
virtual_machines:
  enabled: false
containers:
  enabled: false
filesystems:
  sync_enabled: false
  unmount:
    enabled: false
local_shutdown:
  enabled: true
  command: "/bin/true e2e-rearm-noop"
  wall: false
EOF

# Pin the assumptions this test makes about the scenario contents.
# If a future scenario tweak nudges low-battery.dev's battery.charge
# above the trigger threshold (or online-charging.dev away from OL),
# the scenarios silently stop driving the OB/OL transitions and this
# test would still "pass" without exercising the re-arm path. Fail
# loud instead. Scenario format is plain ``key: value`` lines; battery
# charge can be a float like ``14`` or ``14.0``.
LB_CHARGE=$(awk '/^battery\.charge:/{gsub(/[^0-9.]/, "", $2); print $2; exit}' "$E2E_DIR/scenarios/low-battery.dev")
OL_STATUS=$(awk -F': ' '/^ups\.status:/{print $2; exit}' "$E2E_DIR/scenarios/online-charging.dev")
LB_CHARGE_INT=${LB_CHARGE%.*}
if [ -z "$LB_CHARGE_INT" ] || [ "$LB_CHARGE_INT" -ge 20 ]; then
  echo "FAIL (setup): low-battery.dev battery.charge=${LB_CHARGE:-?} expected < 20"
  exit 1
fi
if ! echo "$OL_STATUS" | grep -q "OL"; then
  echo "FAIL (setup): online-charging.dev ups.status='${OL_STATUS:-?}' expected to contain OL"
  exit 1
fi

# Start with healthy mains so the daemon enters the loop in OL.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Daemon in background. NO --exit-after-shutdown; we want it to stay
# alive across all three transitions.
PYTHONUNBUFFERED=1 eneru run --config "$REARM_DIR/config.yaml" > "$REARM_DIR/daemon.log" 2>&1 &
DAEMON_PID=$!
# Single-quoted trap so $DAEMON_PID resolves at trap-fire time, not
# trap-set time. Functionally equivalent here (DAEMON_PID is already
# set when this line runs and never changes), but matches shell-best-
# practice and silences ShellCheck.
trap 'kill $DAEMON_PID 2>/dev/null || true' EXIT

# Let the daemon initialise + observe OL.
sleep 3

# (1) First outage: low battery -> trigger fires, flag created, no-op
#     "shutdown" command runs, daemon keeps polling.
echo "  step 1: low-battery -> first trigger"
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply.dev

# Wait for the first EMERGENCY_SHUTDOWN_INITIATED to land in SQLite.
DB=$REARM_DIR/default.db
for i in {1..20}; do
  if [ -f "$DB" ]; then
    COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM events WHERE event_type='EMERGENCY_SHUTDOWN_INITIATED'" 2>/dev/null || echo 0)
    if [ "$COUNT" -ge 1 ]; then
      echo "  first trigger logged after ${i}s"
      break
    fi
  fi
  sleep 1
done
if [ "${COUNT:-0}" -lt 1 ]; then
  echo "FAIL: first EMERGENCY_SHUTDOWN_INITIATED never recorded"
  tail -40 "$REARM_DIR/daemon.log"
  exit 1
fi

# (2) Power restored: daemon must see OL again and clear the flag.
echo "  step 2: online-charging -> POWER_RESTORED clears flag"
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev

# Wait until POWER_RESTORED lands AND the flag file is gone.
for i in {1..20}; do
  PR_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM events WHERE event_type='POWER_RESTORED'" 2>/dev/null || echo 0)
  if [ "$PR_COUNT" -ge 1 ] && [ ! -f "$REARM_DIR/shutdown-flag" ]; then
    echo "  POWER_RESTORED logged + flag cleared after ${i}s"
    break
  fi
  sleep 1
done
if [ "${PR_COUNT:-0}" -lt 1 ]; then
  echo "FAIL: POWER_RESTORED never recorded"
  tail -40 "$REARM_DIR/daemon.log"
  exit 1
fi
if [ -f "$REARM_DIR/shutdown-flag" ]; then
  echo "FAIL: flag still present after POWER_RESTORED -- re-arm broken (bug #4 regression)"
  tail -40 "$REARM_DIR/daemon.log"
  exit 1
fi

# (3) Second outage: trigger MUST fire again. Pre-5.2.2 the flag
#     persisted and this no-op'd silently.
echo "  step 3: low-battery again -> second trigger (re-arm proof)"
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply.dev

for i in {1..20}; do
  COUNT2=$(sqlite3 "$DB" "SELECT COUNT(*) FROM events WHERE event_type='EMERGENCY_SHUTDOWN_INITIATED'" 2>/dev/null || echo 1)
  if [ "$COUNT2" -ge 2 ]; then
    echo "  second trigger logged after ${i}s"
    break
  fi
  sleep 1
done
if [ "${COUNT2:-0}" -lt 2 ]; then
  echo "FAIL: second EMERGENCY_SHUTDOWN_INITIATED never recorded -- re-arm broken (bug #4 regression)"
  echo "events table:"
  sqlite3 "$DB" "SELECT ts, event_type, detail FROM events ORDER BY ts" || true
  tail -60 "$REARM_DIR/daemon.log"
  exit 1
fi

# Stop the daemon cleanly.
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT

# Restore baseline so any downstream tests start fresh.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
echo "PASS: bug #4 re-arm; OB->OL->OB produced 2 EMERGENCY_SHUTDOWN_INITIATED rows"
)

echo ""
echo "=== Group 'single-ups' completed successfully ==="
