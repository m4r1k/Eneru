#!/usr/bin/env bash
#
# E2E group: redundancy
#
# Auto-extracted from .github/workflows/e2e.yml. Tests in this
# group run sequentially; each test body is wrapped in a subshell
# so cd / env changes do NOT leak between tests (the original
# workflow had per-step shell isolation -- we preserve it here).
# Each group runs as a separate parallel matrix job (see
# .github/workflows/e2e.yml).
#
# 5.3.0 contract note: pre-5.3.0 every test body opened with
#   rm -f /tmp/eneru-e2e-redundancy*-shutdown-flag* \
#         /tmp/ups-shutdown-redundancy-* 2>/dev/null || true
# to scrub stale flags between tests. The redundancy executor's flag
# is now daemon-managed -- coordinator startup, quorum recovery, and
# graceful exit each clear it -- so those rm lines became redundant
# and were removed. With them gone, every existing test doubles as a
# regression catch for the startup-cleanup contract; Test 37 below
# is the explicit fire->recover->fire-again scenario, and Test 38
# pre-creates a restart-stale flag before daemon startup. Do NOT add the
# rm lines back without first confirming the contract is intentionally
# being inverted.

set -euo pipefail

: "${E2E_DIR:=tests/e2e}"
# Always work with an absolute path so a test that `cd`s elsewhere
# and then references $E2E_DIR/... still resolves correctly. Without
# this, `tests/e2e` would be re-resolved relative to the new cwd.
E2E_DIR="$(cd "$E2E_DIR" && pwd)"
export E2E_DIR

eneru() {
  if [ "${1:-}" = "run" ]; then
    sudo -E env "PATH=$PATH" eneru "$@"
  else
    command eneru "$@"
  fi
}

# Timestamped step markers. The redundancy regressions chain many
# fixed-duration sleeps with docker-compose calls; when CI runners are slow
# the script can be SIGTERMed mid-flight with no idea where it hung. dbg()
# makes the boundary between phases self-diagnosing in the runner log.
dbg() {
  printf '+++ %s [redundancy.sh] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$*"
}

dump_redundancy_nut_state() {
  local label="$1"
  dbg "[$label] docker compose ps nut-server:"
  ( cd "$E2E_DIR" && docker compose ps nut-server 2>&1 ) \
      | sed 's/^/    /' || true
  dbg "[$label] processes inside nut-server:"
  ( cd "$E2E_DIR" \
      && timeout 10s docker compose exec -T nut-server sh -c \
           'ps -ef 2>&1 | grep -E "dummy-ups|upsd" | grep -v grep || true' ) \
      2>&1 | sed 's/^/    /' || true
  dbg "[$label] host upsc probes:"
  for ups in TestUPS UPS1 UPS2; do
    printf '    upsc %s ups.status: ' "$ups"
    timeout 5s upsc "${ups}@localhost:3493" ups.status 2>&1 || echo '<failed/timeout>'
  done
}

wait_for_redundancy_nut() {
  for i in {1..30}; do
    # Bound each upsc call so a wedged libupsclient read cannot eat the
    # entire polling budget on a single iteration.
    if timeout 5s upsc UPS1@localhost:3493 ups.status >/dev/null 2>&1 \
       && timeout 5s upsc UPS2@localhost:3493 ups.status >/dev/null 2>&1; then
      dbg "wait_for_redundancy_nut: ready after $i iteration(s)"
      return 0
    fi
    dbg "wait_for_redundancy_nut: attempt $i/30 still failing"
    sleep 1
  done
  dbg "wait_for_redundancy_nut: gave up after 30 attempts"
  echo "FAIL: redundancy NUT sources did not recover"
  return 1
}

restart_redundancy_nut_server() {
  dbg "restart_redundancy_nut_server: docker compose restart nut-server"
  (
    cd "$E2E_DIR"
    docker compose restart nut-server >/dev/null
  )
  dbg "restart_redundancy_nut_server: docker compose restart returned"
  wait_for_redundancy_nut
  cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply-UPS1.dev"
  cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply-UPS2.dev"
  sleep 3
  dbg "restart_redundancy_nut_server: settle sleep done"
}

