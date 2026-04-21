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

# Run Eneru for 5 seconds - should NOT trigger shutdown
timeout 5 eneru run --config $E2E_DIR/config-e2e-dry-run.yaml 2>&1 | tee /tmp/test2.log || true

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

# Run Eneru in dry-run mode
eneru run --config $E2E_DIR/config-e2e-dry-run.yaml --exit-after-shutdown 2>&1 | tee /tmp/test3.log || true

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

# The dummy NUT driver in this E2E env reports input.transfer.{low,high},
# so the second informational line should be present too. Soft-PASS
# (don't fail) because some NUT drivers don't expose those fields.
if grep -q "UPS battery-switch points:" /tmp/test6.log; then
  echo "PASS (6c): startup log shows UPS battery-switch points line"
else
  echo "Note (6c): UPS battery-switch points line absent -- driver may"
  echo "  not expose input.transfer.{low,high}."
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

echo ""
echo "=== Group 'single-ups' completed successfully ==="
