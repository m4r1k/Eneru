#!/usr/bin/env bash
# E2E group: loopback
# Exercises v5.5 containerized local-host ownership through SSH loopback.

set -euo pipefail

: "${E2E_DIR:=tests/e2e}"
E2E_DIR="$(cd "$E2E_DIR" && pwd)"
export E2E_DIR

# Shared E2E helpers (apply_scenario: poll-until-applied scenario swaps).
. "$E2E_DIR/groups/lib.sh"

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
  # Mirror the shipped Docker/Podman layout: /srv/eneru/ssh is mounted at
  # /var/lib/eneru/ssh and is writable for known_hosts, while the private key
  # file itself stays 0400. With the built-in StrictHostKeyChecking=accept-new
  # default and no ssh_options, the daemon must learn the host key into
  # /var/lib/eneru/ssh/known_hosts.
  # sudo: a previous run may have left these owned by uid 10001 (ssh writes
  # ~/.ssh as root-of-the-container 0700), which the runner user cannot remove.
  sudo rm -rf /tmp/e2e-accept-new-key /tmp/e2e-accept-new-state
  install -m 0755 -d /tmp/e2e-accept-new-key /tmp/e2e-accept-new-state
  cp /tmp/e2e-ssh-key /tmp/e2e-accept-new-key/id_remote
  sudo chown -R 10001:10001 /tmp/e2e-accept-new-key /tmp/e2e-accept-new-state
  sudo chmod 0400 /tmp/e2e-accept-new-key/id_remote
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

  apply_scenario online-charging
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
  apply_scenario low-battery
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
  apply_scenario online-charging
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
  local state_dir="/tmp/e2e-accept-new-state"
  local key_dir="/tmp/e2e-accept-new-key"
  # The container default stores learned trust under /var/lib/eneru/ssh, which
  # maps to this host path.
  local kh="${key_dir}/known_hosts"
  # Start clean so the first run genuinely has no key to trust yet.
  sudo rm -f "$kh" /tmp/e2e-accept-new-known-hosts.first
  cat >"$config" <<'YAML'
ups:
  - name: "TestUPS@nut-server"
    display_name: "Accept-New (default) SSH Trust E2E UPS"
    is_local: false
    remote_servers:
      - name: accept-new-ssh-target
        enabled: true
        host: ssh-target
        user: root
        ssh_key_path: /var/lib/eneru/ssh/id_remote
        # No ssh_options: rely on the built-in StrictHostKeyChecking=accept-new
        # plus the container UserKnownHostsFile default.
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
    echo "  Starting accept-new (default) container (${attempt})"
    # The state volume backs /var/lib/eneru; the nested SSH directory is
    # writable for known_hosts, while id_remote itself is mode 0400.
    docker run -d --name "$name" \
      --network "$NETWORK" \
      -p "127.0.0.1:${port}:9191" \
      -v "$config":/etc/ups-monitor/config.yaml:ro \
      -v "${state_dir}":/var/lib/eneru:rw \
      -v /tmp/e2e-accept-new-key:/var/lib/eneru/ssh:rw \
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

    # The whole point: the built-in accept-new default must LEARN the host key
    # on the first probe and write it to /var/lib/eneru/ssh/known_hosts --
    # no ssh_options and no pre-seeding. The private key file stays 0400.
    if ! sudo test -s "$kh"; then
      echo "FAIL: accept-new default did not record /var/lib/eneru/ssh/known_hosts on the ${attempt} start"
      sudo ls -la "$key_dir" 2>/dev/null || true
      docker logs "$name" || true
      exit 1
    fi
    [ "$attempt" = "first" ] && sudo cp "$kh" /tmp/e2e-accept-new-known-hosts.first
    echo "  PASS: accept-new remote HEALTHY and host key recorded after ${attempt} start"
    docker rm -f "$name" >/dev/null 2>&1 || true
  done

  # The learned key must survive the recreate. Do not require byte-for-byte
  # equality: after a successful trusted connection OpenSSH may append extra
  # host-key material (for example via UpdateHostKeys). The invariant issue #73
  # needs is that the first run's trust anchors are still present after the
  # container was recreated.
  if ! sudo awk 'NR == FNR { seen[$0] = 1; next } { delete seen[$0] } END { for (line in seen) exit 1 }' \
      /tmp/e2e-accept-new-known-hosts.first "$kh"; then
    echo "FAIL: first-run known_hosts entries were not preserved across recreate"
    sudo diff -u /tmp/e2e-accept-new-known-hosts.first "$kh" || true
    exit 1
  fi
  echo "  PASS: learned host key persisted across container recreate"
}

