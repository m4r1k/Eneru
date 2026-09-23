#!/usr/bin/env bash
#
# E2E group: cli
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
# Test 1: Validate E2E config
# ======================================================================
(
echo ""
echo ">>> Running: Test 1: Validate E2E config"

echo "=== Test 1: Config Validation ==="
eneru validate --config $E2E_DIR/config-e2e.yaml
)

# ======================================================================
# Test 8: Multi-UPS config validation
# ======================================================================
(
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
)

# ======================================================================
# Test 11: Ownership validation rejects non-local containers
# ======================================================================
(
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
)

# ======================================================================
# Test 12: CLI safety - bare eneru shows help
# ======================================================================
(
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
)

# ======================================================================
# Test 13: TUI --once snapshot
# ======================================================================
(
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
)

# ======================================================================
# Test 20: Redundancy-group config validation
# ======================================================================
(
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
)

# ======================================================================
# Test E1: shell completion is syntactically valid
# ======================================================================
# Not numbered in docs/testing.md -- a smoke-only check that the
# `eneru completion bash` subcommand emits a valid bash script.
# Item 7 from the 5.1.0-rc8 work; lives in the `cli` group because
# it is a CLI concern and that group is fast.
(
echo ""
echo ">>> Running: Test E1: shell completion is syntactically valid"

# Capture each script first, then assert. Piping straight into
# `grep -q` under `set -o pipefail` can SIGPIPE the producer (`grep -q`
# closes stdin on first match), surfacing as exit 141 and tripping
# `set -e`. Capturing avoids the pipe entirely.
BASH_COMPLETION="$(eneru completion bash)"
echo "$BASH_COMPLETION" | bash -n
echo "PASS (E1a): bash completion script syntax-checks"

ZSH_COMPLETION="$(eneru completion zsh)"
case "$ZSH_COMPLETION" in
  '#compdef eneru'*) echo "PASS (E1b): zsh completion script has #compdef header" ;;
  *) echo "FAIL (E1b): zsh completion missing #compdef header"; exit 1 ;;
esac

FISH_COMPLETION="$(eneru completion fish)"
case "$FISH_COMPLETION" in
  *"complete -c eneru"*) echo "PASS (E1c): fish completion registers complete -c eneru" ;;
  *) echo "FAIL (E1c): fish completion missing 'complete -c eneru' line"; exit 1 ;;
esac
)

# ======================================================================
# Test 51: auth foundation — user + apikey CLI lifecycle (v6.0)
# ======================================================================
# Exercises eneru user create/list/show/passwd/delete and apikey
# create/list/revoke end-to-end against a real bcrypt install and a
# real SQLite auth.db. Proves the [auth] extra is wired in the E2E
# environment and that the store round-trips outside unit mocks.
(
echo ""
echo ">>> Running: Test 51: auth user + apikey CLI lifecycle"

AUTH_DB="$(mktemp -d)/auth.db"

# Capture-then-match throughout: piping a command straight into `grep -q` can
# SIGPIPE the producer under `set -o pipefail` (exit 141). Capture first.
OUT="$(eneru user create alice --generate --auth-db "$AUTH_DB")"
case "$OUT" in *"Created user 'alice'"*) ;; *) echo "FAIL: user create"; exit 1;; esac

OUT="$(printf 'hunter2pw' | eneru user create bob --password-stdin --auth-db "$AUTH_DB")"
case "$OUT" in *"Created user 'bob'"*) ;; *) echo "FAIL: user create stdin"; exit 1;; esac

# duplicate is rejected (non-zero exit)
if eneru user create alice --generate --auth-db "$AUTH_DB" >/dev/null 2>&1; then
  echo "FAIL: duplicate user was allowed"; exit 1
fi
echo "PASS: duplicate user rejected"

# list + show expose metadata, never a hash
OUT="$(eneru user list --auth-db "$AUTH_DB")"
case "$OUT" in *alice*) ;; *) echo "FAIL: user list"; exit 1;; esac
SHOW="$(eneru user show bob --auth-db "$AUTH_DB")"
case "$SHOW" in *"Username:"*) ;; *) echo "FAIL: user show"; exit 1;; esac
case "$SHOW" in *'$2b$'*) echo "FAIL: user show leaked a hash"; exit 1;; esac

