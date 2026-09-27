#!/usr/bin/env bash
#
# E2E group: redundancy-quorum
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

# Shared E2E helpers: apply_scenario (poll-until-applied scenario swaps) plus
# the redundancy group helpers (dbg / dump_redundancy_nut_state /
# wait_for_redundancy_nut / restart_redundancy_nut_server /
# stop_redundancy_nut_drivers). DBG_TAG labels this script's dbg() lines.
DBG_TAG="redundancy-quorum.sh"
. "$E2E_DIR/groups/lib.sh"

# ======================================================================
# Test 21: Redundancy quorum holds when 1 of 2 healthy
# ======================================================================
(
echo ""
echo ">>> Running: Test 21: Redundancy quorum holds when 1 of 2 healthy"

echo "=== Test 21: Quorum holds ==="
# UPS1 critical (low battery), UPS2 healthy
apply_scenario low-battery UPS1
apply_scenario online-charging UPS2

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
apply_scenario low-battery UPS1
apply_scenario low-battery UPS2

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
apply_scenario online-charging UPS1
apply_scenario online-charging UPS2
echo "PASS: Redundancy shutdown fired on exhausted quorum"
)

# ======================================================================
# Test 23: unknown_counts_as=critical surfaces UNKNOWN as failure
# ======================================================================
# F-141 (6.2.0 release review): this test used to keep both UPSes online
# and never produced an UNKNOWN member. Now it makes one for real: UPS1 is
# critical (low battery) and UPS2's dummy driver is killed, so upsd serves
# stale data for it; once UPS2's connection grace expires its snapshot is
# UNKNOWN. Think of two smoke detectors: one is beeping, the other has gone
# silent. With unknown_counts_as=critical the silent one counts as "not
# safe", so zero healthy members remain and the group must fire. (With
# `healthy` it would hold, which is exactly the policy this pins.)
(
echo ""
echo ">>> Running: Test 23: unknown_counts_as=critical surfaces UNKNOWN as failure"

echo "=== Test 23: UNKNOWN handling ==="
dbg "T23: restart_redundancy_nut_server (both drivers alive, both online)"
restart_redundancy_nut_server
apply_scenario low-battery UPS1

# Short-grace config (30s connection grace) keeps the wait bounded; it has
# unknown_counts_as=critical and degraded_counts_as=healthy.
timeout 150s eneru run --config "$E2E_DIR/config-e2e-redundancy-short-grace.yaml" --exit-after-shutdown \
  > /tmp/test23.log 2>&1 &
ENERU_PID=$!
trap 'kill "$ENERU_PID" 2>/dev/null || true; restart_redundancy_nut_server >/dev/null 2>&1 || true' EXIT

# Let both members publish good snapshots and clear the evaluator startup grace.
sleep 13
stop_redundancy_nut_driver UPS2

# UPS2 stays DEGRADED (counts healthy) inside its 30s grace, then turns
# UNKNOWN and quorum is lost. Poll (<=75s) instead of a fixed sleep.
for _i in $(seq 1 375); do
  grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test23.log && break
  sleep 0.2
done
kill "$ENERU_PID" 2>/dev/null || true
wait "$ENERU_PID" 2>/dev/null || true
trap - EXIT
restart_redundancy_nut_server

t23_fail() {
  echo "$1"
  echo "----- /tmp/test23.log -----"
  cat /tmp/test23.log
  dump_redundancy_nut_state "T23 failure"
  exit 1
}
grep -q "Redundancy group 'rack-1-dual-psu' evaluator started" /tmp/test23.log \
  || t23_fail "FAIL: evaluator did not start"
# The tally prints each member's RAW health (redundancy.py evaluate_once):
# UPS2 must be reported unknown and still be counted as not healthy.
grep -qF "quorum LOST (healthy=0, min_healthy=1; UPS1@localhost:3493=critical, UPS2@localhost:3493=unknown)" /tmp/test23.log \
  || t23_fail "FAIL: UNKNOWN UPS2 was not counted as critical in the quorum tally"
grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test23.log \
  || t23_fail "FAIL: quorum loss with an UNKNOWN member did not fire the redundancy shutdown"
echo "PASS: UNKNOWN member counted as critical (unknown_counts_as=critical)"
)

