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
  docker network ls --format '{{.Name}}' | awk '/_eneru-e2e$/ { print; exit }'
}

prepare_loopback_key() {
  cp /tmp/e2e-ssh-key /tmp/e2e-loopback-key
  sudo chown 10001:10001 /tmp/e2e-loopback-key
  sudo chmod 0400 /tmp/e2e-loopback-key
}

prepare_accept_new_ssh_material() {
  # Writable SSH dir holding only the private key. known_hosts is left
  # absent on purpose: with StrictHostKeyChecking=accept-new the daemon must
  # learn the host key on its first probe and write it here, so the directory
  # has to be owned by and writable for uid 10001 (the container user).
  rm -rf /tmp/e2e-accept-new-ssh
  install -m 0755 -d /tmp/e2e-accept-new-ssh
  cp /tmp/e2e-ssh-key /tmp/e2e-accept-new-ssh/id_remote
  sudo chown -R 10001:10001 /tmp/e2e-accept-new-ssh
  sudo chmod 0755 /tmp/e2e-accept-new-ssh
  sudo chmod 0400 /tmp/e2e-accept-new-ssh/id_remote
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
    virtual_machines:
      enabled: true
      max_wait: 2
    containers:
      enabled: true
      runtime: docker
      stop_timeout: 2
      shutdown_all_remaining_containers: true
      include_user_containers: true
      compose_files:
        - path: "/opt/e2e/docker-compose.yml"
          stop_timeout: 2
    filesystems:
      sync_enabled: true
      unmount:
        enabled: true
        timeout: 2
        mounts:
          - path: "/mnt/e2e-loopback"
            options: "-l"
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

assert_loopback_config_shape() {
  local config="$1"
  local key
  for key in virtual_machines containers filesystems; do
    if grep -Eq "^${key}:" "$config"; then
      echo "FAIL: loopback E2E config has top-level '${key}', expected it under the local ups entry"
      cat "$config"
      exit 1
    fi
    if ! grep -Eq "^[[:space:]]{4}${key}:" "$config"; then
      echo "FAIL: loopback E2E config is missing nested '${key}' under the local ups entry"
      cat "$config"
      exit 1
    fi
  done
}

run_loopback_case() {
  local label="$1" user="$2" use_sudo="$3" port="$4"
  local config="/tmp/e2e-loopback-${label}.yaml"
  local name="eneru-e2e-loopback-${label}"
  local identity="e2e-loopback-${label}"
  local cid=""

  write_loopback_config "$config" "$user" "$use_sudo" "$identity"
  assert_loopback_config_shape "$config"

  cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply.dev"
  docker rm -f "$name" >/dev/null 2>&1 || true
  echo "  Starting Eneru container '${name}' as SSH user '${user}' (use_sudo=${use_sudo})"
  cid=$(docker run -d --name "$name" \
    --network "$NETWORK" \
    -p "127.0.0.1:${port}:9191" \
    -v "$config":/etc/ups-monitor/config.yaml:ro \
    -v /tmp/e2e-loopback-key:/var/lib/eneru/ssh/id_loopback:ro \
    eneru:e2e \
    run --config /etc/ups-monitor/config.yaml \
    --api --api-bind 0.0.0.0 --api-port 9191 \
    --exit-after-shutdown)
  echo "  Container started: ${name} (${cid:0:12}); waiting for /ready on 127.0.0.1:${port}"

  if ! poll_http_pattern \
      "http://127.0.0.1:${port}/ready" \
      "/tmp/e2e-loopback-${label}-ready.json" \
      '"ready"[[:space:]]*:[[:space:]]*true'; then
    echo "FAIL: loopback ${label} /ready was not green"
    cat "/tmp/e2e-loopback-${label}-ready.json"
    docker logs "$name" || true
    exit 1
  fi
  echo "  PASS: /ready is green for ${label} loopback"
  echo "  /ready payload:"
  cat "/tmp/e2e-loopback-${label}-ready.json"
  echo

  echo "  Applying low-battery scenario and waiting for delegated dry-run shutdown"
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
  echo "  PASS: delegated shutdown command observed: ${expected}"
  echo "  Matching shutdown log line:"
  grep "$expected" "/tmp/e2e-loopback-${label}.log" | tail -1
  for action in \
    stop_vms \
    stop_compose \
    stop_containers \
    stop_containers_rootless \
    sync \
    unmount_filesystems
  do
    if ! grep -q "$action" "/tmp/e2e-loopback-${label}.log"; then
      echo "FAIL: loopback ${label} did not dry-run delegated action '${action}'"
      cat "/tmp/e2e-loopback-${label}.log"
      exit 1
    fi
  done
  echo "  PASS: delegated local action list observed for ${label}"

  docker rm -f "$name" >/dev/null 2>&1 || true
  cp "$E2E_DIR/scenarios/online-charging.dev" "$E2E_DIR/scenarios/apply.dev"
  echo "  PASS: ${label} loopback case complete"
}

negative_missing_machine_id() {
  local config="/tmp/e2e-loopback-missing-machine-id.yaml"
  local name="eneru-e2e-loopback-missing-machine-id"
  local cid=""
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
  echo "  Starting container with an intentionally empty /etc/machine-id bind mount"
  cid=$(docker run -d --name "$name" \
    --network "$NETWORK" \
    -p 127.0.0.1:19193:9191 \
    -v "$config":/etc/ups-monitor/config.yaml:ro \
    -v /tmp/e2e-loopback-key:/var/lib/eneru/ssh/id_loopback:ro \
    -v /tmp/e2e-empty-machine-id:/etc/machine-id:ro \
    eneru:e2e \
    run --config /etc/ups-monitor/config.yaml \
    --api --api-bind 0.0.0.0 --api-port 9191)
  echo "  Container started: ${name} (${cid:0:12}); waiting for /ready failure diagnostic"

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
  echo "  PASS: /ready is false and includes systemd-machine-id-setup hint"
  echo "  /ready payload:"
  cat /tmp/e2e-loopback-missing-machine-id-ready.json
  echo
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
  echo "  Starting container with local capabilities and explicit is_host_loopback: false"
  # Run detached + bounded wait so CI doesn't hang if the daemon ever
  # starts successfully (the failure mode for this negative test).
  docker run -d --name "$name" \
    --network "$NETWORK" \
    -v "$config":/etc/ups-monitor/config.yaml:ro \
    eneru:e2e \
    run --config /etc/ups-monitor/config.yaml >/dev/null
  sleep 3
  if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
    echo "FAIL: missing loopback config started successfully and is still running"
    docker logs "$name" >/tmp/e2e-loopback-missing-loopback.log 2>&1 || true
    cat /tmp/e2e-loopback-missing-loopback.log
    docker rm -f "$name" >/dev/null 2>&1 || true
    exit 1
  fi
  docker logs "$name" >/tmp/e2e-loopback-missing-loopback.log 2>&1 || true
  if ! grep -q "no enabled is_host_loopback delegate" /tmp/e2e-loopback-missing-loopback.log; then
    echo "FAIL: missing loopback error did not explain the contract"
    cat /tmp/e2e-loopback-missing-loopback.log
    docker rm -f "$name" >/dev/null 2>&1 || true
    exit 1
  fi
  echo "  PASS: startup failed with missing enabled loopback delegate diagnostic"
  docker rm -f "$name" >/dev/null 2>&1 || true
}

accept_new_known_hosts_remote() {
  local config="/tmp/e2e-accept-new.yaml"
  local name="eneru-e2e-accept-new"
  local port="$1"
  local ssh_dir="/tmp/e2e-accept-new-ssh"
  local kh="${ssh_dir}/known_hosts"
  # Start clean so the first run genuinely has no key to trust yet.
  rm -f "$kh"
  cat >"$config" <<'YAML'
ups:
  - name: "TestUPS@nut-server"
    display_name: "Accept-New SSH Trust E2E UPS"
    is_local: false
    remote_servers:
      - name: accept-new-ssh-target
        enabled: true
        host: ssh-target
        user: root
        ssh_key_path: /var/lib/eneru/ssh/id_remote
        ssh_options:
          - "UserKnownHostsFile=/var/lib/eneru/ssh/known_hosts"
          - "StrictHostKeyChecking=accept-new"
        shutdown_command: "shutdown -h now"
local_shutdown:
  enabled: false
  trigger_on: none
remote_health:
  enabled: true
  startup_check: true
  interval: 60   # minimum; the 3600 default would outlast the HEALTHY poll below
  failure_threshold: 1
logging:
  file: null
  state_file: "/var/run/eneru/ups-monitor.state"
  battery_history_file: "/var/run/eneru/ups-battery-history"
  shutdown_flag_file: "/var/run/eneru/ups-shutdown-scheduled"
statistics:
  db_directory: "/var/lib/eneru"
YAML

  for attempt in first recreated; do
    docker rm -f "$name" >/dev/null 2>&1 || true
    if [ "$attempt" = "first" ] && [ -e "$kh" ]; then
      echo "FAIL: known_hosts must not exist before the first accept-new start"
      exit 1
    fi
    echo "  Starting accept-new container (${attempt})"
    # Mount the SSH dir read-write so accept-new can record the learned key.
    docker run -d --name "$name" \
      --network "$NETWORK" \
      -p "127.0.0.1:${port}:9191" \
      -v "$config":/etc/ups-monitor/config.yaml:ro \
      -v "${ssh_dir}":/var/lib/eneru/ssh:rw \
      eneru:e2e \
      run --config /etc/ups-monitor/config.yaml \
      --api --api-bind 0.0.0.0 --api-port 9191 >/dev/null

    if ! poll_http_pattern \
        "http://127.0.0.1:${port}/ready" \
        "/tmp/e2e-accept-new-${attempt}.json" \
        '"ready"[[:space:]]*:[[:space:]]*true'; then
      echo "FAIL: accept-new remote was not ready after ${attempt} start"
      cat "/tmp/e2e-accept-new-${attempt}.json" || true
      docker logs "$name" || true
      exit 1
    fi
    echo "  PASS: accept-new remote ready after ${attempt} start"

    # Readiness alone can flip true on the NUT poll while the SSH probe is
    # still UNKNOWN (background thread; readiness treats UNKNOWN as
    # achievable). Wait for the remote target to actually reach HEALTHY so
    # this proves the SSH probe succeeded. collect_status serializes with
    # sort_keys=True, so within each remoteHealth entry "server" sits
    # immediately before "status". Poll up to 75s (150 x 0.5s): the startup
    # probe is normally HEALTHY within seconds, and remote_health.interval is
    # pinned to 60 so a missed first probe still retries inside the window.
    # A match returns immediately, so this costs nothing on the happy path.
    if ! poll_http_pattern \
        "http://127.0.0.1:${port}/api/v1/ups" \
        "/tmp/e2e-accept-new-${attempt}-health.json" \
        '"server"[[:space:]]*:[[:space:]]*"accept-new-ssh-target"[[:space:]]*,[[:space:]]*"status"[[:space:]]*:[[:space:]]*"HEALTHY"' \
        150; then
      echo "FAIL: accept-new remote SSH probe never reached HEALTHY after ${attempt} start"
      cat "/tmp/e2e-accept-new-${attempt}-health.json" || true
      docker logs "$name" || true
      exit 1
    fi

    # The whole point of accept-new: SSH must have LEARNED the host key on
    # the first probe and written it to the writable mount -- no ssh-keyscan,
    # no pre-seeding.
    if [ ! -s "$kh" ]; then
      echo "FAIL: accept-new did not record known_hosts on the ${attempt} start"
      ls -la "$ssh_dir" || true
      docker logs "$name" || true
      exit 1
    fi
    [ "$attempt" = "first" ] && cp "$kh" /tmp/e2e-accept-new-known-hosts.first
    echo "  PASS: accept-new remote HEALTHY and host key recorded after ${attempt} start"
    docker rm -f "$name" >/dev/null 2>&1 || true
  done

  # The learned key must survive the recreate unchanged: the second start
  # reused the persisted file instead of re-learning. That cross-recreate
  # persistence is exactly what issue #73 was missing.
  if ! cmp -s /tmp/e2e-accept-new-known-hosts.first "$kh"; then
    echo "FAIL: known_hosts changed across recreate (host key was not persisted/reused)"
    exit 1
  fi
  echo "  PASS: learned host key persisted unchanged across container recreate"
}

echo ">>> Building Eneru OCI image for loopback E2E"
docker build -t eneru:e2e .
NETWORK="$(network_name)"
if [ -z "$NETWORK" ]; then
  echo "FAIL: E2E Docker network not found"
  docker network ls
  exit 1
fi
echo ">>> Using Docker network: ${NETWORK}"
prepare_loopback_key
echo ">>> Prepared loopback private key at /tmp/e2e-loopback-key"
prepare_accept_new_ssh_material
echo ">>> Prepared accept-new remote SSH key (writable dir, no pre-seeded known_hosts) at /tmp/e2e-accept-new-ssh"

echo ">>> Running: Test 47: E2E Loopback Root"
run_loopback_case root root false 19191
echo ">>> Running: Test 48: E2E Loopback Sudo"
run_loopback_case sudo testuser true 19192
echo ">>> Running: Test 49: E2E Loopback missing machine-id readiness"
negative_missing_machine_id
echo ">>> Running: Test 50: E2E Loopback missing delegate startup failure"
negative_missing_loopback
echo ">>> Running: Test 57: E2E container remote accept-new learns host key and survives recreate"
accept_new_known_hosts_remote 19194

echo "PASS: E2E loopback root/sudo, accept-new SSH trust, and negative readiness checks passed"
