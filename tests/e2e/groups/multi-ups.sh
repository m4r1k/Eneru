#!/usr/bin/env bash
#
# E2E group: multi-ups
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
# Test 9: Multi-UPS isolation (UPS1 fails, UPS2 normal)
# ======================================================================
(
echo ""
echo ">>> Running: Test 9: Multi-UPS isolation (UPS1 fails, UPS2 normal)"

echo "=== Test 9: Multi-UPS Isolation ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag*

# Ensure both UPSes start online
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# Now fail UPS1 (low battery) while UPS2 stays online
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
sleep 3

# Verify UPS1 is on battery / low
UPS1_STATUS=$(upsc UPS1@localhost:3493 ups.status 2>/dev/null)
UPS2_STATUS=$(upsc UPS2@localhost:3493 ups.status 2>/dev/null)

echo "UPS1 status: $UPS1_STATUS"
echo "UPS2 status: $UPS2_STATUS"

if echo "$UPS1_STATUS" | grep -q "OB"; then
  echo "PASS: UPS1 correctly shows on battery"
else
  echo "FAIL: UPS1 should be on battery, got: $UPS1_STATUS"
  exit 1
fi

if echo "$UPS2_STATUS" | grep -q "OL"; then
  echo "PASS: UPS2 correctly shows online (unaffected)"
else
  echo "FAIL: UPS2 should still be online, got: $UPS2_STATUS"
  exit 1
fi

# Run multi-UPS Eneru briefly -- UPS1 should trigger shutdown for its group
eneru run --config $E2E_DIR/config-e2e-multi-ups.yaml --exit-after-shutdown 2>&1 | tee /tmp/test9.log || true

# Verify UPS1 triggered shutdown
if grep -q "SHUTDOWN SEQUENCE\|SHUTDOWN INITIATED\|Triggering immediate shutdown" /tmp/test9.log; then
  echo "PASS: UPS1 low battery triggered shutdown"
else
  echo "FAIL: No shutdown triggered for UPS1"
  cat /tmp/test9.log
  exit 1
fi

# Verify the log shows UPS1 context (prefixed with display name or UPS name)
if grep -q "E2E UPS1\|UPS1@localhost" /tmp/test9.log; then
  echo "PASS: Shutdown log correctly identifies UPS1"
else
  echo "Note: UPS identification in logs not verified"
fi

echo "PASS: Multi-UPS isolation working correctly"
)

# ======================================================================
# Test 10: Multi-UPS both online (no false triggers)
# ======================================================================
(
echo ""
echo ">>> Running: Test 10: Multi-UPS both online (no false triggers)"

echo "=== Test 10: Multi-UPS Normal Operation ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag*

# Both UPSes online
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# Run briefly -- should NOT trigger any shutdown
timeout 5 eneru run --config $E2E_DIR/config-e2e-multi-ups.yaml 2>&1 | tee /tmp/test10.log || true

if grep -q "SHUTDOWN SEQUENCE\|SHUTDOWN INITIATED" /tmp/test10.log; then
  echo "FAIL: Shutdown triggered during normal multi-UPS operation!"
  exit 1
fi

echo "PASS: No false shutdown triggers with both UPSes online"
)

# ======================================================================
# Test 14: Multi-UPS concurrent failure (both UPSes fail)
# ======================================================================
(
echo ""
echo ">>> Running: Test 14: Multi-UPS concurrent failure (both UPSes fail)"

echo "=== Test 14: Concurrent UPS Failure ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag*

# Both UPSes go low-battery simultaneously
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# Run multi-UPS Eneru -- both groups should trigger shutdown
eneru run --config $E2E_DIR/config-e2e-multi-ups.yaml --exit-after-shutdown 2>&1 | tee /tmp/test14.log || true

# Verify shutdown was triggered
if ! grep -q "SHUTDOWN SEQUENCE\|SHUTDOWN INITIATED\|Triggering immediate shutdown" /tmp/test14.log; then
  echo "FAIL: No shutdown triggered during concurrent failure"
  cat /tmp/test14.log
  exit 1
fi

# Verify both UPSes are referenced in the log
if grep -q "E2E UPS1\|UPS1@localhost" /tmp/test14.log; then
  echo "PASS: UPS1 shutdown logged"
else
  echo "Note: UPS1 identification not verified in logs"
fi

if grep -q "E2E UPS2\|UPS2@localhost" /tmp/test14.log; then
  echo "PASS: UPS2 shutdown logged"
else
  echo "Note: UPS2 identification not verified in logs"
fi

echo "PASS: Concurrent failure handled correctly"
)

