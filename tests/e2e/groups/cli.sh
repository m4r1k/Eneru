#!/usr/bin/env bash
#
# E2E group: cli
#
# Auto-extracted from .github/workflows/e2e.yml. Tests in this
# group run sequentially; each group runs as a separate parallel
# matrix job (see .github/workflows/e2e.yml).

set -euo pipefail

: "${E2E_DIR:=tests/e2e}"
export E2E_DIR

# ======================================================================
# Test 1: Validate E2E config
# ======================================================================
echo ""
echo ">>> Running: Test 1: Validate E2E config"

echo "=== Test 1: Config Validation ==="
eneru validate --config $E2E_DIR/config-e2e.yaml

# ======================================================================
# Test 8: Multi-UPS config validation
# ======================================================================
echo ""
echo ">>> Running: Test 8: Multi-UPS config validation"

echo "=== Test 8: Multi-UPS Config Validation ==="

# Verify both UPSes are reachable
upsc UPS1@localhost:3493 2>/dev/null | grep -q "ups.status" || { echo "FAIL: UPS1 not reachable"; exit 1; }
upsc UPS2@localhost:3493 2>/dev/null | grep -q "ups.status" || { echo "FAIL: UPS2 not reachable"; exit 1; }

echo "Both UPS1 and UPS2 reachable on NUT server"

# Validate multi-UPS config
eneru validate --config $E2E_DIR/config-e2e-multi-ups.yaml

echo "PASS: Multi-UPS config validates against real NUT server"

# ======================================================================
# Test 11: Ownership validation rejects non-local containers
# ======================================================================
echo ""
echo ">>> Running: Test 11: Ownership validation rejects non-local containers"

echo "=== Test 11: Ownership Validation ==="

cat > /tmp/config-bad-ownership.yaml <<YAML
ups:
  - name: "UPS1@localhost:3493"
    is_local: true
  - name: "UPS2@localhost:3493"
    containers:
      enabled: true
YAML

# validate should report ERROR for non-local group with containers
OUTPUT=$(eneru validate --config /tmp/config-bad-ownership.yaml 2>&1) || true

if echo "$OUTPUT" | grep -q "ERROR.*containers"; then
  echo "PASS: Ownership violation correctly detected"
else
  echo "FAIL: Ownership violation not detected"
  echo "$OUTPUT"
  exit 1
fi

# ======================================================================
# Test 12: CLI safety - bare eneru shows help
# ======================================================================
echo ""
echo ">>> Running: Test 12: CLI safety - bare eneru shows help"

echo "=== Test 12: CLI Safety ==="

OUTPUT=$(eneru 2>&1) || true
EXIT_CODE=$?

if echo "$OUTPUT" | grep -q "run\|validate\|monitor"; then
  echo "PASS: Bare 'eneru' shows help with subcommands"
else
  echo "FAIL: Bare 'eneru' did not show help"
  echo "$OUTPUT"
  exit 1
fi

# ======================================================================
# Test 13: TUI --once snapshot
# ======================================================================
echo ""
echo ">>> Running: Test 13: TUI --once snapshot"

echo "=== Test 13: TUI --once ==="

OUTPUT=$(eneru monitor --config $E2E_DIR/config-e2e-dry-run.yaml --once 2>&1)

if echo "$OUTPUT" | grep -q "TestUPS@localhost\|Eneru v"; then
  echo "PASS: TUI --once outputs UPS status"
else
  echo "FAIL: TUI --once did not produce expected output"
  echo "$OUTPUT"
  exit 1
fi

# ======================================================================
# Test 20: Redundancy-group config validation
# ======================================================================
echo ""
echo ">>> Running: Test 20: Redundancy-group config validation"

echo "=== Test 20: Redundancy-group config validation ==="

# 20a -- valid config validates with exit 0
eneru validate --config $E2E_DIR/config-e2e-redundancy.yaml | tee /tmp/test20a.log
if grep -q "Configuration is valid" /tmp/test20a.log; then
  echo "PASS (20a): valid redundancy config accepted"
else
  echo "FAIL (20a): expected 'Configuration is valid' in output"
  exit 1
fi
if ! grep -q "rack-1-dual-psu" /tmp/test20a.log; then
  echo "FAIL (20a): redundancy group name not surfaced in validate output"
  exit 1
fi
if ! grep -q "min_healthy=1" /tmp/test20a.log; then
  echo "FAIL (20a): quorum line not surfaced in validate output"
  exit 1
fi

# 20b -- malformed config (min_healthy: 0) exits non-zero with the right error
cat > /tmp/config-e2e-redundancy-bad.yaml <<'EOF'
ups:
  - name: "UPS1@localhost:3493"
  - name: "UPS2@localhost:3493"
redundancy_groups:
  - name: "broken"
    ups_sources: ["UPS1@localhost:3493", "UPS2@localhost:3493"]
    min_healthy: 0
behavior:
  dry_run: true
notifications:
  enabled: false
local_shutdown:
  enabled: false
  trigger_on: "none"
EOF

set +e
eneru validate --config /tmp/config-e2e-redundancy-bad.yaml > /tmp/test20b.log 2>&1
rc=$?
set -e
echo "  exit code: $rc"
cat /tmp/test20b.log

if [ "$rc" -eq 0 ]; then
  echo "FAIL (20b): malformed config validated with exit 0; expected non-zero"
  exit 1
fi
if ! grep -q "min_healthy must be >= 1" /tmp/test20b.log; then
  echo "FAIL (20b): expected 'min_healthy must be >= 1' error message"
  exit 1
fi

echo ""
echo "=== Test 20 PASSED: redundancy-group config validation verified ==="

# ======================================================================
# Test E1 (group-local): shell completion script is syntactically valid
# ======================================================================
# Not numbered in docs/testing.md -- a smoke-only check that the
# `eneru completion bash` subcommand emits a valid bash script. Item 7
# from the 5.1.0-rc8 work; lives in the `cli` group because it's a CLI
# concern and that group is fast.
echo ""
echo ">>> Running: Test E1: shell completion is syntactically valid"

eneru completion bash | bash -n
echo "PASS (E1a): bash completion script syntax-checks"

eneru completion zsh | head -1 | grep -q "^#compdef eneru"
echo "PASS (E1b): zsh completion script has #compdef header"

eneru completion fish | grep -q "complete -c eneru"
echo "PASS (E1c): fish completion script registers 'complete -c eneru'"


echo ""
echo "=== Group 'cli' completed successfully ==="
