#!/usr/bin/env bash
#
# E2E group: single-ups-core
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
# Test 2: Monitor normal state (no shutdown triggered)
# ======================================================================
(
echo ""
echo ">>> Running: Test 2: Monitor normal state (no shutdown triggered)"

echo "=== Test 2: Normal State Monitoring ==="

# Ensure UPS is in online state
apply_scenario online-charging

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
apply_scenario low-battery

# Run Eneru in dry-run mode. With --exit-after-shutdown, eneru should
# exit 0 once the dry-run shutdown sequence completes; anything else
# is a real failure that the previous `|| true` was masking.
set +e
# timeout bounds a trigger regression (fail in minutes, not at the 25-min job limit).
timeout 180s eneru run --config $E2E_DIR/config-e2e-dry-run.yaml --exit-after-shutdown 2>&1 | tee /tmp/test3.log
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
apply_scenario low-battery

# Run Eneru briefly - will trigger shutdown and send SSH command
timeout 180s eneru run --config config-e2e.yaml --exit-after-shutdown 2>&1 | tee /tmp/test4.log || true

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

# Switch to FSD scenario. fsd.dev keeps battery.charge/runtime ABOVE the
# dry-run config's low-battery (20%) and runtime (600s) thresholds, so the
# only thing that can fire is the FSD flag itself. (It used to sit below
# both, so low-battery fired too and "FSD" matched the logged status line
# even with FSD handling removed.)
apply_scenario fsd

# Run Eneru in dry-run mode; a clean dry-run sequence exits 0.
set +e
timeout 180s eneru run --config $E2E_DIR/config-e2e-dry-run.yaml --exit-after-shutdown 2>&1 | tee /tmp/test5.log
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 0 ]; then
  echo "FAIL: eneru exited with code $RC (expected 0)"
  exit 1
fi

# The exact trigger reason (monitor.py FSD branch), not just the token.
if ! grep -qF "Triggering immediate shutdown. Reason: UPS signaled FSD (Forced Shutdown) flag." /tmp/test5.log; then
  echo "FAIL: FSD flag did not trigger the immediate shutdown!"
  cat /tmp/test5.log
  exit 1
fi
if ! grep -q "SHUTDOWN SEQUENCE" /tmp/test5.log; then
  echo "FAIL: FSD trigger did not run the shutdown sequence"
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

# Start with normal state, then switch to brownout (each apply blocks
# until upsd serves the new state).
apply_scenario online-charging
apply_scenario brownout

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
#
# ISS-051: an empty URL used to be an unconditional SKIP that still passed
# the required check — so a silent secret rotation (or the secret never
# being wired) could leave notification delivery untested indefinitely, a
# failure mode e2e.yml itself has recorded. Now the context decides: when
# the workflow can see the secret (non-fork push/PR) it exports
# E2E_EXPECT_NOTIFICATION_SECRET=1, turning an empty URL into a hard FAIL.
# Fork PRs (no secret access) keep the legitimate SKIP.
if [ -z "${E2E_NOTIFICATION_URL:-}" ]; then
  if [ "${E2E_EXPECT_NOTIFICATION_SECRET:-}" = "1" ]; then
    echo "FAIL: E2E_NOTIFICATION_URL is empty but this context expects it"
    echo "  (secret rotated/unset in the upstream repo?). Notification"
    echo "  delivery must be exercised on non-fork runs."
    exit 1
  fi
  echo "SKIP: E2E_NOTIFICATION_URL secret not configured (fork/no-secret context)"
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
apply_scenario online-charging
)

# ======================================================================
# Test 70: Shutdown re-arm on POWER_RESTORED (5.2.2 / bug #4)
# (Numbered 34 until 6.2.0, which collided with stats.sh Test 34.)
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
echo ">>> Running: Test 70: Shutdown re-arm on POWER_RESTORED (bug #4)"
echo "=== Test 70: re-arm after POWER_RESTORED ==="

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
  on_battery_stabilization_delay: 0
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
apply_scenario online-charging

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
apply_scenario low-battery

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
apply_scenario online-charging

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
apply_scenario low-battery

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
apply_scenario online-charging
echo "PASS: bug #4 re-arm; OB->OL->OB produced 2 EMERGENCY_SHUTDOWN_INITIATED rows"
)

# ======================================================================
# Test 39: On-battery stabilization suppresses transient critical readings
# ======================================================================
(
echo ""
echo ">>> Running: Test 39: On-battery stabilization suppresses transient critical readings"

STAB_CFG=/tmp/config-e2e-stabilization.yaml
sed '/on_battery_stabilization_delay/d' "$E2E_DIR/config-e2e-dry-run.yaml" > "$STAB_CFG"

rm -f /tmp/eneru-e2e-shutdown-flag
apply_scenario low-battery

set +e
timeout 10s eneru run --config "$STAB_CFG" --exit-after-shutdown 2>&1 | tee /tmp/test39.log
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 124 ]; then
  echo "FAIL: expected stabilization run to keep monitoring until timeout, got $RC"
  cat /tmp/test39.log
  exit 1