# ======================================================================
# Test 24: Both UPSes UNKNOWN -> fail-safe shutdown
# ======================================================================
# F-141: previously a copy of Test 22 (both low battery). Now both drivers
# are killed while the UPSes are ONLINE, so the only way the group can fire
# is the fail-safe path: every member UNKNOWN after its connection grace.
(
echo ""
echo ">>> Running: Test 24: Both UPSes UNKNOWN -> fail-safe shutdown"

echo "=== Test 24: Both UNKNOWN ==="
dbg "T24: restart_redundancy_nut_server (both drivers alive, both online)"
restart_redundancy_nut_server

timeout 150s eneru run --config "$E2E_DIR/config-e2e-redundancy-short-grace.yaml" --exit-after-shutdown \
  > /tmp/test24.log 2>&1 &
ENERU_PID=$!
trap 'kill "$ENERU_PID" 2>/dev/null || true; restart_redundancy_nut_server >/dev/null 2>&1 || true' EXIT

sleep 13
stop_redundancy_nut_drivers

for _i in $(seq 1 375); do
  grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test24.log && break
  sleep 0.2
done
kill "$ENERU_PID" 2>/dev/null || true
wait "$ENERU_PID" 2>/dev/null || true
trap - EXIT
restart_redundancy_nut_server

t24_fail() {
  echo "$1"
  echo "----- /tmp/test24.log -----"
  cat /tmp/test24.log
  dump_redundancy_nut_state "T24 failure"
  exit 1
}
grep -q "Redundancy group 'rack-1-dual-psu' evaluator started" /tmp/test24.log \
  || t24_fail "FAIL: evaluator did not start"
# degraded_counts_as=healthy: quorum holds until the SECOND member turns
# UNKNOWN, so the loss line must show both members unknown.
grep -qF "quorum LOST (healthy=0, min_healthy=1; UPS1@localhost:3493=unknown, UPS2@localhost:3493=unknown)" /tmp/test24.log \
  || t24_fail "FAIL: quorum loss was not driven by both members being UNKNOWN"
grep -q "REDUNDANCY GROUP SHUTDOWN" /tmp/test24.log \
  || t24_fail "FAIL: expected fail-safe shutdown with both members UNKNOWN"
echo "PASS: Fail-safe shutdown fired with both members UNKNOWN"
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
apply_scenario low-battery UPS1
apply_scenario online-charging UPS2

set +e
timeout 18s eneru run --config $E2E_DIR/config-e2e-redundancy-cross-group.yaml --exit-after-shutdown 2>&1 | tee /tmp/test25.log
RC=${PIPESTATUS[0]}
set -e
# 124 = still monitoring at the deadline, 0 = a clean --exit-after-shutdown;
# anything else is a crash or config error, which must not pass as "held".
if [ "$RC" -ne 124 ] && [ "$RC" -ne 0 ]; then
  echo "FAIL: eneru exited with code $RC"
  tail -40 /tmp/test25.log
  exit 1
fi
# Positive anchors: the evaluator ran, and UPS1's critical state was seen
# (a negative-only check would pass if the daemon never started).
if ! grep -q "Redundancy group 'rack-1-dual-psu' evaluator started" /tmp/test25.log; then
  echo "FAIL: evaluator did not start"
  tail -40 /tmp/test25.log
  exit 1
fi
if ! grep -q "\[E2E UPS1\] .*Trigger condition met (advisory, redundancy group)" /tmp/test25.log; then
  echo "FAIL: UPS1's low battery was never observed"
  tail -40 /tmp/test25.log
  exit 1
fi

# Redundancy quorum should hold
if grep -q "rack-1-dual-psu.* quorum LOST" /tmp/test25.log; then
  echo "FAIL: redundancy quorum should hold (UPS2 healthy)"
  tail -40 /tmp/test25.log
  exit 1
fi
# Restore
apply_scenario online-charging UPS1
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
apply_scenario low-battery UPS1
apply_scenario online-charging UPS2

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
apply_scenario online-charging UPS1
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
apply_scenario online-charging
apply_scenario low-battery UPS1
apply_scenario low-battery UPS2

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
apply_scenario online-charging UPS1
apply_scenario online-charging UPS2
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
apply_scenario online-charging UPS1
apply_scenario online-charging UPS2

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
apply_scenario low-battery UPS1
apply_scenario low-battery UPS2
sleep 8

# Phase 2: restore both -> evaluator must log "quorum restored -- re-armed"
# AND clear the executor's re-entry guard.
apply_scenario online-charging UPS1
apply_scenario online-charging UPS2
sleep 8

# Phase 3: drop both critical again -> SECOND quorum-loss shutdown.
apply_scenario low-battery UPS1
apply_scenario low-battery UPS2
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
apply_scenario online-charging UPS1
apply_scenario online-charging UPS2
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
apply_scenario low-battery UPS1
apply_scenario low-battery UPS2

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
apply_scenario online-charging UPS1
apply_scenario online-charging UPS2
echo "PASS: stale restart flag was cleared before redundancy shutdown"
)