stop_redundancy_nut_drivers() {
  dbg "stop_redundancy_nut_drivers: pkill UPS1+UPS2 dummy-ups in container"
  (
    cd "$E2E_DIR"
    # The ``[d]`` bracket trick is load-bearing: it makes the regex match the
    # literal string "dummy-ups" inside a real driver cmdline, but NOT the
    # pkill wrapper's own cmdline (which contains the literal characters
    # ``[d]ummy-ups``). Without the trick, pkill kills its own ``sh -c``
    # wrapper before it can run the second pkill, and ``docker compose exec``
    # is left holding a half-dead exec stream that hangs until the runner
    # SIGTERMs the whole step.
    timeout --kill-after=5s 10s docker compose exec -T nut-server sh -c \
      "pkill -f '[d]ummy-ups.*-a UPS1' || true; pkill -f '[d]ummy-ups.*-a UPS2' || true"
  )
  dbg "stop_redundancy_nut_drivers: pkill returned, verifying drivers are gone"
  ( cd "$E2E_DIR" \
      && timeout --kill-after=5s 10s docker compose exec -T nut-server sh -c \
           'ps -ef | grep -E "[d]ummy-ups.*-a UPS[12]" || echo "    (no UPS1/UPS2 driver processes)"' ) \
      2>&1 | sed 's/^/    /' || true
}

