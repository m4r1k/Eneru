"""Tests for remote-server config: server lists, pre-shutdown commands, ordering, parallel batching, safety margin."""

import pytest
import yaml
from pathlib import Path

from eneru import (
    Config,
    ConfigLoader,
    UPSConfig,
    TriggersConfig,
    NotificationsConfig,
    ContainersConfig,
    ComposeFileConfig,
    RemoteServerConfig,
    RemoteCommandConfig,
)
from test_constants import (
    TEST_DISCORD_WEBHOOK_ID,
    TEST_DISCORD_WEBHOOK_TOKEN,
    TEST_DISCORD_APPRISE_URL,
    TEST_DISCORD_WEBHOOK_URL,
    TEST_SLACK_APPRISE_URL,
)


class TestRemoteServersConfig:
    """Test remote servers configuration parsing."""

    @pytest.mark.unit
    def test_multiple_remote_servers(self, temp_config_file):
        """Test multiple remote server configurations."""
        config_data = """
remote_servers:
  - name: "NAS 1"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "sudo shutdown -h now"
  - name: "NAS 2"
    enabled: false
    host: "192.168.1.51"
    user: "root"
    connect_timeout: 15
    command_timeout: 45
    shutdown_command: "poweroff"
    ssh_options:
      - "-o StrictHostKeyChecking=no"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.remote_servers) == 2

        server1 = config.remote_servers[0]
        assert server1.name == "NAS 1"
        assert server1.enabled is True
        assert server1.host == "192.168.1.50"
        assert server1.user == "admin"
        assert server1.shutdown_command == "sudo shutdown -h now"
        assert server1.connect_timeout == 10  # default
        assert server1.command_timeout == 30  # default

        server2 = config.remote_servers[1]
        assert server2.name == "NAS 2"
        assert server2.enabled is False
        assert server2.host == "192.168.1.51"
        assert server2.user == "root"
        assert server2.connect_timeout == 15
        assert server2.command_timeout == 45
        assert server2.shutdown_command == "poweroff"
        assert "-o StrictHostKeyChecking=no" in server2.ssh_options

    @pytest.mark.unit
    def test_remote_server_ssh_key_path(self, temp_config_file):
        """ssh_key_path is parsed without changing ssh_options behavior."""
        config_data = """
remote_servers:
  - name: "NAS"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    ssh_key_path: "/var/lib/eneru/ssh/id_ups_shutdown"
    ssh_options:
      - "StrictHostKeyChecking=yes"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.ssh_key_path == "/var/lib/eneru/ssh/id_ups_shutdown"
        assert server.ssh_options == ["StrictHostKeyChecking=yes"]

    @pytest.mark.unit
    def test_pre_shutdown_commands_with_actions(self, temp_config_file):
        """Test pre_shutdown_commands with predefined actions."""
        config_data = """
remote_servers:
  - name: "Proxmox Host"
    enabled: true
    host: "192.168.1.60"
    user: "root"
    pre_shutdown_commands:
      - action: "stop_proxmox_vms"
        timeout: 120
      - action: "stop_proxmox_cts"
        timeout: 60
      - action: "sync"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.remote_servers) == 1
        server = config.remote_servers[0]
        assert len(server.pre_shutdown_commands) == 3

        cmd1 = server.pre_shutdown_commands[0]
        assert cmd1.action == "stop_proxmox_vms"
        assert cmd1.timeout == 120
        assert cmd1.command is None

        cmd2 = server.pre_shutdown_commands[1]
        assert cmd2.action == "stop_proxmox_cts"
        assert cmd2.timeout == 60

        cmd3 = server.pre_shutdown_commands[2]
        assert cmd3.action == "sync"
        assert cmd3.timeout is None  # Uses server default

    @pytest.mark.unit
    def test_pre_shutdown_commands_with_custom_command(self, temp_config_file):
        """Test pre_shutdown_commands with custom commands."""
        config_data = """