# ======================================================================
# Test 63: Redundancy plan and live shutdown progress API
# ======================================================================
(
echo ""
echo ">>> Running: Test 63: Redundancy plan and live shutdown progress API"

apply_scenario online-charging UPS1
apply_scenario online-charging UPS2
eneru run --config "$E2E_DIR/config-e2e-redundancy.yaml" \
  --api --api-bind 127.0.0.1 --api-port 9193 > /tmp/test63.log 2>&1 &
ENERU_PID=$!
cleanup_test63() {
  kill -TERM "$ENERU_PID" 2>/dev/null || true
  wait "$ENERU_PID" 2>/dev/null || true
}
trap cleanup_test63 EXIT

PLAN_SEEN=false
for _ in $(seq 1 40); do
  if curl -fsS \
      http://127.0.0.1:9193/api/v1/redundancy-groups/rack-1-dual-psu/shutdown-plan \
      > /tmp/test63-plan.json 2>/dev/null; then
    PLAN_SEEN=true
    break
  fi
  sleep 0.5
done
if [ "$PLAN_SEEN" != true ]; then
  echo "FAIL: redundancy shutdown plan did not respond within 20 seconds"
  cat /tmp/test63-plan.json 2>/dev/null || true
  tail -60 /tmp/test63.log
  exit 1
fi
python3 - /tmp/test63-plan.json <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
assert payload["group"] == "rack-1-dual-psu"
assert payload["upsSources"] == ["UPS1@localhost:3493", "UPS2@localhost:3493"]
phases = payload["plan"]["phases"]
assert [phase["id"] for phase in phases] == [
    "vms", "containers", "filesystem-sync", "filesystem-unmount",
    "remote", "final-sync", "local-poweroff",
]
assert [phase["id"] for phase in phases if phase["enabled"]] == ["remote"]
PY

# Issue #98: the per-UPS 800 W override must differ from UPS2's inherited
# 1200 W default, and both configured watt ratings must beat the dummy's
# reported 1000 W ups.realpower.nominal (and its 1000 VA rating).
POWER_SEEN=false
# Keep the query in the raw-sample retention tier. A from=0 query is clamped
# to five years and selects hourly rollups, which a fresh daemon has not made.
POWER_FROM=$(($(date +%s) - 60))
for _ in $(seq 1 30); do
  if curl -fsS \
      "http://127.0.0.1:9193/api/v1/ups/UPS1%40localhost%3A3493/power?from=$POWER_FROM" \
      > /tmp/test63-power1.json 2>/dev/null && \
     curl -fsS \
      "http://127.0.0.1:9193/api/v1/ups/UPS2%40localhost%3A3493/power?from=$POWER_FROM" \
      > /tmp/test63-power2.json 2>/dev/null && \
     curl -fsS http://127.0.0.1:9193/api/v1/ups \
      > /tmp/test63-status.json 2>/dev/null && \
     python3 - /tmp/test63-power1.json /tmp/test63-power2.json \
       /tmp/test63-status.json <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    power1 = json.load(handle)["data"]
with open(sys.argv[2], encoding="utf-8") as handle:
    power2 = json.load(handle)["data"]
with open(sys.argv[3], encoding="utf-8") as handle:
    status = json.load(handle)
assert power1 and power2
assert power1[-1]["estimated"] and power1[-1]["watts"] == 200.0
assert power2[-1]["estimated"] and power2[-1]["watts"] == 300.0
group = status["redundancyGroups"][0]
assert group["upsSources"] == ["UPS1@localhost:3493", "UPS2@localhost:3493"]
assert group["telemetry"]["redundancyLoad"]["percent"] == 62.5
energy = group["telemetry"]["energy"]
assert energy and energy["membersReported"] == 2
member_kwh = [row["energy"]["todayKwh"] for row in status["ups"]]
assert all(value is not None for value in member_kwh)
assert abs(energy["todayKwh"] - round(sum(member_kwh), 6)) < 1e-9
PY
  then
    POWER_SEEN=true
    break
  fi
  sleep 0.5
done
if [ "$POWER_SEEN" != true ]; then
  echo "FAIL: per-UPS energy overrides or redundancy telemetry were not published"
  cat /tmp/test63-power1.json /tmp/test63-power2.json \
    /tmp/test63-status.json 2>/dev/null || true
  exit 1
fi

# Only UPS2's inherited 1200 W exceeds the reported 1000 W rating, so exactly
# one override warning is logged; UPS1's lower 800 W stays silent.
WARNINGS=$(grep -c "nominal_power (.* W) is above this UPS's reported" /tmp/test63.log || true)
if [ "$WARNINGS" != "1" ] || \
   ! grep -q "nominal_power (1200 W) is above this UPS's reported ups.realpower.nominal (1000 W)" /tmp/test63.log; then
  echo "FAIL: expected exactly one nominal_power override warning (1200 W > 1000 W), got $WARNINGS"
  tail -60 /tmp/test63.log
  exit 1
fi

apply_scenario low-battery UPS1
apply_scenario low-battery UPS2
PROGRESS_SEEN=false
for _ in $(seq 1 60); do
  if curl -fsS \
      http://127.0.0.1:9193/api/v1/redundancy-groups/rack-1-dual-psu/shutdown-progress \
      > /tmp/test63-progress.json 2>/dev/null && \
      python3 - /tmp/test63-progress.json <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
progress = payload["progress"]
# Anonymous readers never receive raw remote command output.
assert payload["remoteDetailAvailable"] is False
assert all("detail" not in remote for remote in progress["remotes"])
assert progress["runId"] > 0
assert progress["state"] == "succeeded"
assert progress["finishedAt"] is not None
remote_phase = next(phase for phase in progress["phases"]
                    if phase["id"] == "remote")
assert remote_phase["state"] == "succeeded"
assert progress["remotes"]
assert all(remote["state"] == "succeeded" for remote in progress["remotes"])
assert all(remote["finishedAt"] is not None for remote in progress["remotes"])
PY
  then
    PROGRESS_SEEN=true
    break
  fi
  sleep 0.5
done
if [ "$PROGRESS_SEEN" != true ]; then
  echo "FAIL: redundancy shutdown progress was never published"
  cat /tmp/test63-progress.json 2>/dev/null || true
  tail -60 /tmp/test63.log
  exit 1
fi

apply_scenario online-charging UPS1
apply_scenario online-charging UPS2
echo "PASS: per-UPS energy, redundancy telemetry, plan, and progress verified"
)

echo ""
echo "=== Group 'redundancy-quorum' completed successfully ==="
