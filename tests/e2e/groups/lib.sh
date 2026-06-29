#!/usr/bin/env bash
#
# Shared helpers for the E2E group scripts (tests/e2e/groups/*.sh).
# Source this near the top of a group, AFTER $E2E_DIR is resolved to an
# absolute path:
#
#     . "$E2E_DIR/groups/lib.sh"
#
# These helpers run on the HOST (the GitHub runner / dev box). The dummy
# NUT server runs in a container with tests/e2e/scenarios bind-mounted at
# /scenarios, so a file the host writes at $E2E_DIR/scenarios/<x> shows
# up inside the container at /scenarios/<x>, and the marker the container
# writes at /scenarios/applied-<ups> shows up for the host at
# $E2E_DIR/scenarios/applied-<ups>.

# apply_scenario <scenario> [ups]
#
#   Swap a dummy UPS to <scenario> (a basename under scenarios/, e.g.
#   "low-battery" for scenarios/low-battery.dev) and BLOCK until the
#   dummy confirms the new state is actually live in upsd. This replaces
#   the old blind `cp scenarios/<x>.dev scenarios/apply.dev; sleep 3`.
#
#   ups defaults to TestUPS (single-UPS mode). Pass UPS1 / UPS2 for the
#   per-UPS multi-UPS triggers.
#
#   Think of it like ordering at a deli counter that calls your number:
#   we take the old ticket off the board (delete the stale marker), drop
#   our order in the basket (publish the trigger atomically), then wait
#   for our number to be called (the marker reappears) instead of
#   guessing "three minutes should be enough". The watcher only calls the
#   number once upsd is serving the new ups.status (see
#   tests/e2e/nut-dummy/entrypoint.sh apply_one).
#
#   A timeout (~20s) is a HARD failure: the watcher publishes the marker
#   only once upsd is actually serving the new ups.status, so a timeout
#   means the scenario did not go live and every downstream assertion would
#   be running against stale state. Returning non-zero trips the group's
#   `set -euo pipefail` and fails the test loudly at the apply boundary
#   instead of as a confusing assertion failure three steps later. The
#   window is generous so a loaded runner doesn't flake.
apply_scenario() {
    local scenario="$1"
    local ups="${2:-TestUPS}"
    local sdir="$E2E_DIR/scenarios"
    local trigger marker i

    if [ "$ups" = "TestUPS" ]; then
        trigger="apply.dev"
    else
        trigger="apply-${ups}.dev"
    fi
    marker="$sdir/applied-${ups}"

    # Drop the stale marker FIRST so the poll below can only succeed on
    # the watcher's fresh confirmation for THIS apply, never a leftover.
    rm -f "$marker"

    # Atomic publish: write a temp in the same dir, then rename, so the
    # container-side watcher never copies a half-written trigger file. A
    # publish failure (e.g. a typo'd scenario name) is a test bug, not a
    # slow apply -- fail hard immediately instead of falling through to the
    # soft timeout below and silently running against the previous scenario.
    if ! cp "$sdir/${scenario}.dev" "$sdir/${trigger}.tmp"; then
        echo "ERROR: apply_scenario: cannot read scenario '$sdir/${scenario}.dev'" >&2
        return 1
    fi
    if ! mv -f "$sdir/${trigger}.tmp" "$sdir/${trigger}"; then
        echo "ERROR: apply_scenario: cannot publish trigger '$sdir/${trigger}'" >&2
        return 1
    fi

    # Poll up to ~20s (100 * 0.2s) for the dummy to confirm + mark applied.
    for i in $(seq 1 100); do
        if [ -f "$marker" ]; then
            return 0
        fi
        sleep 0.2
    done

    # Hard timeout: the watcher publishes the marker only once the new state
    # is actually served, so reaching here means the apply never went live in
    # 20s. Fail at the apply boundary (set -e trips on the non-zero return)
    # rather than letting the test run on against stale dummy state.
    echo "FAIL: apply_scenario '${scenario}' -> ${ups} timed out (~20s) waiting for the applied marker" >&2
    return 1
}

# ----------------------------------------------------------------------
# Redundancy group helpers (shared by redundancy-regression.sh and
# redundancy-quorum.sh). These were duplicated verbatim across both
# scripts; keep the single copy here. A sourcing script may set
# DBG_TAG before sourcing to label its dbg() lines (defaults to the
# script's own basename).
# ----------------------------------------------------------------------

# dbg <message>
#
#   Timestamped step marker. The redundancy regressions chain many
#   fixed-duration sleeps with docker-compose calls; when CI runners are
#   slow the script can be SIGTERMed mid-flight with no idea where it
#   hung. dbg() makes the boundary between phases self-diagnosing in the
#   runner log. Label is DBG_TAG (set per-script) or the script basename.
dbg() {
  printf '+++ %s [%s] %s\n' \
    "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "${DBG_TAG:-$(basename "$0")}" "$*"
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
    docker compose restart -t 2 nut-server >/dev/null
  )
  dbg "restart_redundancy_nut_server: docker compose restart returned"
  wait_for_redundancy_nut
  apply_scenario online-charging UPS1
  apply_scenario online-charging UPS2
  dbg "restart_redundancy_nut_server: UPS1+UPS2 reset to online (scenarios confirmed live)"
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