# passwd reset
OUT="$(printf 'newpw12345' | eneru user passwd bob --password-stdin --auth-db "$AUTH_DB")"
case "$OUT" in *"Updated password for 'bob'"*) ;; *) echo "FAIL: user passwd"; exit 1;; esac

# apikey create prints the key once, list never shows it, revoke removes it
KEYOUT="$(eneru apikey create --label grafana --auth-db "$AUTH_DB")"
case "$KEYOUT" in *"API key: eneru_"*) ;; *) echo "FAIL: apikey create"; exit 1;; esac
LIST="$(eneru apikey list --auth-db "$AUTH_DB")"
case "$LIST" in *grafana*) ;; *) echo "FAIL: apikey list"; exit 1;; esac
case "$LIST" in *eneru_*) echo "FAIL: apikey list leaked key"; exit 1;; esac
OUT="$(eneru apikey revoke 1 --auth-db "$AUTH_DB")"
case "$OUT" in *"Revoked API key #1"*) ;; *) echo "FAIL: apikey revoke"; exit 1;; esac

# delete + missing-user error
OUT="$(eneru user delete bob --auth-db "$AUTH_DB")"
case "$OUT" in *"Deleted user 'bob'"*) ;; *) echo "FAIL: user delete"; exit 1;; esac
if eneru user delete nobody --auth-db "$AUTH_DB" >/dev/null 2>&1; then
  echo "FAIL: deleting missing user succeeded"; exit 1
fi
echo "PASS: auth user + apikey CLI lifecycle"
)

# ======================================================================
# Test 58: NUT name autodiscovery self-heals single exposed UPS
# ======================================================================
# Issue #71: when ups.name uses a wrong NUT device name but the target
# server exposes exactly one UPS, Eneru should list names with `upsc -l`,
# auto-correct only the runtime poll target, and tell the operator to fix
# config. This uses a separate temporary single-UPS NUT container so
# the shared E2E compose services remain untouched.
(
echo ""
echo ">>> Running: Test 58: NUT name autodiscovery self-heals single exposed UPS"

docker build -t eneru:e2e-nut-single "$E2E_DIR/nut-dummy"
docker rm -f eneru-e2e-nut-single >/dev/null 2>&1 || true
docker run -d --name eneru-e2e-nut-single \
  -p 127.0.0.1:3494:3493 \
  --entrypoint /bin/bash \
  eneru:e2e-nut-single \
  -c '
set -e
cat >/etc/nut/ups.conf <<EOF
[TestUPS]
    driver = dummy-ups
    port = TestUPS.dev
    desc = "Eneru E2E Test UPS (single autodiscovery)"
EOF
chown nut:nut /etc/nut/ups.conf /etc/nut/TestUPS.dev
chmod 640 /etc/nut/ups.conf
nohup /usr/lib/nut/dummy-ups -a TestUPS -D >/tmp/e2e-test58-dummy.log 2>&1 &
sleep 2
exec /usr/sbin/upsd -D -F
' >/dev/null
cleanup_test58() {
  docker rm -f eneru-e2e-nut-single >/dev/null 2>&1 || true
}
trap cleanup_test58 EXIT

nut_single_ready=false
for _ in $(seq 1 30); do
  if upsc TestUPS@localhost:3494 2>/dev/null | grep -q "ups.status"; then
    nut_single_ready=true
    break
  fi
  sleep 1
done
if [ "$nut_single_ready" != "true" ]; then
  echo "FAIL: temporary single-UPS NUT container never became ready"
  docker logs eneru-e2e-nut-single || true
  exit 1
fi

NAMES="$(upsc -l localhost:3494 2>/dev/null | tr -d '\r')"
if [ "$NAMES" != "TestUPS" ]; then
  echo "FAIL: expected temporary NUT server to expose exactly TestUPS"
  printf 'Observed names:\n%s\n' "$NAMES"
  docker logs eneru-e2e-nut-single || true
  exit 1
fi
echo "PASS: temporary NUT server exposes exactly one UPS name"

cat >/tmp/config-e2e-nut-name-autodiscovery.yaml <<'YAML'
ups:
  name: upsmon@localhost:3494
  display_name: "E2E Wrong NUT Name"
  check_interval: 1
  max_stale_data_tolerance: 5
behavior:
  dry_run: true
local_shutdown:
  enabled: false
  trigger_on: none
remote_health:
  enabled: false
statistics:
  db_directory: /tmp/e2e-test58-stats
logging:
  file: null
  state_file: /tmp/e2e-test58-state
  battery_history_file: /tmp/e2e-test58-history
  shutdown_flag_file: /tmp/e2e-test58-shutdown-flag
YAML

set +e
timeout 55s eneru run --config /tmp/config-e2e-nut-name-autodiscovery.yaml \
  >/tmp/test58.log 2>&1
rc=$?
set -e
cat /tmp/test58.log
if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ]; then
  echo "FAIL: autodiscovery daemon exited unexpectedly with status $rc"
  exit 1