remote_servers:
  - name: "Docker Server"
    enabled: true
    host: "192.168.1.70"
    user: "root"
    pre_shutdown_commands:
      - command: "systemctl stop my-service"
        timeout: 30
      - command: "docker stop $(docker ps -q)"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert len(server.pre_shutdown_commands) == 2

        cmd1 = server.pre_shutdown_commands[0]
        assert cmd1.command == "systemctl stop my-service"
        assert cmd1.timeout == 30
        assert cmd1.action is None

        cmd2 = server.pre_shutdown_commands[1]
        assert cmd2.command == "docker stop $(docker ps -q)"
        assert cmd2.timeout is None

    @pytest.mark.unit
    def test_pre_shutdown_commands_with_compose_path(self, temp_config_file):
        """Test pre_shutdown_commands with stop_compose action and path."""
        config_data = """
remote_servers:
  - name: "Docker Server"
    enabled: true
    host: "192.168.1.70"
    user: "root"
    pre_shutdown_commands:
      - action: "stop_compose"
        path: "/opt/myapp/docker-compose.yml"
        timeout: 120
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert len(server.pre_shutdown_commands) == 1

        cmd = server.pre_shutdown_commands[0]
        assert cmd.action == "stop_compose"
        assert cmd.path == "/opt/myapp/docker-compose.yml"
        assert cmd.timeout == 120

    @pytest.mark.unit
    def test_pre_shutdown_commands_mixed(self, temp_config_file):
        """Test pre_shutdown_commands with mixed actions and commands."""
        config_data = """
remote_servers:
  - name: "Mixed Server"
    enabled: true
    host: "192.168.1.80"
    user: "root"
    command_timeout: 45
    pre_shutdown_commands:
      - action: "stop_containers"
        timeout: 90
      - command: "systemctl stop nginx"
        timeout: 15
      - action: "sync"
    shutdown_command: "poweroff"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.command_timeout == 45
        assert len(server.pre_shutdown_commands) == 3

        # Action with custom timeout
        assert server.pre_shutdown_commands[0].action == "stop_containers"
        assert server.pre_shutdown_commands[0].timeout == 90

        # Custom command
        assert server.pre_shutdown_commands[1].command == "systemctl stop nginx"
        assert server.pre_shutdown_commands[1].timeout == 15

        # Action without timeout (uses server default)
        assert server.pre_shutdown_commands[2].action == "sync"
        assert server.pre_shutdown_commands[2].timeout is None

    @pytest.mark.unit
    def test_pre_shutdown_commands_empty(self, temp_config_file):
        """Test server with empty pre_shutdown_commands."""
        config_data = """
remote_servers:
  - name: "Simple Server"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    pre_shutdown_commands: []
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.pre_shutdown_commands == []

    @pytest.mark.unit
    def test_pre_shutdown_commands_not_specified(self, temp_config_file):
        """Test server without pre_shutdown_commands field (backward compatible)."""
        config_data = """
remote_servers:
  - name: "Legacy Server"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.pre_shutdown_commands == []

    @pytest.mark.unit
    def test_parallel_option_default_none(self, temp_config_file):
        """Test that parallel defaults to None (unset) when not specified.

        Absent ``parallel`` is treated as the default parallel batch by
        ``compute_effective_order``; the None state is preserved so the
        validator can detect mutual-exclusion conflicts with shutdown_order.
        """
        config_data = """
remote_servers:
  - name: "Server Without Parallel"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.parallel is None

    @pytest.mark.unit
    def test_parallel_option_explicit_false(self, temp_config_file):
        """Test setting parallel to False."""
        config_data = """
remote_servers:
  - name: "Sequential Server"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    parallel: false
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.parallel is False

    @pytest.mark.unit
    def test_parallel_option_mixed(self, temp_config_file):
        """Test mixed parallel and sequential servers."""
        config_data = """
remote_servers:
  - name: "Parallel Server 1"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "shutdown -h now"
  - name: "Sequential Server"
    enabled: true
    host: "192.168.1.51"
    user: "admin"
    parallel: false
    shutdown_command: "shutdown -h now"
  - name: "Parallel Server 2"
    enabled: true
    host: "192.168.1.52"
    user: "admin"
    parallel: true
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.remote_servers) == 3
        assert config.remote_servers[0].parallel is None  # field omitted
        assert config.remote_servers[1].parallel is False
        assert config.remote_servers[2].parallel is True


class TestShutdownOrderConfig:
    """Test shutdown_order configuration parsing and validation."""

    @pytest.mark.unit
    def test_shutdown_order_default_none(self, temp_config_file):
        """Test that shutdown_order defaults to None when not specified."""
        config_data = """
remote_servers:
  - name: "Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.remote_servers[0].shutdown_order is None

    @pytest.mark.unit
    def test_shutdown_order_positive_integer(self, temp_config_file):
        """Test parsing a positive shutdown_order value."""
        config_data = """
remote_servers:
  - name: "Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 2
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.remote_servers[0].shutdown_order == 2

    @pytest.mark.unit
    def test_shutdown_order_multiple_servers(self, temp_config_file):
        """Test multiple servers with different shutdown_order values."""
        config_data = """