delegated_completion_marker_case() {
  local config="/tmp/e2e-loopback-delivery.yaml"
  local state_dir="/tmp/e2e-loopback-delivery-state"
  local name="eneru-e2e-loopback-delivery"
  local log="/tmp/e2e-loopback-delivery.log"

  cat >"$config" <<'YAML'
ups:
  - name: "UPS1@nut-server"
    display_name: "Loopback delivery UPS"
    is_local: true
    check_interval: 1
    triggers:
      on_battery_stabilization_delay: 0
      low_battery_threshold: 95
      critical_runtime_threshold: 600
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
        host_identity_command: "echo e2e-loopback-delivery"
        expected_host_identity: "e2e-loopback-delivery"
        shutdown_command: "shutdown -h now"
  - name: "UPS2@nut-server"
    display_name: "Loopback healthy peer"
    is_local: false
    check_interval: 1
behavior:
  dry_run: false
local_shutdown:
  enabled: true
  trigger_on: any
logging:
  file: null
  state_file: "/var/lib/eneru/ups-monitor.state"
  battery_history_file: "/var/lib/eneru/ups-battery-history"
  shutdown_flag_file: "/var/lib/eneru/ups-shutdown-scheduled"
statistics:
  db_directory: "/var/lib/eneru"
YAML

  sudo rm -rf "$state_dir"
  install -d -m 0755 "$state_dir"
  sudo chown 10001:10001 "$state_dir"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker compose -f "$E2E_DIR/docker-compose.yml" exec -T ssh-target \
    rm -f /var/run/shutdown-triggered
  apply_scenario online-charging UPS1
  apply_scenario online-charging UPS2
  apply_scenario low-battery UPS1

  set +e
  timeout 60s docker run --name "$name" \
    --network "$NETWORK" \
    -v "$config":/etc/ups-monitor/config.yaml:ro \
    -v /tmp/e2e-loopback-key:/var/lib/eneru/ssh/id_loopback:ro \
    -v "$state_dir":/var/lib/eneru \
    eneru:e2e \
    run --config /etc/ups-monitor/config.yaml --exit-after-shutdown \
    2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  set -e
  docker rm -f "$name" >/dev/null 2>&1 || true

  if [ "$rc" -ne 0 ]; then
    echo "FAIL: delegated completion case exited with rc=$rc"
    cat "$log"
    exit 1
  fi
  if ! docker compose -f "$E2E_DIR/docker-compose.yml" exec -T ssh-target \
      test -f /var/run/shutdown-triggered; then
    echo "FAIL: loopback host poweroff command was not delivered"
    cat "$log"
    exit 1
  fi
  if ! grep -q "Skipping in-container poweroff" "$log"; then
    echo "FAIL: coordinator did not take the delegated poweroff skip path"
    cat "$log"
    exit 1
  fi
  if grep -qi "sequence is incomplete" "$log"; then
    echo "FAIL: successful loopback delivery was classified incomplete"
    cat "$log"
    exit 1
  fi
  if ! sudo grep -q '"reason": "sequence_complete"' \
      "$state_dir/.shutdown_state.json"; then
    echo "FAIL: successful delegated poweroff did not persist completion marker"
    sudo find "$state_dir" -maxdepth 1 -type f -ls || true
    cat "$log"
    exit 1
  fi
  apply_scenario online-charging UPS1
  apply_scenario online-charging UPS2
  echo "  PASS: delivered loopback poweroff skipped container halt and wrote completion marker"
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
echo ">>> Prepared accept-new remote SSH key (read-only) + writable state dir at /tmp/e2e-accept-new-{key,state}"

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
echo ">>> Running: Test 61: E2E delegated poweroff delivery writes completion marker"
delegated_completion_marker_case

echo "PASS: E2E loopback root/sudo, delivery marker, accept-new SSH trust, and negative readiness checks passed"
