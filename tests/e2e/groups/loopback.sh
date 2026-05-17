#!/usr/bin/env bash
# E2E group: loopback
# Exercises v5.5 containerized local-host ownership through SSH loopback.

set -euo pipefail

: "${E2E_DIR:=tests/e2e}"
E2E_DIR="$(cd "$E2E_DIR" && pwd)"
export E2E_DIR

ROOT_DIR="$(cd "$E2E_DIR/../.." && pwd)"
cd "$ROOT_DIR"

poll_http() {
  local url="$1" out="$2" tries="${3:-60}"
  for _ in $(seq 1 "$tries"); do
    if curl -sS "$url" >"$out" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

poll_http_pattern() {
  local url="$1" out="$2" pattern="$3" tries="${4:-80}"
  for _ in $(seq 1 "$tries"); do
    curl -sS "$url" >"$out" 2>/dev/null || true
    if grep -Eq "$pattern" "$out" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

poll_log() {
  local container="$1" pattern="$2" out="$3" tries="${4:-80}"
  for _ in $(seq 1 "$tries"); do
    docker logs "$container" >"$out" 2>&1 || true
    if grep -q "$pattern" "$out"; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

network_name() {
  docker network ls --format '{{.Name}}' | grep '_eneru-e2e$' | head -1
}

prepare_loopback_key() {
  cp /tmp/e2e-ssh-key /tmp/e2e-loopback-key
  chmod 0444 /tmp/e2e-loopback-key
}

write_loopback_config() {
  local path="$1" user="$2" use_sudo="$3" identity="$4"
  cat >"$path" <<YAML
ups:
  - name: "TestUPS@nut-server"
    display_name: "Loopback E2E UPS"
    is_local: true
    triggers:
      on_battery_stabilization_delay: 0
      low_battery_threshold: 95
      critical_runtime_threshold: 600
    remote_servers:
      - name: host-loopback
        enabled: true
        host: ssh-target
        user: "$user"
        ssh_key_path: /var/lib/eneru/ssh/id_loopback
        ssh_options:
          - "StrictHostKeyChecking=no"
          - "UserKnownHostsFile=/dev/null"
        is_host_loopback: true
        use_sudo: $use_sudo
        host_identity_command: "echo $identity"
        expected_host_identity: "$identity"
        shutdown_command: "shutdown -h now"
        shutdown_order: 999
behavior:
  dry_run: true
local_shutdown:
  enabled: true
  trigger_on: any
remote_health:
  enabled: true
  startup_check: true
  failure_threshold: 1
logging:
  file: null
  state_file: "/var/run/eneru/ups-monitor.state"
  battery_history_file: "/var/run/eneru/ups-battery-history"
  shutdown_flag_file: "/var/run/eneru/ups-shutdown-scheduled"
statistics:
  db_directory: "/var/lib/eneru"
YAML
}

run_loopback_case() {
  local label="$1" user="$2" use_sudo="$3" port="$4"
  local config="/tmp/e2e-loopback-${label}.yaml"
  local name="eneru-e2e-loopback-${label}"
  local identity="e2e-loopback-${label}"

  write_loopback_config "$config" "$user" "$use_sudo" "$identity"

  cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply.dev"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    --network "$NETWORK" \
    -p "127.0.0.1:${port}:9191" \
    -v "$config":/etc/ups-monitor/config.yaml:ro \
    -v /tmp/e2e-loopback-key:/var/lib/eneru/ssh/id_loopback:ro \
    eneru:e2e \
    run --config /etc/ups-monitor/config.yaml \
    --api --api-bind 0.0.0.0 --api-port 9191 \
    --exit-after-shutdown

  if ! poll_http_pattern \
      "http://127.0.0.1:${port}/ready" \
      "/tmp/e2e-loopback-${label}-ready.json" \
      '"ready"[[:space:]]*:[[:space:]]*true'; then
    echo "FAIL: loopback ${label} /ready was not green"
    cat "/tmp/e2e-loopback-${label}-ready.json"
    docker logs "$name" || true
    exit 1
  fi

  cp "$E2E_DIR/scenarios/low-battery.dev" "$E2E_DIR/scenarios/apply.dev"
  if [ "$use_sudo" = "true" ]; then
    expected="Would send command 'sudo -n shutdown -h now'"
  else
    expected="Would send command 'shutdown -h now'"
  fi
  if ! poll_log "$name" "$expected" "/tmp/e2e-loopback-${label}.log"; then
    echo "FAIL: loopback ${label} dry-run shutdown did not delegate expected command"
    cat "/tmp/e2e-loopback-${label}.log"
    exit 1
  fi

  docker rm -f "$name" >/dev/null 2>&1 || true
  cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply.dev"
}

negative_missing_machine_id() {
  local config="/tmp/e2e-loopback-missing-machine-id.yaml"
  local name="eneru-e2e-loopback-missing-machine-id"
  cat >"$config" <<'YAML'
ups:
  - name: "TestUPS@nut-server"
    is_local: true
    remote_servers:
      - name: host-loopback
        enabled: true
        host: ssh-target
        user: root
        ssh_key_path: /var/lib/eneru/ssh/id_loopback
        ssh_options:
          - "StrictHostKeyChecking=no"
          - "UserKnownHostsFile=/dev/null"
        is_host_loopback: true
        shutdown_command: "shutdown -h now"
        shutdown_order: 999
behavior:
  dry_run: true
local_shutdown:
  enabled: true
remote_health:
  enabled: true
  startup_check: true
  failure_threshold: 1
logging:
  file: null
  state_file: "/var/run/eneru/ups-monitor.state"
  battery_history_file: "/var/run/eneru/ups-battery-history"
  shutdown_flag_file: "/var/run/eneru/ups-shutdown-scheduled"
YAML
  : >/tmp/e2e-empty-machine-id
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    --network "$NETWORK" \
    -p 127.0.0.1:19193:9191 \
    -v "$config":/etc/ups-monitor/config.yaml:ro \
    -v /tmp/e2e-loopback-key:/var/lib/eneru/ssh/id_loopback:ro \
    -v /tmp/e2e-empty-machine-id:/etc/machine-id:ro \
    eneru:e2e \
    run --config /etc/ups-monitor/config.yaml \
    --api --api-bind 0.0.0.0 --api-port 9191

  if ! poll_http_pattern \
      http://127.0.0.1:19193/ready \
      /tmp/e2e-loopback-missing-machine-id-ready.json \
      'systemd-machine-id-setup'; then
    echo "FAIL: missing machine-id readiness did not include setup hint"
    cat /tmp/e2e-loopback-missing-machine-id-ready.json || true
    docker logs "$name" || true
    exit 1
  fi
  if ! grep -Eq '"ready"[[:space:]]*:[[:space:]]*false' /tmp/e2e-loopback-missing-machine-id-ready.json; then
    echo "FAIL: missing machine-id did not make /ready false"
    cat /tmp/e2e-loopback-missing-machine-id-ready.json || true
    docker logs "$name" || true
    exit 1
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
}

negative_missing_loopback() {
  local config="/tmp/e2e-loopback-missing-loopback.yaml"
  local name="eneru-e2e-loopback-missing-loopback"
  cat >"$config" <<'YAML'
ups:
  - name: "TestUPS@nut-server"
    is_local: true
    remote_servers:
      - name: explicit-not-loopback
        enabled: true
        host: ssh-target
        user: root
        is_host_loopback: false
behavior:
  dry_run: true
local_shutdown:
  enabled: true
logging:
  file: null
YAML
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run --name "$name" \
    --network "$NETWORK" \
    -v "$config":/etc/ups-monitor/config.yaml:ro \
    eneru:e2e \
    run --config /etc/ups-monitor/config.yaml \
    >/tmp/e2e-loopback-missing-loopback.log 2>&1 && {
      echo "FAIL: missing loopback config started successfully"
      cat /tmp/e2e-loopback-missing-loopback.log
      exit 1
    }
  if ! grep -q "no enabled is_host_loopback delegate" /tmp/e2e-loopback-missing-loopback.log; then
    echo "FAIL: missing loopback error did not explain the contract"
    cat /tmp/e2e-loopback-missing-loopback.log
    exit 1
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
}

docker build -t eneru:e2e .
NETWORK="$(network_name)"
if [ -z "$NETWORK" ]; then
  echo "FAIL: E2E Docker network not found"
  docker network ls
  exit 1
fi
prepare_loopback_key

echo ">>> Running: Test 47: E2E Loopback Root"
run_loopback_case root root false 19191
echo ">>> Running: Test 48: E2E Loopback Sudo"
run_loopback_case sudo testuser true 19192
echo ">>> Running: Test 49: E2E Loopback missing machine-id readiness"
negative_missing_machine_id
echo ">>> Running: Test 50: E2E Loopback missing delegate startup failure"
negative_missing_loopback

echo "PASS: E2E loopback root/sudo and negative readiness checks passed"