remote_servers:
  - name: "App Server 1"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 1
    shutdown_command: "shutdown -h now"
  - name: "Storage Server"
    enabled: true
    host: "192.168.1.11"
    user: "root"
    shutdown_order: 2
    shutdown_command: "shutdown -h now"
  - name: "App Server 2"
    enabled: true
    host: "192.168.1.12"
    user: "root"
    shutdown_order: 1
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.remote_servers[0].shutdown_order == 1
        assert config.remote_servers[1].shutdown_order == 2
        assert config.remote_servers[2].shutdown_order == 1

    @pytest.mark.unit
    def test_shutdown_order_mixed_with_parallel_flag(self, temp_config_file):
        """Test that shutdown_order and parallel are parsed independently."""
        config_data = """
remote_servers:
  - name: "With Order"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 3
    shutdown_command: "shutdown -h now"
  - name: "Legacy Sequential"
    enabled: true
    host: "192.168.1.11"
    user: "root"
    parallel: false
    shutdown_command: "shutdown -h now"
  - name: "Legacy Parallel"
    enabled: true
    host: "192.168.1.12"
    user: "root"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.remote_servers[0].shutdown_order == 3
        assert config.remote_servers[0].parallel is None  # parallel not in YAML
        assert config.remote_servers[1].shutdown_order is None
        assert config.remote_servers[1].parallel is False
        assert config.remote_servers[2].shutdown_order is None
        assert config.remote_servers[2].parallel is None  # parallel not in YAML

    @pytest.mark.unit
    def test_shutdown_order_with_gaps(self, temp_config_file):
        """Test that gaps in shutdown_order values are accepted."""
        config_data = """
remote_servers:
  - name: "Server A"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 10
    shutdown_command: "shutdown -h now"
  - name: "Server B"
    enabled: true
    host: "192.168.1.11"
    user: "root"
    shutdown_order: 30
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.remote_servers[0].shutdown_order == 10
        assert config.remote_servers[1].shutdown_order == 30

    @pytest.mark.unit
    def test_validation_error_shutdown_order_with_parallel_false(self, temp_config_file):
        """Test that validation errors when shutdown_order and parallel: false are both set."""
        config_data = """
remote_servers:
  - name: "Conflicting Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 2
    parallel: false
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [
            m for m in messages
            if "ERROR" in m and "cannot set both" in m
        ]
        assert len(error_msgs) == 1
        assert "Conflicting Server" in error_msgs[0]
        assert "shutdown_order" in error_msgs[0]
        assert "parallel" in error_msgs[0]

    @pytest.mark.unit
    def test_validation_error_shutdown_order_with_parallel_true(self, temp_config_file):
        """Test that validation errors when shutdown_order and parallel: true are both set."""
        config_data = """
remote_servers:
  - name: "Conflicting Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 2
    parallel: true
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [
            m for m in messages
            if "ERROR" in m and "cannot set both" in m
        ]
        assert len(error_msgs) == 1
        assert "Conflicting Server" in error_msgs[0]

    @pytest.mark.unit
    def test_validation_error_shutdown_order_zero(self, temp_config_file):
        """Test that validation rejects shutdown_order: 0."""
        config_data = """
remote_servers:
  - name: "Bad Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 0
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [m for m in messages if "ERROR" in m and "shutdown_order" in m]
        assert len(error_msgs) == 1
        assert ">= 1" in error_msgs[0]

    @pytest.mark.unit
    def test_validation_error_shutdown_order_negative(self, temp_config_file):
        """Test that validation rejects negative shutdown_order."""
        config_data = """
remote_servers:
  - name: "Bad Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: -1
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [m for m in messages if "ERROR" in m and "shutdown_order" in m]
        assert len(error_msgs) == 1
        assert ">= 1" in error_msgs[0]

    @pytest.mark.unit
    def test_validation_no_message_shutdown_order_alone(self, temp_config_file):
        """Test no validation message when shutdown_order is set without parallel."""
        config_data = """
remote_servers:
  - name: "Good Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 1
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        assert not any("shutdown_order" in m for m in messages)
        assert not any("cannot set both" in m for m in messages)

    @pytest.mark.unit
    def test_validation_error_shutdown_order_boolean(self, temp_config_file):
        """Test that validation rejects shutdown_order: true (YAML bool is int subclass)."""
        config_data = """
