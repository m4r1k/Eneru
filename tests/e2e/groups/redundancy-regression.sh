#!/usr/bin/env bash
#
# E2E group: redundancy-regression
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

# Timestamped step markers. The redundancy regressions chain many
# fixed-duration sleeps with docker-compose calls; when CI runners are slow
# the script can be SIGTERMed mid-flight with no idea where it hung. dbg()
# makes the boundary between phases self-diagnosing in the runner log.
dbg() {
  printf '+++ %s [redundancy-regression.sh] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$*"
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
echo "=== Group 'redundancy-regression' completed successfully ==="
