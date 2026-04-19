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