# ======================================================================
# Test 15: Non-local failure (UPS2 fails, UPS1 unaffected)
# ======================================================================
(
echo ""
echo ">>> Running: Test 15: Non-local failure (UPS2 fails, UPS1 unaffected)"

echo "=== Test 15: Non-Local UPS Failure ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag*

# UPS1 stays online, UPS2 goes low-battery
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# Verify UPS states
UPS1_STATUS=$(upsc UPS1@localhost:3493 ups.status 2>/dev/null)
UPS2_STATUS=$(upsc UPS2@localhost:3493 ups.status 2>/dev/null)
echo "UPS1 status: $UPS1_STATUS (should be OL)"
echo "UPS2 status: $UPS2_STATUS (should be OB)"

# Run multi-UPS Eneru -- UPS2 (non-local) should trigger, UPS1 unaffected
eneru run --config $E2E_DIR/config-e2e-multi-ups.yaml --exit-after-shutdown 2>&1 | tee /tmp/test15.log || true

# Verify UPS2 triggered shutdown
if ! grep -q "SHUTDOWN SEQUENCE\|SHUTDOWN INITIATED\|Triggering immediate shutdown" /tmp/test15.log; then
  echo "FAIL: No shutdown triggered for UPS2"
  cat /tmp/test15.log
  exit 1
fi

# Verify UPS2 is identified in shutdown context
if grep -q "E2E UPS2\|UPS2@localhost" /tmp/test15.log; then
  echo "PASS: UPS2 correctly triggered shutdown"
else
  echo "Note: UPS2 identification not verified in logs"
fi

echo "PASS: Non-local failure correctly handled"
)

# ======================================================================
# Test 16: Local drain (drain_on_local_shutdown=true)
# ======================================================================
(
echo ""
echo ">>> Running: Test 16: Local drain (drain_on_local_shutdown=true)"

echo "=== Test 16: Local Drain ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag*

# UPS1 (is_local) goes low-battery, UPS2 stays online
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# Run with drain config
eneru run --config $E2E_DIR/config-e2e-multi-ups-drain.yaml --exit-after-shutdown 2>&1 | tee /tmp/test16.log || true

# Verify shutdown was triggered
if ! grep -q "SHUTDOWN SEQUENCE\|SHUTDOWN INITIATED\|Triggering immediate shutdown" /tmp/test16.log; then
  echo "FAIL: No shutdown triggered"
  cat /tmp/test16.log
  exit 1
fi

# Verify drain message appears
if grep -qi "drain" /tmp/test16.log; then
  echo "PASS: Drain message logged"
else
  echo "FAIL: Drain message not found in logs"
  cat /tmp/test16.log
  exit 1
fi

echo "PASS: Local drain correctly executed"
)

# ======================================================================
# Test 17: Local no-drain (drain_on_local_shutdown=false)
# ======================================================================
(
echo ""
echo ">>> Running: Test 17: Local no-drain (drain_on_local_shutdown=false)"

echo "=== Test 17: Local No-Drain ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag*

# UPS1 (is_local) goes low-battery, UPS2 stays online
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

# Run with default multi-UPS config (drain=false)
eneru run --config $E2E_DIR/config-e2e-multi-ups.yaml --exit-after-shutdown 2>&1 | tee /tmp/test17.log || true

# Verify UPS1 shutdown triggered
if ! grep -q "SHUTDOWN SEQUENCE\|SHUTDOWN INITIATED\|Triggering immediate shutdown" /tmp/test17.log; then
  echo "FAIL: No shutdown triggered for UPS1"
  cat /tmp/test17.log
  exit 1
fi

# Verify NO drain message
if grep -qi "drain" /tmp/test17.log; then
  echo "FAIL: Drain should NOT occur with drain_on_local_shutdown=false"
  exit 1
fi

echo "PASS: No-drain correctly skipped drain step"
)