remote_servers:
  - name: "Bad Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: true
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [m for m in messages if "ERROR" in m and "shutdown_order" in m]
        assert len(error_msgs) == 1
        assert "positive integer" in error_msgs[0]

    @pytest.mark.unit
    def test_validation_error_shutdown_order_float(self, temp_config_file):
        """Test that validation rejects shutdown_order: 2.5 (YAML float)."""
        config_data = """
remote_servers:
  - name: "Bad Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 2.5
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [m for m in messages if "ERROR" in m and "shutdown_order" in m]
        assert len(error_msgs) == 1
        assert "positive integer" in error_msgs[0]

    @pytest.mark.unit
    def test_validation_no_conflict_error_when_shutdown_order_invalid(self, temp_config_file):
        """Test no 'cannot set both' error when shutdown_order itself is invalid.

        When shutdown_order fails its own validation, we should not also
        complain about the mutual-exclusion conflict — the user needs to
        fix shutdown_order first; chaining errors would be noisy.
        """
        config_data = """
remote_servers:
  - name: "Bad Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: -1
    parallel: false
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        order_error_msgs = [
            m for m in messages
            if "ERROR" in m and "shutdown_order" in m and ">= 1" in m
        ]
        conflict_msgs = [m for m in messages if "cannot set both" in m]
        assert len(order_error_msgs) == 1
        assert len(conflict_msgs) == 0


class TestShutdownSafetyMargin:
    """Test shutdown_safety_margin configuration parsing and validation."""

    @pytest.mark.unit
    def test_safety_margin_default_60(self, temp_config_file):
        """Test that shutdown_safety_margin defaults to 60 when not specified."""
        config_data = """
remote_servers:
  - name: "Default Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))
        assert config.remote_servers[0].shutdown_safety_margin == 60

    @pytest.mark.unit
    def test_safety_margin_custom_value(self, temp_config_file):
        """Test parsing a custom shutdown_safety_margin value."""
        config_data = """
remote_servers:
  - name: "Slow NAS"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_safety_margin: 180
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))
        assert config.remote_servers[0].shutdown_safety_margin == 180

    @pytest.mark.unit
    def test_safety_margin_zero_accepted(self, temp_config_file):
        """Test that shutdown_safety_margin: 0 is accepted (opt-out of buffer)."""
        config_data = """
remote_servers:
  - name: "No Margin"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_safety_margin: 0
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))
        assert config.remote_servers[0].shutdown_safety_margin == 0

        messages = ConfigLoader.validate_config(config)
        assert not any("shutdown_safety_margin" in m for m in messages)

    @pytest.mark.unit
    def test_safety_margin_negative_rejected(self, temp_config_file):
        """Test that negative shutdown_safety_margin is rejected."""
        config_data = """
remote_servers:
  - name: "Bad Margin"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_safety_margin: -5
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [
            m for m in messages
            if "ERROR" in m and "shutdown_safety_margin" in m
        ]
        assert len(error_msgs) == 1
        assert ">= 0" in error_msgs[0]

    @pytest.mark.unit
    def test_safety_margin_boolean_rejected(self, temp_config_file):
        """Test that boolean shutdown_safety_margin is rejected."""
        config_data = """
remote_servers:
  - name: "Bad Margin"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_safety_margin: true
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [
            m for m in messages
            if "ERROR" in m and "shutdown_safety_margin" in m
        ]
        assert len(error_msgs) == 1
        assert "non-negative integer" in error_msgs[0]

    @pytest.mark.unit
    def test_safety_margin_float_rejected(self, temp_config_file):
        """Test that float shutdown_safety_margin is rejected."""
        config_data = """
remote_servers:
  - name: "Bad Margin"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_safety_margin: 30.5
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        messages = ConfigLoader.validate_config(config)
        error_msgs = [
            m for m in messages
            if "ERROR" in m and "shutdown_safety_margin" in m
        ]
        assert len(error_msgs) == 1
        assert "non-negative integer" in error_msgs[0]


