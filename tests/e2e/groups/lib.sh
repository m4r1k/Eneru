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
#   A timeout (~10s) is a soft warning, not a hard failure: we fall
#   through like the old sleep did and let the test's own assertions
#   catch a genuinely-unapplied scenario, rather than converting a slow
#   apply into a spurious red build.
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
    # container-side watcher never copies a half-written trigger file.
    cp "$sdir/${scenario}.dev" "$sdir/${trigger}.tmp"
    mv -f "$sdir/${trigger}.tmp" "$sdir/${trigger}"

    # Poll up to ~10s (50 * 0.2s) for the dummy to confirm + mark applied.
    for i in $(seq 1 50); do
        if [ -f "$marker" ]; then
            return 0
        fi
        sleep 0.2
    done

    echo "WARN: apply_scenario '${scenario}' -> ${ups} timed out (~10s) waiting for the applied marker" >&2
    return 0
}