# ======================================================================
# Test 18: Recovery - power restored before shutdown
# ======================================================================
(
echo ""
echo ">>> Running: Test 18: Recovery - power restored before shutdown"

echo "=== Test 18: Power Recovery ==="

# Clean up
rm -f /tmp/eneru-e2e-shutdown-flag*

# Start with on-battery (above thresholds -- no shutdown trigger)
cp $E2E_DIR/scenarios/on-battery.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Run Eneru in background
timeout 12 eneru run --config $E2E_DIR/config-e2e-dry-run.yaml 2>&1 | tee /tmp/test18.log &
ENERU_PID=$!

# Wait for Eneru to detect on-battery state
sleep 4

# Restore power
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# Wait for Eneru to detect recovery
wait $ENERU_PID 2>/dev/null || true

# Verify POWER_RESTORED was logged
if grep -qi "POWER_RESTORED\|power.*restored\|Power restored" /tmp/test18.log; then
  echo "PASS: Power restoration detected"
else
  echo "FAIL: POWER_RESTORED not logged"
  cat /tmp/test18.log
  exit 1
fi

# Verify NO shutdown was triggered
if grep -q "SHUTDOWN SEQUENCE" /tmp/test18.log; then
  echo "FAIL: Shutdown should NOT have been triggered during recovery"
  exit 1
fi

echo "PASS: Recovery correctly handled - no shutdown, power restored logged"
)

# ======================================================================
# Test 19: Multi-phase shutdown ordering (shutdown_order)
# ======================================================================
(
echo ""
echo ">>> Running: Test 19: Multi-phase shutdown ordering (shutdown_order)"

echo "=== Test 19: Multi-Phase Shutdown Ordering ==="

cd $E2E_DIR

# Reset shutdown state on all three SSH targets so we get fresh
# timestamps from this run only.
for svc in ssh-target ssh-target-2 ssh-target-3; do
  docker compose exec -T "$svc" sh -c \
    ": > /var/log/shutdown.log && rm -f /var/run/shutdown-triggered && touch /var/run/server-alive"
done

rm -f /tmp/eneru-e2e-shutdown-order-flag

# Trigger shutdown via low battery
cp scenarios/low-battery.dev scenarios/apply.dev
sleep 3

eneru run --config config-e2e-shutdown-order.yaml --exit-after-shutdown 2>&1 \
  | tee /tmp/test19.log || true

echo ""
echo "=== Verifying all three targets received shutdown ==="
for svc in ssh-target ssh-target-2 ssh-target-3; do
  if docker compose exec -T "$svc" test -f /var/run/shutdown-triggered; then
    echo "  PASS: $svc received shutdown"
  else
    echo "  FAIL: $svc did NOT receive shutdown"
    docker compose logs "$svc"
    exit 1
  fi
done

echo ""
echo "=== Verifying phase log lines in Eneru output ==="
if grep -q "Phase 1/2 (order=1)" /tmp/test19.log; then
  echo "PASS: Phase 1/2 (order=1) logged"
else
  echo "FAIL: Phase 1/2 (order=1) not found in Eneru output"
  cat /tmp/test19.log
  exit 1
fi

if grep -q "Phase 2/2 (order=2)" /tmp/test19.log; then
  echo "PASS: Phase 2/2 (order=2) logged"
else
  echo "FAIL: Phase 2/2 (order=2) not found in Eneru output"
  cat /tmp/test19.log
  exit 1
fi

echo ""
echo "=== Verifying timestamp ordering across phases ==="
# Each /var/log/shutdown.log has a single line:
#   Tue Apr 17 19:43:00 UTC 2026: Shutdown command received: -h now
# We extract the date prefix, parse to epoch, and assert
# phase-2 timestamp >= max(phase-1 timestamps).
parse_epoch() {
  local svc="$1"
  local line
  line=$(docker compose exec -T "$svc" cat /var/log/shutdown.log)
  local ts="${line%%: Shutdown*}"
  # Fail loudly on empty timestamp instead of letting `date` produce a
  # bogus "now" epoch that would silently corrupt the phase-ordering
  # comparison below.
  if [ -z "$ts" ]; then
    echo "ERROR: parse_epoch($svc): /var/log/shutdown.log is empty or missing the expected prefix" >&2
    return 1
  fi
  date -d "$ts" +%s
}

T1=$(parse_epoch ssh-target)
T2=$(parse_epoch ssh-target-2)
T3=$(parse_epoch ssh-target-3)
PHASE1_MAX=$T1
[ "$T2" -gt "$PHASE1_MAX" ] && PHASE1_MAX=$T2

echo "  Phase 1 timestamps: ssh-target=$T1, ssh-target-2=$T2 (max=$PHASE1_MAX)"
echo "  Phase 2 timestamp:  ssh-target-3=$T3"

if [ "$T3" -ge "$PHASE1_MAX" ]; then
  echo "PASS: phase-2 (T=$T3) ran at or after phase-1 max (T=$PHASE1_MAX)"
else
  echo "FAIL: phase-2 (T=$T3) ran BEFORE phase-1 max (T=$PHASE1_MAX) — ordering broken"
  exit 1
fi

echo ""
echo "=== Test 19 PASSED: multi-phase shutdown ordering verified ==="
)


