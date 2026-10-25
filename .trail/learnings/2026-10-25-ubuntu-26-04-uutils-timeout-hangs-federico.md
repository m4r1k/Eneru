---
title: Ubuntu 26.04 uutils timeout hangs E2E if you signal the timeout wrapper twice
author: Federico
date: 2026-10-25
branch: ci/pin-py39-ubuntu-2404
cost: { minutes: 1, tokens: 0 }
id: l-d4db5a1d
scope: repo
status: active
src: agent
touches:
  - tests/e2e/groups/single-ups-auth.sh
---

## Decided
In E2E scripts, send signals (HUP/TERM) to the eneru process (`pgrep -P "$TIMEOUT_PID"`), never to the `timeout` wrapper. See Test 54 in tests/e2e/groups/single-ups-auth.sh.

## Tried and ruled out
- "GitHub flake, just re-run": ruled out. It hung on 5 of 5 runs on ubuntu-26.04 and on 0 runs on 24.04.
- Daemon deadlock after SIGHUP reload: ruled out. /health returned 200 right after the reload, and the reload/login calls succeeded.

## Watch out
Ubuntu 26.04 ships uutils coreutils (timeout 0.10.0). After relaying a received signal, it does wait() on the child with no deadline and signals blocked. A 2nd signal to the wrapper is never delivered, and the timeout no longer fires. GNU timeout keeps relaying, so this only shows up on ubuntu-latest (26.04). The symptom is an E2E job silently stuck until timeout-minutes cancels it, with no failure logs (the Collect-logs step runs only on failure, not on cancel).