fi
if grep -q "SHUTDOWN SEQUENCE" /tmp/test39.log; then
  echo "FAIL: shutdown fired inside on-battery stabilization window"
  exit 1
fi
if ! grep -q "stabilization" /tmp/test39.log; then
  echo "FAIL: expected stabilization log line"
  exit 1
fi
apply_scenario online-charging
echo "PASS: on-battery stabilization suppressed transient critical readings"
)

# ======================================================================
# Test 40: Remote SSH healthcheck is harmless
# ======================================================================
(
echo ""
echo ">>> Running: Test 40: Remote SSH healthcheck is harmless"

cd $E2E_DIR
docker compose exec -T ssh-target sh -c "rm -f /var/run/shutdown-triggered && touch /var/run/server-alive"
rm -f /tmp/eneru-e2e-state.remote-health.json /tmp/eneru-e2e-shutdown-flag
apply_scenario online-charging

set +e
timeout 8s eneru run --config $E2E_DIR/config-e2e-dry-run.yaml 2>&1 | tee /tmp/test40.log
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 124 ]; then
  echo "FAIL: expected timeout while daemon stayed alive, got $RC"
  exit 1
fi
if ! grep -q "HEALTHY" /tmp/eneru-e2e-state.remote-health.json; then
  echo "FAIL: remote health sidecar did not show HEALTHY"
  cat /tmp/eneru-e2e-state.remote-health.json 2>/dev/null || true
  exit 1
fi
if docker compose exec -T ssh-target test -f /var/run/shutdown-triggered; then
  echo "FAIL: healthcheck created shutdown marker"
  exit 1
fi
echo "PASS: remote SSH healthcheck reached target without shutdown"
)

# ======================================================================
# Test 41: Manual remote dry-run executes no configured commands
# ======================================================================
(
echo ""
echo ">>> Running: Test 41: Manual remote dry-run executes no configured commands"

cd $E2E_DIR
docker compose exec -T ssh-target sh -c "rm -f /var/run/shutdown-triggered && touch /var/run/server-alive"

eneru shutdown remote --config "$E2E_DIR/config-e2e.yaml" \
  --server "E2E SSH Target" --dry-run 2>&1 | tee /tmp/test41.log

if docker compose exec -T ssh-target test -f /var/run/shutdown-triggered; then
  echo "FAIL: dry-run sent configured shutdown command"
  exit 1
fi
if ! grep -q "Dry-run" /tmp/test41.log; then
  echo "FAIL: dry-run output missing"
  exit 1
fi
echo "PASS: manual remote dry-run did not execute configured commands"
)

# ======================================================================
# Test 42: Manual confirmed remote shutdown reaches selected target
# ======================================================================
(
echo ""
echo ">>> Running: Test 42: Manual confirmed remote shutdown reaches selected target"

cd $E2E_DIR
docker compose exec -T ssh-target sh -c "rm -f /var/run/shutdown-triggered && touch /var/run/server-alive"

eneru shutdown remote --config "$E2E_DIR/config-e2e.yaml" \
  --server "E2E SSH Target" \
  --i-really-want-to-proceed-with-remote-shutdown 2>&1 | tee /tmp/test42.log

if ! docker compose exec -T ssh-target test -f /var/run/shutdown-triggered; then
  echo "FAIL: confirmed manual remote shutdown did not reach target"
  exit 1
fi
echo "PASS: manual confirmed remote shutdown reached selected target"
)