class TestHostLoopback:
    """v5.5: is_host_loopback marks a remote_server as the container's host delegate."""

    @pytest.mark.unit
    def test_defaults_off(self, temp_config_file):
        """Existing configs without the flag default to is_host_loopback=False."""
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: nas\n"
            "    enabled: true\n"
            "    host: 10.0.0.5\n"
            "    user: root\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        assert config.remote_servers[0].is_host_loopback is False
        assert config.remote_servers[0].host_identity_command == "cat /etc/machine-id"
        assert config.remote_servers[0].expected_host_identity is None

    @pytest.mark.unit
    def test_loopback_defaults_host_to_127_0_0_1(self, temp_config_file):
        """When is_host_loopback: true and host is omitted, default to 127.0.0.1
        — works out of the box with `network_mode: host`."""
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        srv = config.remote_servers[0]
        assert srv.is_host_loopback is True
        assert srv.host == "127.0.0.1"

    @pytest.mark.unit
    def test_loopback_explicit_host_override_kept(self, temp_config_file):
        """Operators on Docker bridge / k8s override host to the host-reachable IP."""
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 172.17.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        assert config.remote_servers[0].host == "172.17.0.1"

    @pytest.mark.unit
    def test_custom_host_identity_command_parsed(self, temp_config_file):
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
            "    host_identity_command: 'cat /opt/marker'\n"
            "    expected_host_identity: 'eneru-host-1'\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        srv = config.remote_servers[0]
        assert srv.host_identity_command == "cat /opt/marker"
        assert srv.expected_host_identity == "eneru-host-1"


class TestHostLoopbackValidation:
    """v5.5: configuration-time validation of is_host_loopback entries."""

    @pytest.mark.unit
    def test_multiple_loopbacks_rejected(self, temp_config_file):
        temp_config_file.write_text(
            "ups:\n"
            "  - name: 'UPS-A@localhost'\n"
            "    is_local: true\n"
            "    remote_servers:\n"
            "      - name: lb-a\n"
            "        enabled: true\n"
            "        host: 127.0.0.1\n"
            "        user: root\n"
            "        is_host_loopback: true\n"
            "  - name: 'UPS-B@localhost'\n"
            "    remote_servers:\n"
            "      - name: lb-b\n"
            "        enabled: true\n"
            "        host: 127.0.0.2\n"
            "        user: root\n"
            "        is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        messages = ConfigLoader.validate_config(config)
        loopback_errors = [
            m for m in messages
            if m.startswith("ERROR:") and "is_host_loopback" in m
        ]
        assert any("Multiple remote_servers" in m for m in loopback_errors)

    @pytest.mark.unit
    def test_loopback_disabled_is_error(self, temp_config_file):
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: false\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        messages = ConfigLoader.validate_config(config)
        assert any(
            m.startswith("ERROR:") and "is_host_loopback" in m and "enabled is false" in m
            for m in messages
        )

    @pytest.mark.unit
    def test_loopback_empty_user_is_error(self, temp_config_file):
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: ''\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        messages = ConfigLoader.validate_config(config)
        assert any(
            m.startswith("ERROR:") and "is_host_loopback" in m and "'user' is empty" in m
            for m in messages
        )

    @pytest.mark.unit
    def test_loopback_unsafe_identity_command_rejected(self, temp_config_file):
        """A probe that could chain into a destructive command is rejected."""
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
            "    host_identity_command: 'cat /etc/machine-id; shutdown -h now'\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        messages = ConfigLoader.validate_config(config)
        assert any(
            m.startswith("ERROR:") and "host_identity_command" in m and "unsafe" in m
            for m in messages
        )

    @pytest.mark.unit
    def test_loopback_non_root_user_without_sudo_warns(self, temp_config_file):
        """Per-plan sudo guard: non-root user + no sudo prefix → WARNING."""
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: eneru\n"
            "    shutdown_command: 'shutdown -h now'\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        messages = ConfigLoader.validate_config(config)
        assert any(
            m.startswith("WARNING:") and "user is 'eneru'" in m and "sudo" in m
            for m in messages
        )

    @pytest.mark.unit
    def test_sudo_guard_applies_to_non_loopback_servers_too(self, temp_config_file):
        """The sudo warning fires for any non-root remote_server, not just loopback."""
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: nas\n"
            "    enabled: true\n"
            "    host: 10.0.0.5\n"
            "    user: deploy\n"
            "    shutdown_command: 'shutdown -h now'\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        messages = ConfigLoader.validate_config(config)
        assert any(
            m.startswith("WARNING:") and "user is 'deploy'" in m and "sudo" in m
            for m in messages
        )

    @pytest.mark.unit
    def test_unknown_key_rejection_intact_for_remote_servers(self, temp_config_file):
        """Unknown keys still rejected; new keys accepted."""
        temp_config_file.write_text(
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
            "    nonsense_key: 'foo'\n"
        )
        config = ConfigLoader.load(str(temp_config_file))
        raw = yaml.safe_load(open(temp_config_file))
        messages = ConfigLoader.validate_config(config, raw_data=raw)
        assert any(
            m.startswith("ERROR:") and "nonsense_key" in m for m in messages
        )