fi

if ! grep -q "Auto-correcting this session" /tmp/test58.log; then
  echo "FAIL: autodiscovery did not log session auto-correction"
  exit 1
fi
if ! grep -q "poll 'TestUPS@localhost:3494'" /tmp/test58.log; then
  echo "FAIL: autodiscovery did not switch runtime poll target to TestUPS"
  exit 1
fi
if ! grep -q "Please fix ups.name in your config" /tmp/test58.log; then
  echo "FAIL: autodiscovery did not tell the operator to fix config"
  exit 1
fi
if ! grep -q "value before '@' in ups.name must be the UPS device name" /tmp/test58.log; then
  echo "FAIL: autodiscovery did not include the username-vs-device-name hint"
  exit 1
fi

echo "PASS: NUT name autodiscovery self-healed the single-UPS target"
cleanup_test58
trap - EXIT
)

# ======================================================================
# Test 65: config check live-probes NUT, SSH, sudo and every command
# ======================================================================
# v6.2 `eneru config check`: the building inspector walks the real NUT server
# and SSH target read-only. It must log in to NUT, SSH in, prove sudo with
# `sudo -n -l` WITHOUT running the shutdown command, find binaries through
# Eneru's augmented remote PATH, flag missing tools in red, and never execute
# a custom command or the final shutdown command.
(
echo ""
echo ">>> Running: Test 65: config check live-probes NUT, SSH, sudo and commands"

docker exec eneru-e2e-ssh rm -f /tmp/eneru-path-augmented /var/run/shutdown-triggered

cat >/tmp/config-e2e-config-check.yaml <<'YAML'
ups:
  name: "TestUPS@localhost:3493"
nut_control:
  username: "admin"
  password: "testpass"
behavior:
  dry_run: true
local_shutdown:
  enabled: false
remote_servers:
  - name: "Probe Target"
    enabled: true
    host: "localhost"
    user: "testuser"
    use_sudo: true
    shutdown_command: "shutdown -h now"
    ssh_options:
      - "-o Port=2222"
      - "-o StrictHostKeyChecking=no"
      - "-o UserKnownHostsFile=/dev/null"
      - "-o IdentityFile=/tmp/e2e-ssh-key"
    pre_shutdown_commands:
      - action: "sync"
      - action: "stop_containers"
      - command: "eneru-path-probe"
  - name: "Missing Tool"
    enabled: true
    host: "localhost"
    user: "testuser"
    shutdown_command: "sudo -n synoshutdown -s"
    ssh_options:
      - "-o Port=2222"
      - "-o StrictHostKeyChecking=no"
      - "-o UserKnownHostsFile=/dev/null"
      - "-o IdentityFile=/tmp/e2e-ssh-key"
YAML

set +e
eneru config check --config /tmp/config-e2e-config-check.yaml >/tmp/test65.log 2>&1
rc=$?
set -e
cat /tmp/test65.log
if [ "$rc" -ne 1 ]; then
  echo "FAIL: config check should exit 1 on the intentional errors (got $rc)"; exit 1
fi
expect() {
  if ! grep -qF -- "$1" /tmp/test65.log; then
    echo "FAIL: expected in config check output: $1"; exit 1
  fi
}
expect "NUT server localhost:3493 lists UPS 'TestUPS'"
expect "NUT login as 'admin' works"
expect "Probe Target: SSH as testuser@localhost works"
expect "Probe Target: 'shutdown' is installed"
expect "Probe Target: sudo allows 'shutdown -h now' without a password"
expect "Probe Target: 'eneru-path-probe' is installed"
expect "Probe Target: failed: listing containers works"
expect "Missing Tool: 'synoshutdown' is NOT installed"
expect "What happens on power loss"
# Read-only guarantees: nothing custom ran, nothing was powered off.
if docker exec eneru-e2e-ssh test -e /tmp/eneru-path-augmented; then
  echo "FAIL: config check EXECUTED a custom pre-shutdown command"; exit 1
fi
if docker exec eneru-e2e-ssh test -e /var/run/shutdown-triggered; then
  echo "FAIL: config check EXECUTED the shutdown command"; exit 1
fi

# Argument-pinned sudoers rule (the documented Synology pattern): sudo -n -l
# must be asked about the EXACT command, so the matching one passes and a
# different argument list is flagged -- still without executing anything.
docker exec eneru-e2e-ssh sh -c '
  id pinned >/dev/null 2>&1 || adduser -D -s /bin/bash pinned
  echo "pinned:$(head -c 12 /dev/urandom | od -An -tx1 | tr -d " \n")" | chpasswd >/dev/null
  mkdir -p /home/pinned/.ssh
  cp /home/testuser/.ssh/authorized_keys /home/pinned/.ssh/authorized_keys
  chown -R pinned:pinned /home/pinned/.ssh
  chmod 700 /home/pinned/.ssh; chmod 600 /home/pinned/.ssh/authorized_keys
  grep -q "^pinned " /etc/sudoers || echo "pinned ALL=(ALL) NOPASSWD: /usr/local/bin/shutdown -h now" >> /etc/sudoers
'
cat >/tmp/config-e2e-config-check-pinned.yaml <<'YAML'
ups:
  name: "TestUPS@localhost:3493"
behavior:
  dry_run: true
local_shutdown:
  enabled: false
remote_servers:
  - name: "Pinned OK"
    enabled: true
    host: "localhost"
    user: "pinned"
    shutdown_command: "sudo shutdown -h now"
    ssh_options: ["-o Port=2222", "-o StrictHostKeyChecking=no", "-o UserKnownHostsFile=/dev/null", "-o IdentityFile=/tmp/e2e-ssh-key"]
  - name: "Pinned Wrong Args"
    enabled: true
    host: "localhost"
    user: "pinned"
    shutdown_command: "sudo shutdown -r now"
    ssh_options: ["-o Port=2222", "-o StrictHostKeyChecking=no", "-o UserKnownHostsFile=/dev/null", "-o IdentityFile=/tmp/e2e-ssh-key"]
YAML
set +e
eneru config check --config /tmp/config-e2e-config-check-pinned.yaml >/tmp/test65p.log 2>&1
set -e
cat /tmp/test65p.log
grep -qF "Pinned OK: sudo allows 'shutdown -h now' without a password" /tmp/test65p.log || {
  echo "FAIL: arg-pinned sudoers rule not recognised"; exit 1; }
grep -qF "Pinned Wrong Args: sudo refuses it without a password" /tmp/test65p.log || {
  echo "FAIL: mismatching arguments not flagged"; exit 1; }
if docker exec eneru-e2e-ssh test -e /var/run/shutdown-triggered; then
  echo "FAIL: config check EXECUTED the pinned shutdown command"; exit 1
fi

# A wrong NUT device name is pinpointed with the names that do exist.
sed -i 's/TestUPS@localhost:3493/upsmon@localhost:3493/' /tmp/config-e2e-config-check.yaml
set +e
eneru config check --config /tmp/config-e2e-config-check.yaml >/tmp/test65b.log 2>&1
set -e
if ! grep -q "UPS 'upsmon' does not exist on localhost:3493 (available: .*TestUPS" /tmp/test65b.log; then
  cat /tmp/test65b.log
  echo "FAIL: wrong UPS name was not reported with the available names"; exit 1
fi

# The shipped E2E config is clean offline (static layer only).
eneru config check --offline --quiet --config "$E2E_DIR/config-e2e.yaml"
echo "PASS: config check probed NUT/SSH/sudo read-only and flagged real problems"
)