# ======================================================================
# Test 36: v5.2.1 single-restart-notification — multi-UPS coordinator
#
# Same contract as Test 35 but exercises MultiUPSCoordinator instead of
# single-UPS UPSGroupMonitor. Verifies that
# _cancel_prev_pending_lifecycle_rows sweeps each per-UPS store on the
# next coordinator startup. Without that sweep, multi-UPS users still
# see two notifications on every restart even though _handle_signal
# correctly defers the stop.
# ======================================================================
(
echo ""
echo ">>> Running: Test 36: single-restart-notification multi-UPS (v5.2.1)"

echo "=== Test 36: v5.2.1 single-restart-notification (coord mode) ==="

# Clean slate.
rm -rf /tmp/eneru-e2e-restart-multi /tmp/eneru-e2e-restart-multi-*
mkdir -p /tmp/eneru-e2e-restart-multi
rm -f /tmp/eneru-e2e-restart-multi-shutdown-flag

# Start from on-line.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply.dev
sleep 3

# --- First run: coordinator startup, then SIGTERM. ---
eneru run --config $E2E_DIR/config-e2e-restart-multi.yaml > /tmp/test36-run1.log 2>&1 &
ENERU_PID=$!
sleep 6  # coordinator init + per-UPS monitors register stores + drain memory buffer
kill -TERM $ENERU_PID 2>/dev/null || true
wait $ENERU_PID 2>/dev/null || true

# At least one per-UPS store should have a pending 'Service Stopped'
# row from the coordinator's _handle_signal (the send goes through the
# worker's first registered store).
DBS=$(find /tmp/eneru-e2e-restart-multi -maxdepth 1 -name '*.db' 2>/dev/null)
if [ -z "$DBS" ]; then
  echo "FAIL: no SQLite stats DB created at /tmp/eneru-e2e-restart-multi/"
  cat /tmp/test36-run1.log
  exit 1
fi

total_pending_stop=0
for db in $DBS; do
  c=$(sqlite3 "$db" \
    "SELECT COUNT(*) FROM notifications \
     WHERE category='lifecycle' AND status='pending' \
       AND body LIKE '%Service Stopped%';" 2>/dev/null || echo 0)
  total_pending_stop=$((total_pending_stop + c))
done
if [ "$total_pending_stop" -lt "1" ]; then
  echo "FAIL (36a): expected at least 1 pending 'Service Stopped' row across stores, got $total_pending_stop"
  for db in $DBS; do
    echo "--- $db: ---"
    sqlite3 "$db" "SELECT id, ts, category, status, cancel_reason, substr(body,1,80) FROM notifications ORDER BY id;"
  done
  cat /tmp/test36-run1.log
  exit 1
fi
echo "PASS (36a): coordinator left $total_pending_stop pending 'Service Stopped' row(s)"

# --- Second run: same config, simulating restart. ---
eneru run --config $E2E_DIR/config-e2e-restart-multi.yaml > /tmp/test36-run2.log 2>&1 &
ENERU_PID=$!
sleep 7  # coordinator startup → _cancel_prev_pending_lifecycle_rows → new lifecycle send
kill -TERM $ENERU_PID 2>/dev/null || true
wait $ENERU_PID 2>/dev/null || true

# After the second coordinator's startup sweep, every prior 'Service
# Stopped' row should be status='cancelled' with cancel_reason='superseded'.
total_superseded=0
for db in $DBS; do
  c=$(sqlite3 "$db" \
    "SELECT COUNT(*) FROM notifications \
     WHERE category='lifecycle' AND status='cancelled' \
       AND cancel_reason='superseded' \
       AND body LIKE '%Service Stopped%';" 2>/dev/null || echo 0)
  total_superseded=$((total_superseded + c))
done
if [ "$total_superseded" -lt "$total_pending_stop" ]; then
  echo "FAIL (36b): expected >= $total_pending_stop superseded rows, got $total_superseded"
  for db in $DBS; do
    echo "--- $db: ---"
    sqlite3 "$db" "SELECT id, ts, category, status, cancel_reason, substr(body,1,80) FROM notifications ORDER BY id;"
  done
  cat /tmp/test36-run2.log
  exit 1
fi
echo "PASS (36b): $total_superseded prior 'Service Stopped' row(s) cancelled with reason='superseded'"

echo ""
echo "=== Test 36 PASSED: coordinator single-notification on restart verified ==="
)

echo ""
echo "=== Group 'multi-ups' completed successfully ==="