# ======================================================================
# Test 43: Embedded API health/readiness/metrics/index
# ======================================================================
(
echo ""
echo ">>> Running: Test 43: Embedded API health/readiness/metrics/index"

apply_scenario online-charging
timeout 60s eneru run --config $E2E_DIR/config-e2e-dry-run.yaml \
  > /tmp/test43-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT

# Each startup endpoint gets a 10 s poll budget (0.5 s x 20 attempts), so
# their serial retries can take 30 s. The later progress poll adds 15 s;
# the 60 s outer timeout covers that 45 s retry budget plus test work.
poll_endpoint() {
  local url="$1" out="$2" tries="${3:-20}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$url" >"$out" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if ! poll_endpoint http://127.0.0.1:9100/ready /tmp/test43-ready.json; then
  echo "FAIL: /ready never responded within poll budget"
  cat /tmp/test43-daemon.log
  exit 1
fi
if ! poll_endpoint http://127.0.0.1:9100/metrics /tmp/test43-metrics.txt; then
  echo "FAIL: /metrics never responded within poll budget"
  cat /tmp/test43-daemon.log
  exit 1
fi
if ! poll_endpoint http://127.0.0.1:9100/api/v1 /tmp/test43-index.json; then
  echo "FAIL: /api/v1 never responded within poll budget"
  cat /tmp/test43-daemon.log
  exit 1
fi

MISSING_UPS_STATUS=$(curl -sS \
  -o /tmp/test43-missing-ups.json \
  -w "%{http_code}" \
  http://127.0.0.1:9100/api/v1/ups/missing)
if [ "$MISSING_UPS_STATUS" != "404" ]; then
  echo "FAIL: missing UPS endpoint returned HTTP $MISSING_UPS_STATUS, expected 404"
  cat /tmp/test43-missing-ups.json
  exit 1
fi

if ! grep -q "eneru_up 1" /tmp/test43-metrics.txt; then
  echo "FAIL: metrics endpoint missing eneru_up"
  cat /tmp/test43-metrics.txt
  exit 1
fi
if ! grep -q "eneru_ups_input_voltage" /tmp/test43-metrics.txt; then
  echo "FAIL: metrics endpoint missing power-quality voltage metric"
  cat /tmp/test43-metrics.txt
  exit 1
fi
if ! grep -q "eneru_ups_voltage_state" /tmp/test43-metrics.txt; then
  echo "FAIL: metrics endpoint missing grid-quality state metric"
  cat /tmp/test43-metrics.txt
  exit 1
fi
if ! grep -q '"/api/v1/events"' /tmp/test43-index.json; then
  echo "FAIL: API index missing /api/v1/events"
  cat /tmp/test43-index.json
  exit 1
fi
if ! grep -q '"availableEndpoints"' /tmp/test43-missing-ups.json; then
  echo "FAIL: API 404 missing availableEndpoints"
  cat /tmp/test43-missing-ups.json
  exit 1
fi

# Exercise the per-UPS shutdown plan/progress path against a real NUT event.
UPS_PROGRESS_URL='http://127.0.0.1:9100/api/v1/ups/TestUPS%40localhost%3A3493/shutdown-progress'
UPS_PLAN_URL='http://127.0.0.1:9100/api/v1/ups/TestUPS%40localhost%3A3493/shutdown-plan'
if ! curl -fsS "$UPS_PLAN_URL" > /tmp/test43-plan.json; then
  echo "FAIL: per-UPS shutdown plan endpoint did not respond"
  exit 1
fi

# 6.2 UX contract: next-trigger outlook, role, freshness and status
# vocabulary on the live API, plus the state-file EPOCH the TUI reads.
if ! curl -fsS http://127.0.0.1:9100/api/v1/ups > /tmp/test43-ups.json || \
   ! curl -fsS 'http://127.0.0.1:9100/api/v1/ups/TestUPS%40localhost%3A3493' \
     > /tmp/test43-ups-one.json; then
  echo "FAIL: /api/v1/ups did not respond"
  exit 1
fi
if ! python3 - /tmp/test43-ups.json /tmp/test43-ups-one.json \
     /tmp/test43-plan.json /tmp/eneru-e2e-state <<'PY'
import json
import sys
import time

fleet = json.load(open(sys.argv[1], encoding="utf-8"))
one = json.load(open(sys.argv[2], encoding="utf-8"))
plan = json.load(open(sys.argv[3], encoding="utf-8"))
state = dict(line.split("=", 1)
             for line in open(sys.argv[4], encoding="utf-8").read().splitlines())
assert isinstance(fleet["generatedAt"], float)
assert isinstance(one["generatedAt"], float)
row = fleet["ups"][0]
assert "nextTrigger" in row and row["nextTrigger"] is None, row["nextTrigger"]
ids = [t["id"] for t in row["triggerOutlook"]["triggers"]]
assert ids == ["fsd", "failsafe", "lowBattery", "criticalRuntime",
               "depletionRate", "extendedTime", "selfTestFailure"], ids
assert row["triggerOutlook"]["onBattery"] is False
# Containers + unmounts + one remote, local_shutdown off: drains this host
# and shuts the remote down, but the host itself stays up.
role = row["role"]
assert role["kind"] == "local", role
assert role["shutsDownLocalHost"] is False and role["localDrain"] is True, role
assert role["remoteServers"] == 1 and role["redundancyGroups"] == [], role
action = row["triggerOutlook"]["action"]
assert action["kind"] == "local-shutdown", action
assert action["label"] == ("Stops local workloads (this host stays up) and shuts "
                           "down 1 remote server (dry-run)"), action
assert row["freshness"]["stale"] is False
assert abs(row["freshness"]["lastPollAt"] - time.time()) < 60
assert row["statusSummary"]["state"] == "online", row["statusSummary"]
assert row["statusSummary"]["severity"] == "ok"
assert plan["triggers"]["conditions"][0].startswith("charge below ")
assert plan["role"]["kind"] == row["role"]["kind"]
assert abs(float(state["EPOCH"]) - time.time()) < 60
assert state["TIMESTAMP_ISO"][-6] in "+-"
print("PASS (43a): next-trigger/role/freshness contract on the live API")
PY
then
  echo "FAIL: /api/v1/ups is missing the 6.2 outlook contract"
  cat /tmp/test43-ups.json /tmp/eneru-e2e-state 2>/dev/null || true
  exit 1
fi
# apply_scenario blocks until upsd serves the new state (no reload race).
apply_scenario low-battery
UPS_PROGRESS_SEEN=false
for _ in $(seq 1 30); do
  if curl -fsS "$UPS_PROGRESS_URL" > /tmp/test43-progress.json 2>/dev/null && \
     python3 - /tmp/test43-plan.json /tmp/test43-progress.json <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    plan = json.load(handle)["plan"]
with open(sys.argv[2], encoding="utf-8") as handle:
    progress = json.load(handle)["progress"]
assert [phase["id"] for phase in plan["phases"]] == [
    "vms", "containers", "filesystem-sync", "filesystem-unmount",
    "remote", "final-sync", "local-poweroff",
]
assert progress["runId"] > 0
assert progress["state"] == "succeeded"
assert progress["finishedAt"] is not None
phases = {phase["id"]: phase for phase in progress["phases"]}
assert phases["vms"]["state"] == "skipped"
assert phases["vms"]["detail"] == "disabled"
assert phases["remote"]["state"] == "succeeded"
assert progress["remotes"]
assert all(remote["state"] == "succeeded" for remote in progress["remotes"])
# The TUI reads the same progress from the sidecar next to the state file.
with open("/tmp/eneru-e2e-state.shutdown-progress.json", encoding="utf-8") as handle:
    sidecar = json.load(handle)
assert sidecar["runId"] == progress["runId"]
assert sidecar["state"] == "succeeded" and sidecar["writtenAt"] > 0
PY
  then
    UPS_PROGRESS_SEEN=true
    break
  fi
  sleep 0.5
done
if [ "$UPS_PROGRESS_SEEN" != true ]; then
  echo "FAIL: per-UPS terminal shutdown progress was not published"
  cat /tmp/test43-progress.json /tmp/test43-daemon.log 2>/dev/null || true
  # Don't leak the low-battery state into the next test in this group.
  kill "$DAEMON_PID" 2>/dev/null || true
  trap - EXIT
  apply_scenario online-charging
  exit 1
fi
# F-188: the trigger the dashboard names as "next" must be the one the daemon
# actually fired. The shutdown has run (progress succeeded) and the dry-run
# daemon keeps polling, so the row still shows the low-battery reading.
if ! curl -fsS http://127.0.0.1:9100/api/v1/ups > /tmp/test43-ups-lb.json || \
   ! python3 - /tmp/test43-ups-lb.json /tmp/test43-daemon.log <<'PY'
import json
import re
import sys

row = json.load(open(sys.argv[1], encoding="utf-8"))["ups"][0]
log = open(sys.argv[2], encoding="utf-8", errors="replace").read()
# monitor.py _trigger_immediate_shutdown / _handle_on_battery (T1).
fired = re.findall(r"Triggering immediate shutdown\. Reason: (.+)", log)
assert fired, "daemon never logged a trigger"
assert fired[0] == "Battery charge 14% below threshold 20%", fired
prefix = {"lowBattery": "Battery charge ", "criticalRuntime": "Runtime ",
          "depletionRate": "Depletion rate ", "extendedTime": "Time on battery "}
nxt = row["nextTrigger"]
assert nxt is not None, row["triggerOutlook"]
assert nxt["id"] == "lowBattery" and nxt["state"] == "fired", nxt
assert fired[0].startswith(prefix[nxt["id"]]), (nxt, fired)
outlook = row["triggerOutlook"]
assert outlook["onBattery"] is True, outlook
assert outlook["firing"][:1] == ["lowBattery"], outlook["firing"]
assert outlook["summary"].startswith("Shutdown condition met: low battery"), outlook
assert row["role"]["kind"] == "local", row["role"]
print("PASS (43a): nextTrigger names the trigger the daemon fired: " + fired[0])
PY
then
  echo "FAIL: nextTrigger does not match the trigger the daemon fired"
  cat /tmp/test43-ups-lb.json /tmp/test43-daemon.log 2>/dev/null || true
  kill "$DAEMON_PID" 2>/dev/null || true
  trap - EXIT
  apply_scenario online-charging
  exit 1
fi
apply_scenario online-charging

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "PASS: embedded API and per-UPS shutdown observability responded"
)

# ======================================================================
# Test 46: OCI image runs with API enabled by CLI flags
# ======================================================================
(
echo ""
echo ">>> Running: Test 46: OCI image runs with API enabled by CLI flags"

cat >/tmp/test46-container-config.yaml <<'YAML'
ups:
  - name: "TestUPS@nut-server"
    display_name: "Container E2E UPS"
    is_local: false
local_shutdown:
  enabled: false
  trigger_on: none
logging:
  file: null
  state_file: "/var/run/eneru/ups-monitor.state"
  battery_history_file: "/var/run/eneru/ups-battery-history"
  shutdown_flag_file: "/var/run/eneru/ups-shutdown-scheduled"
statistics:
  db_directory: "/var/lib/eneru"
YAML

docker build -t eneru:e2e .
NETWORK=$(docker network ls --format '{{.Name}}' | grep '_eneru-e2e$' | head -1)
if [ -z "$NETWORK" ]; then
  echo "FAIL: E2E Docker network not found"
  docker network ls
  exit 1
fi

docker rm -f eneru-e2e-under-test >/dev/null 2>&1 || true
docker run -d --name eneru-e2e-under-test \
  --network "$NETWORK" \
  -p 127.0.0.1:19191:9191 \
  -v /tmp/test46-container-config.yaml:/etc/ups-monitor/config.yaml:ro \
  eneru:e2e \
  run --config /etc/ups-monitor/config.yaml \
  --api --api-bind 0.0.0.0 --api-port 9191

cleanup_container() {
  docker logs eneru-e2e-under-test >/tmp/test46-daemon.log 2>&1 || true
  docker rm -f eneru-e2e-under-test >/dev/null 2>&1 || true
}
trap cleanup_container EXIT

poll_endpoint() {
  local url="$1" out="$2" tries="${3:-20}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$url" >"$out" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if ! poll_endpoint http://127.0.0.1:19191/health /tmp/test46-health.json 60; then
  echo "FAIL: container /health never responded"
  docker logs eneru-e2e-under-test || true
  exit 1
fi
if ! poll_endpoint http://127.0.0.1:19191/ready /tmp/test46-ready.json 60; then
  echo "FAIL: container /ready never became ready"
  docker logs eneru-e2e-under-test || true
  exit 1
fi
if ! grep -Eq '"ready"[[:space:]]*:[[:space:]]*true' /tmp/test46-ready.json; then
  echo "FAIL: container /ready did not report ready=true"
  cat /tmp/test46-ready.json
  exit 1
fi

# The dashboard is served by the embedded API (from the eneru.web package).
DASH_HTML="$(curl -fsS http://127.0.0.1:19191/ 2>/dev/null || true)"
case "$DASH_HTML" in
  *"<title>Eneru</title>"*) echo "PASS (46b): dashboard served from OCI image" ;;
  *) echo "FAIL: dashboard not served by OCI image"; docker logs eneru-e2e-under-test || true; exit 1 ;;
esac
curl -fsS http://127.0.0.1:19191/app.js >/dev/null 2>&1 \
  || { echo "FAIL: dashboard app.js not served by OCI image"; exit 1; }
# ISS-011: favicon.svg is a packaged wheel asset (the OCI image installs from the
# wheelhouse). Exercise it where the wheel is actually installed so a missing
# *.svg package-data glob is caught here, not just by unit tests.
curl -fsS http://127.0.0.1:19191/favicon.svg >/dev/null 2>&1 \
  || { echo "FAIL: favicon.svg not served by OCI image (wheel package-data drift?)"; exit 1; }

cleanup_container
trap - EXIT
echo "PASS: OCI image responded with CLI-enabled API + dashboard"
)

# ======================================================================
# Test 44: Unreachable remote shutdown is bounded
# ======================================================================
(
echo ""
echo ">>> Running: Test 44: Unreachable remote shutdown is bounded"

cat >/tmp/config-e2e-unreachable-remote.yaml <<'YAML'
ups:
  name: TestUPS@localhost:3493
  display_name: "E2E Unreachable Remote"
  check_interval: 1
triggers:
  on_battery_stabilization_delay: 0
  low_battery_threshold: 95
  critical_runtime_threshold: 600
behavior:
  dry_run: false
local_shutdown:
  enabled: false
remote_servers:
  - name: unreachable
    enabled: true
    host: 203.0.113.1
    user: root
    connect_timeout: 1
    command_timeout: 1
    shutdown_safety_margin: 1
    shutdown_command: "sudo shutdown -h now"
    ssh_options:
      - "StrictHostKeyChecking=no"
      - "UserKnownHostsFile=/dev/null"
remote_health:
  enabled: false
statistics:
  db_directory: /tmp/eneru-e2e-stats
logging:
  file: null
  state_file: /tmp/eneru-e2e-state
  shutdown_flag_file: /tmp/eneru-e2e-shutdown-flag
YAML

apply_scenario low-battery
# Nanosecond-precision wallclock so the upper-bound assertion below
# isn't hostage to whole-second rounding (a 10.999 s real run would
# round down to 10 with `date +%s` and silently pass).
START_NS=$(date +%s%N)
set +e
timeout 12s eneru run --config /tmp/config-e2e-unreachable-remote.yaml \
  --exit-after-shutdown 2>&1 | tee /tmp/test44.log
RC=${PIPESTATUS[0]}
set -e
ELAPSED_NS=$(( $(date +%s%N) - START_NS ))
ELAPSED_MS=$(( ELAPSED_NS / 1000000 ))
apply_scenario online-charging

if [ "$RC" -eq 124 ]; then
  echo "FAIL: unreachable remote stalled shutdown past outer timeout"
  cat /tmp/test44.log
  exit 1
fi
# A crash (traceback, bad config) must not pass as "bounded": the completed
# sequence exits 0 via --exit-after-shutdown even though the remote failed.
if [ "$RC" -ne 0 ]; then
  echo "FAIL: eneru exited with code $RC (expected 0 after the bounded sequence)"
  cat /tmp/test44.log
  exit 1
fi
if ! grep -q "SHUTDOWN SEQUENCE" /tmp/test44.log; then
  echo "FAIL: low battery did not start the shutdown sequence"
  cat /tmp/test44.log
  exit 1
fi
# 12 s outer timeout enforced via `timeout`; assert the daemon itself
# returned in well under that so a near-boundary edge case still fails
# loudly. 11 s gives a 1 s safety margin on shared CI runners.
if [ "$ELAPSED_MS" -ge 11000 ]; then
  echo "FAIL: unreachable remote took too long (${ELAPSED_MS} ms)"
  exit 1
fi
# Exact summary format from RemoteShutdownMixin (shutdown/remote.py).
if ! grep -q "Remote shutdown complete (0/1 succeeded" /tmp/test44.log; then
  echo "FAIL: remote shutdown summary did not report bounded failure"
  cat /tmp/test44.log
  exit 1
fi
echo "PASS: unreachable remote shutdown completed within bounded timeout"
)

# ======================================================================
# Test 45: MQTT status publishes power-quality metrics
# ======================================================================
(
echo ""
echo ">>> Running: Test 45: MQTT status publishes power-quality metrics"

cat >/tmp/config-e2e-mqtt.yaml <<'YAML'
ups:
  name: TestUPS@localhost:3493
  display_name: "E2E MQTT UPS"
  check_interval: 1
triggers:
  on_battery_stabilization_delay: 0
behavior:
  dry_run: true
local_shutdown:
  enabled: false
remote_health:
  enabled: false
statistics:
  db_directory: /tmp/eneru-e2e-stats
logging:
  file: null
  state_file: /tmp/eneru-e2e-state
  shutdown_flag_file: /tmp/eneru-e2e-shutdown-flag
mqtt:
  enabled: true
  broker: mqtt://127.0.0.1:1883
  topic_prefix: eneru-e2e
  publish_interval: 1
YAML

rm -f /tmp/test45-mqtt.json /tmp/test45-subscriber.log
apply_scenario online-charging
python3 - <<'PY' > /tmp/test45-subscriber.log 2>&1 &
import json
import os
import sys
import time

import paho.mqtt.client as mqtt

OUT = "/tmp/test45-mqtt.json"
TOPIC = "eneru-e2e/status"


def new_client():
    try:
        return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        return mqtt.Client()


def on_message(client, userdata, msg):
    payload = json.loads(msg.payload.decode("utf-8"))
    ups = payload.get("ups") or []
    power = ups[0].get("powerQuality", {}) if ups else {}
    # The MQTT publisher sends a status snapshot immediately after it
    # connects. During daemon startup that first frame can legitimately
    # contain the powerQuality object before the first UPS poll has
    # populated readings, so wait for the observed telemetry frame.
    if (
        power.get("inputVoltage") not in ("", None)
        and "voltageState" in power
        and "nominalVoltage" in power
    ):
        with open(OUT, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        client.disconnect()


client = new_client()
client.on_message = on_message
client.connect("127.0.0.1", 1883, 30)
client.subscribe(TOPIC)
client.loop_start()
deadline = time.time() + 25
while time.time() < deadline and not os.path.exists(OUT):
    time.sleep(0.2)
client.loop_stop()
try:
    client.disconnect()
except Exception:
    pass
sys.exit(0 if os.path.exists(OUT) else 1)
PY
SUB_PID=$!

timeout 20s eneru run --config /tmp/config-e2e-mqtt.yaml \
  > /tmp/test45-daemon.log 2>&1 &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" "$SUB_PID" 2>/dev/null || true' EXIT

if ! wait "$SUB_PID"; then
  echo "FAIL: MQTT subscriber did not receive Eneru status payload"
  cat /tmp/test45-subscriber.log
  cat /tmp/test45-daemon.log
  exit 1
fi

kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT

python3 - <<'PY'
import json

with open("/tmp/test45-mqtt.json", encoding="utf-8") as handle:
    payload = json.load(handle)
power = payload["ups"][0]["powerQuality"]
assert power["inputVoltage"] != ""
assert "voltageState" in power
assert "nominalVoltage" in power
PY

echo "PASS: MQTT status payload arrived with power-quality metrics"
)

# ======================================================================
# Test 59: Remote PATH augmentation always resolves bare commands
# ======================================================================
(
echo ""
echo ">>> Running: Test 59: Remote PATH augmentation always resolves bare commands"

config=/tmp/config-e2e-path.yaml
docker compose -f "$E2E_DIR/docker-compose.yml" exec -T ssh-target \
  rm -f /tmp/eneru-path-augmented
cat >"$config" <<'YAML'
ups:
  name: "TestUPS@localhost:3493"
  check_interval: 1
triggers:
  on_battery_stabilization_delay: 0
  low_battery_threshold: 95
  critical_runtime_threshold: 600
behavior:
  dry_run: false
logging:
  file: null
  state_file: "/tmp/eneru-e2e-path-state"
  battery_history_file: "/tmp/eneru-e2e-path-history"
  shutdown_flag_file: "/tmp/eneru-e2e-path-flag"
statistics:
  db_directory: "/tmp/eneru-e2e-path-stats"
remote_servers:
  - name: "always-augmented-path-target"
    enabled: true
    host: "localhost"
    user: "testuser"
    shutdown_command: "eneru-path-probe"
    ssh_options:
      - "-o Port=2222"
      - "-o StrictHostKeyChecking=no"
      - "-o UserKnownHostsFile=/dev/null"
      - "-o IdentityFile=/tmp/e2e-ssh-key"
local_shutdown:
  enabled: false
YAML

apply_scenario low-battery
timeout 180s eneru run --config "$config" --exit-after-shutdown \
  2>&1 | tee /tmp/test59.log

if ! docker compose -f "$E2E_DIR/docker-compose.yml" exec -T ssh-target \
    test -f /tmp/eneru-path-augmented; then
  echo "FAIL: unconditional PATH augmentation did not resolve the Synology probe"
  cat /tmp/test59.log
  exit 1
fi
echo "PASS: unconditional PATH augmentation resolved the Synology probe"
)

# ======================================================================
# Test 60: Local Compose shutdown honors the configured timeout form
# ======================================================================
(
echo ""
echo ">>> Running: Test 60: Local Compose shutdown honors the configured timeout form"

compose_file=/tmp/eneru-e2e-compose-timeout.yaml
config=/tmp/config-e2e-compose-timeout.yaml
cat >"$compose_file" <<'YAML'
services:
  sleeper:
    image: alpine:3.20
    container_name: eneru-e2e-compose-timeout
    command: ["sleep", "infinity"]
YAML
trap 'docker compose -f "$compose_file" down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
docker compose -f "$compose_file" up -d

cat >"$config" <<YAML
ups:
  name: "TestUPS@localhost:3493"
  check_interval: 1
triggers:
  on_battery_stabilization_delay: 0
  low_battery_threshold: 95
  critical_runtime_threshold: 600
behavior:
  dry_run: false
logging:
  file: null
  state_file: "/tmp/eneru-e2e-compose-state"
  battery_history_file: "/tmp/eneru-e2e-compose-history"
  shutdown_flag_file: "/tmp/eneru-e2e-compose-flag"
statistics:
  db_directory: "/tmp/eneru-e2e-compose-stats"
containers:
  enabled: true
  runtime: docker
  stop_timeout: 3
  shutdown_all_remaining_containers: false
  compose_files:
    - path: "$compose_file"
      stop_timeout: 2
filesystems:
  sync_enabled: false
local_shutdown:
  enabled: false
YAML

apply_scenario low-battery
timeout 180s eneru run --config "$config" --exit-after-shutdown \
  2>&1 | tee /tmp/test60.log

if docker inspect eneru-e2e-compose-timeout >/dev/null 2>&1; then
  echo "FAIL: compose stack remained after Eneru's down -t shutdown phase"
  cat /tmp/test60.log
  exit 1
fi
# The per-file stop_timeout (2) must win over the containers default (3);
# containers.py logs the effective value it passes to `down -t`.
if ! grep -qF "Stopping: $compose_file (timeout: 2s)" /tmp/test60.log; then
  echo "FAIL: compose down did not use the per-file stop_timeout (2s)"
  cat /tmp/test60.log
  exit 1
fi
trap - EXIT
echo "PASS: Eneru removed the Compose stack with the configured timeout"
)

# ======================================================================
# Test 71: One-entry ups: list honours an explicit is_local: false (6.2)
# ======================================================================
# Since 5.0 a lone list-form UPS powered this host off on every trigger,
# even with `is_local: false`. 6.2: an explicit false keeps the host up
# (its remote servers still shut down); an omitted is_local keeps the old
# poweroff and logs a warning at startup.
(
echo ""
echo ">>> Running: Test 71: One-entry ups list honours explicit is_local: false"

write_is_local_config() {
  # $1 = output path, $2 = "is_local: false" line or "" (omitted)
  local out="$1" is_local_line="$2"
  cat >"$out" <<YAML
ups:
  - name: "TestUPS@localhost:3493"
    check_interval: 1
    ${is_local_line}
    remote_servers:
      - name: "E2E SSH Target"
        enabled: true
        host: "localhost"
        user: "testuser"
        connect_timeout: 5
        command_timeout: 10
        shutdown_command: "sudo shutdown -h now"
        ssh_options:
          - "-o Port=2222"
          - "-o StrictHostKeyChecking=no"
          - "-o UserKnownHostsFile=/dev/null"
          - "-o IdentityFile=/tmp/e2e-ssh-key"
triggers:
  on_battery_stabilization_delay: 0
  low_battery_threshold: 20
  critical_runtime_threshold: 600
behavior:
  dry_run: true
logging:
  file: null
  state_file: "/tmp/eneru-e2e-islocal-state"
  battery_history_file: "/tmp/eneru-e2e-islocal-history"
  shutdown_flag_file: "/tmp/eneru-e2e-islocal-flag"
statistics:
  db_directory: "/tmp/eneru-e2e-islocal-stats"
notifications:
  enabled: false
local_shutdown:
  enabled: true
  command: "shutdown -h now"
YAML
}

apply_scenario low-battery

# --- 71a: explicit is_local: false -> remote shutdown only, host stays up.
write_is_local_config /tmp/config-e2e-islocal-false.yaml "is_local: false"
rm -f /tmp/eneru-e2e-islocal-flag
set +e
timeout 180s eneru run --config /tmp/config-e2e-islocal-false.yaml \
  --exit-after-shutdown 2>&1 | tee /tmp/test71a.log
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 0 ]; then
  echo "FAIL (71a): eneru exited with code $RC (expected 0)"
  cat /tmp/test71a.log
  exit 1
fi
if ! grep -qF "Would send command 'sudo shutdown -h now' to testuser@localhost" /tmp/test71a.log; then
  echo "FAIL (71a): the remote server shutdown did not run"
  cat /tmp/test71a.log
  exit 1
fi
if grep -qF "Would execute: shutdown -h now" /tmp/test71a.log; then
  echo "FAIL (71a): explicit is_local: false still powered this host off"
  cat /tmp/test71a.log
  exit 1
fi
if ! grep -qF "SHUTDOWN SEQUENCE COMPLETE (is_local: false -- this host stays up)" /tmp/test71a.log; then
  echo "FAIL (71a): missing the host-stays-up completion line"
  cat /tmp/test71a.log
  exit 1
fi
echo "PASS (71a): explicit is_local: false shut down the remote only"

# --- 71b: is_local omitted -> unchanged host poweroff, plus a warning.
write_is_local_config /tmp/config-e2e-islocal-omitted.yaml ""
rm -f /tmp/eneru-e2e-islocal-flag
set +e
timeout 180s eneru run --config /tmp/config-e2e-islocal-omitted.yaml \
  --exit-after-shutdown 2>&1 | tee /tmp/test71b.log
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 0 ]; then
  echo "FAIL (71b): eneru exited with code $RC (expected 0)"
  cat /tmp/test71b.log
  exit 1
fi
if ! grep -qF "WARNING: ups[0] has no is_local; as the only UPS it powers off this host on a shutdown trigger." /tmp/test71b.log; then
  echo "FAIL (71b): the omitted-is_local startup warning is missing"
  cat /tmp/test71b.log
  exit 1
fi
if ! grep -qF "Would execute: shutdown -h now" /tmp/test71b.log; then
  echo "FAIL (71b): omitted is_local no longer powers this host off"
  cat /tmp/test71b.log
  exit 1
fi
if ! grep -qF "Would send command 'sudo shutdown -h now' to testuser@localhost" /tmp/test71b.log; then
  echo "FAIL (71b): the remote server shutdown did not run"
  cat /tmp/test71b.log
  exit 1
fi
# `eneru validate` prints the same warning.
eneru validate --config /tmp/config-e2e-islocal-omitted.yaml >/tmp/test71b-validate.log 2>&1 || true
if ! grep -qF "ups[0] has no is_local" /tmp/test71b-validate.log; then
  echo "FAIL (71b): eneru validate did not warn about the omitted is_local"
  cat /tmp/test71b-validate.log
  exit 1
fi
echo "PASS (71b): omitted is_local still powers this host off and warns"
)

echo ""
echo "=== Group 'single-ups-core' completed successfully ==="