# ======================================================================
# Test 66: config editor TUI edits in place and creates new files
# ======================================================================
# Drives the curses editor through a pty. Editing must change only the
# touched value (operator comments survive, a .bak keeps the old file);
# creating must write a 0600 file with safe defaults and an explanation
# above every key, and the result must validate and pass config check.
(
echo ""
echo ">>> Running: Test 66: config editor TUI edits in place and creates new files"

cp "$E2E_DIR/config-e2e.yaml" /tmp/config-e2e-tui.yaml
# Stage 2 (Safety) -> Enter toggles dry_run -> Save (y confirms if asked) -> Quit
python3 "$E2E_DIR/config-tui-driver.py" '2|\r|S|y|q' -- \
  eneru config --basic --config /tmp/config-e2e-tui.yaml >/tmp/test66a.log
if ! grep -qE '^  dry_run: true +# Real execution for E2E tests' /tmp/config-e2e-tui.yaml; then
  grep -n dry_run /tmp/config-e2e-tui.yaml || true
  echo "FAIL: TUI did not toggle dry_run in place with its comment intact"; exit 1
fi
if [ "$(diff "$E2E_DIR/config-e2e.yaml" /tmp/config-e2e-tui.yaml | grep -c '^[<>]')" -ne 2 ]; then
  diff "$E2E_DIR/config-e2e.yaml" /tmp/config-e2e-tui.yaml || true
  echo "FAIL: TUI changed more than the one edited line"; exit 1
fi
cmp -s "$E2E_DIR/config-e2e.yaml" /tmp/config-e2e-tui.yaml.bak || {
  echo "FAIL: .bak does not hold the previous version"; exit 1; }
eneru validate --config /tmp/config-e2e-tui.yaml

rm -f /tmp/config-e2e-tui-new.yaml
# New file -> edit UPS name (Ctrl-U clears) -> Save -> Quit
python3 "$E2E_DIR/config-tui-driver.py" '\r|\x15TestUPS@localhost:3493\r|S|y|q' -- \
  eneru config --basic --config /tmp/config-e2e-tui-new.yaml >/tmp/test66b.log
test -f /tmp/config-e2e-tui-new.yaml || { echo "FAIL: new file not created"; exit 1; }
if [ "$(stat -c %a /tmp/config-e2e-tui-new.yaml)" != "600" ]; then
  echo "FAIL: new config must be mode 0600 (it can hold secrets)"; exit 1
fi
cat /tmp/config-e2e-tui-new.yaml
grep -q '^  name: TestUPS@localhost:3493' /tmp/config-e2e-tui-new.yaml
grep -q '^  dry_run: true' /tmp/config-e2e-tui-new.yaml
grep -q '^  # NUT identifier NAME@HOST' /tmp/config-e2e-tui-new.yaml
eneru validate --config /tmp/config-e2e-tui-new.yaml
set +e
eneru config check --config /tmp/config-e2e-tui-new.yaml >/tmp/test66c.log 2>&1
set -e
cat /tmp/test66c.log
grep -qF "NUT server localhost:3493 lists UPS 'TestUPS'" /tmp/test66c.log
echo "PASS: config editor edits in place and creates explained 0600 configs"
)

echo ""
echo "=== Group 'cli' completed successfully ==="