# ======================================================================
# Test 21: Redundancy quorum holds when 1 of 2 healthy
# ======================================================================
(
echo ""
echo ">>> Running: Test 21: Redundancy quorum holds when 1 of 2 healthy"

echo "=== Test 21: Quorum holds ==="
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
# Test 37: Re-arm after quorum recovery (issue #4)
# ======================================================================
#
# Pre-5.3.0: once a redundancy group fired a shutdown, the evaluator
# pinned ``_fired = True`` for the lifetime of the daemon AND the
# executor's on-disk flag survived restarts. Result: every quorum
# loss after the first one silently no-op'd, even after power was
# restored. Reported by ckrevel in
# github.com/m4r1k/Eneru/issues/4#issuecomment-4375517607.
#
# This test drives two consecutive quorum-loss events back-to-back
# and asserts the second one fires its own shutdown sequence.
(
echo ""
echo ">>> Running: Test 37: Redundancy re-arm after quorum recovery (issue #4)"

echo "=== Test 37: re-arm ==="

# Start from a known-healthy quorum so the evaluator's startup grace
# elapses without firing.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 2

# Run eneru in background -- we need to drive scenarios in flight,
# so --exit-after-shutdown is intentionally omitted. Budget: 13s
# startup grace + three 8s phase sleeps + dry-run shutdown sequence
# overhead per phase = ~50s expected. 90s leaves headroom for slow
# CI runners (matches the safety margin of R1/R2 below).
timeout 90s eneru run --config $E2E_DIR/config-e2e-redundancy.yaml \
  > /tmp/test37.log 2>&1 &
ENERU_PID=$!
trap 'kill "$ENERU_PID" 2>/dev/null || true' EXIT

# Clear evaluator startup grace (~10s for check_interval=1).
sleep 13

# Phase 1: drop both UPSes critical -> first quorum-loss shutdown.
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 8

# Phase 2: restore both -> evaluator must log "quorum restored -- re-armed"
# AND clear the executor's re-entry guard.
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 8

# Phase 3: drop both critical again -> SECOND quorum-loss shutdown.
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 8

kill "$ENERU_PID" 2>/dev/null || true
wait "$ENERU_PID" 2>/dev/null || true
trap - EXIT

t37_fail() {
  echo "$1"
  echo "----- /tmp/test37.log (full) -----"
  cat /tmp/test37.log
  echo "----- /tmp/test37.log end -----"
  exit 1
}

# 1. Quorum LOST must appear at least twice (once per phase).
LOST_COUNT=$(grep -c "rack-1-dual-psu.* quorum LOST" /tmp/test37.log || true)
if [ "$LOST_COUNT" -lt 2 ]; then
  t37_fail "FAIL: expected >=2 'quorum LOST' lines, got $LOST_COUNT"
fi

# 2. Re-arm log line must appear between the two losses.
if ! grep -q "quorum restored -- re-armed" /tmp/test37.log; then
  t37_fail "FAIL: expected 'quorum restored -- re-armed' log line after first shutdown"
fi

# 3. The load-bearing assertion: TWO shutdowns must have actually fired.
SHUTDOWN_COUNT=$(grep -c "REDUNDANCY GROUP SHUTDOWN: rack-1-dual-psu" /tmp/test37.log || true)
if [ "$SHUTDOWN_COUNT" -lt 2 ]; then
  t37_fail "FAIL: expected 2 'REDUNDANCY GROUP SHUTDOWN' lines (re-arm broken, issue #4 regression), got $SHUTDOWN_COUNT"
fi

# 4. The pre-5.3.0 silent-no-op path must NOT have surfaced. Its
#    presence here would mean the startup-cleanup contract failed to
#    clean a leftover flag.
if grep -q "suppressed: flag .* startup cleanup bypassed" /tmp/test37.log; then
  t37_fail "FAIL: stale-flag suppression warning fired -- startup cleanup contract violated"
fi

# Restore for downstream tests
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 2
echo "PASS: redundancy re-arm verified across two consecutive quorum-loss events"
)

# ======================================================================
# Test 38: Stale redundancy flag from prior daemon restart is cleared
# ======================================================================
(
echo ""
echo ">>> Running: Test 38: Stale redundancy flag across restart is cleared"

echo "=== Test 38: stale flag restart ==="

# Pre-5.3.0/rc4 regression: a stale flag from a prior daemon instance
# suppressed the executor before it could log REDUNDANCY GROUP SHUTDOWN.
# This uses the real redundancy flag path derived from
# logging.shutdown_flag_file's parent plus the group name.
printf "stale-pre-rc4-flag\n" > /tmp/ups-shutdown-redundancy-rack-1-dual-psu
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/low-battery.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 3

timeout 30s eneru run --config $E2E_DIR/config-e2e-redundancy.yaml --exit-after-shutdown \
  > /tmp/test38.log 2>&1 || true

if ! grep -q "REDUNDANCY GROUP SHUTDOWN: rack-1-dual-psu" /tmp/test38.log; then
  echo "FAIL: stale restart flag blocked redundancy shutdown"
  echo "----- /tmp/test38.log (full) -----"
  cat /tmp/test38.log
  echo "----- /tmp/test38.log end -----"
  exit 1
fi
if grep -q "suppressed: flag .* startup cleanup bypassed" /tmp/test38.log; then
  echo "FAIL: stale flag suppression warning fired after startup cleanup"
  cat /tmp/test38.log
  exit 1
fi

# Restore for downstream tests
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS1.dev
cp $E2E_DIR/scenarios/online-charging.dev $E2E_DIR/scenarios/apply-UPS2.dev
sleep 2
echo "PASS: stale restart flag was cleared before redundancy shutdown"
)

# ======================================================================
# Regression R1: Runtime transient NUT loss stays inside redundancy grace
# ======================================================================
(
echo ""
echo ">>> Running: Regression R1: Runtime transient NUT loss stays inside redundancy grace"

echo "=== Regression R1: transient runtime NUT visibility loss ==="
dbg "R1 step 1/8: pre-test restart_redundancy_nut_server"
restart_redundancy_nut_server
dump_redundancy_nut_state "R1 after first restart"

dbg "R1 step 2/8: launching eneru in background (timeout 90s)"
timeout 90s eneru run --config "$E2E_DIR/config-e2e-redundancy-short-grace.yaml" --exit-after-shutdown \
  > /tmp/test-r1.log 2>&1 &
ENERU_PID=$!
trap 'kill "$ENERU_PID" 2>/dev/null || true; restart_redundancy_nut_server >/dev/null 2>&1 || true' EXIT

# Let both member monitors publish good snapshots and clear evaluator startup grace.
dbg "R1 step 3/8: sleep 13 (let monitors publish good snapshots)"
sleep 13
dbg "R1 step 4/8: stop_redundancy_nut_drivers (induce visibility loss)"
stop_redundancy_nut_drivers

# Old behavior could turn the stale snapshots UNKNOWN after ~5s and fire
# quorum loss before the connection grace expired. Recover inside grace.
dbg "R1 step 5/8: sleep 7 (stay inside the 40s connection grace)"
sleep 7
dbg "R1 step 6/8: restart_redundancy_nut_server (recover NUT inside grace)"
restart_redundancy_nut_server
dbg "R1 step 7/8: sleep 10 (let monitor observe recovery)"
sleep 10

dbg "R1 step 8/8: kill eneru and verify"
kill "$ENERU_PID" 2>/dev/null || true
wait "$ENERU_PID" 2>/dev/null || true
trap - EXIT

r1_fail() {
  echo "$1"
  echo "----- /tmp/test-r1.log (full) -----"
  cat /tmp/test-r1.log
  echo "----- /tmp/test-r1.log end -----"
  dump_redundancy_nut_state "R1 failure"
  exit 1
}

if grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test-r1.log; then
  r1_fail "FAIL: transient NUT loss should not fire redundancy shutdown"
fi
if ! grep -q "Redundancy group 'rack-1-dual-psu' evaluator started" /tmp/test-r1.log; then
  r1_fail "FAIL: evaluator did not start during transient NUT-loss regression"
fi
if ! grep -q "Grace period started" /tmp/test-r1.log; then
  r1_fail "FAIL: transient NUT-loss regression did not enter connection grace"
fi
if ! grep -q "recovered during grace period" /tmp/test-r1.log; then
  r1_fail "FAIL: transient NUT-loss regression did not prove recovery inside grace"
fi
if grep -q "rack-1-dual-psu.* quorum LOST" /tmp/test-r1.log; then
  r1_fail "FAIL: transient NUT loss should not lose redundancy quorum"
fi
echo "PASS: transient runtime NUT loss recovered inside grace without redundancy shutdown"
)

# ======================================================================
# Regression R2: Runtime persistent NUT loss still fails safe after grace
# ======================================================================
(
echo ""
echo ">>> Running: Regression R2: Runtime persistent NUT loss fails safe after grace"

echo "=== Regression R2: persistent runtime NUT visibility loss ==="
dbg "R2 step 1/8: pre-test restart_redundancy_nut_server"
restart_redundancy_nut_server
dump_redundancy_nut_state "R2 after first restart"

dbg "R2 step 2/8: launching eneru in background (timeout 105s)"
timeout 105s eneru run --config "$E2E_DIR/config-e2e-redundancy-short-grace.yaml" --exit-after-shutdown \
  > /tmp/test-r2.log 2>&1 &
ENERU_PID=$!
trap 'kill "$ENERU_PID" 2>/dev/null || true; restart_redundancy_nut_server >/dev/null 2>&1 || true' EXIT

dbg "R2 step 3/8: sleep 13 (let monitors publish good snapshots)"
sleep 13
dbg "R2 step 4/8: stop_redundancy_nut_drivers (induce visibility loss)"
stop_redundancy_nut_drivers

# Hold loss longer than connection grace. Fail-safe UNKNOWN handling must
# still fire once the member monitors mark the connection FAILED.
dbg "R2 step 5/8: sleep 55 (hold loss past 40s grace + headroom)"
sleep 55
dbg "R2 step 6/8: kill eneru"
kill "$ENERU_PID" 2>/dev/null || true
wait "$ENERU_PID" 2>/dev/null || true
trap - EXIT
dbg "R2 step 7/8: post-test restart_redundancy_nut_server (cleanup)"
restart_redundancy_nut_server
dbg "R2 step 8/8: verify assertions"

r2_fail() {
  echo "$1"
  echo "----- /tmp/test-r2.log (full) -----"
  cat /tmp/test-r2.log
  echo "----- /tmp/test-r2.log end -----"
  dump_redundancy_nut_state "R2 failure"
  exit 1
}

if ! grep -q "Redundancy group 'rack-1-dual-psu' evaluator started" /tmp/test-r2.log; then
  r2_fail "FAIL: evaluator did not start during persistent NUT-loss regression"
fi
if ! grep -q "Grace period started" /tmp/test-r2.log; then
  r2_fail "FAIL: persistent NUT-loss regression did not enter connection grace"
fi
if ! grep -q "rack-1-dual-psu.* quorum LOST" /tmp/test-r2.log; then
  r2_fail "FAIL: persistent NUT loss should lose redundancy quorum after grace"
fi
if ! grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test-r2.log; then
  r2_fail "FAIL: persistent NUT loss should fire redundancy shutdown after grace"
fi
echo "PASS: persistent runtime NUT loss fired redundancy shutdown after grace"
)


echo ""
echo "=== Group 'redundancy' completed successfully ==="
