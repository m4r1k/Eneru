"""Tests for VM and container config: compose files, runtime, container shutdown options."""

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


class TestComposeFilesConfig:
    """Test compose files configuration parsing."""

    @pytest.mark.unit
    def test_string_compose_paths(self, temp_config_file):
        """Test simple string compose file paths."""
        config_data = """
containers:
  enabled: true
  compose_files:
    - "/path/to/docker-compose.yml"
    - "/another/path/compose.yaml"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.containers.compose_files) == 2
        assert config.containers.compose_files[0].path == "/path/to/docker-compose.yml"
        assert config.containers.compose_files[0].stop_timeout is None
        assert config.containers.compose_files[1].path == "/another/path/compose.yaml"

    @pytest.mark.unit
    def test_dict_compose_paths_with_timeout(self, temp_config_file):
        """Test dictionary compose paths with custom timeout."""
        config_data = """
containers:
  enabled: true
  stop_timeout: 60
  compose_files:
    - path: "/path/to/critical-db/docker-compose.yml"
      stop_timeout: 120
    - path: "/path/to/app/docker-compose.yml"
      stop_timeout: 30
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.containers.compose_files) == 2
        assert config.containers.compose_files[0].path == "/path/to/critical-db/docker-compose.yml"
        assert config.containers.compose_files[0].stop_timeout == 120
        assert config.containers.compose_files[1].path == "/path/to/app/docker-compose.yml"
        assert config.containers.compose_files[1].stop_timeout == 30

    @pytest.mark.unit
    def test_mixed_compose_formats(self, temp_config_file):
        """Test mixed string and dictionary compose file formats."""
        config_data = """
containers:
  enabled: true
  compose_files:
    - "/simple/path/docker-compose.yml"
    - path: "/path/with/timeout/docker-compose.yml"
      stop_timeout: 180
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.containers.compose_files) == 2
        assert config.containers.compose_files[0].path == "/simple/path/docker-compose.yml"
        assert config.containers.compose_files[0].stop_timeout is None
        assert config.containers.compose_files[1].path == "/path/with/timeout/docker-compose.yml"
        assert config.containers.compose_files[1].stop_timeout == 180

    @pytest.mark.unit
    def test_shutdown_all_remaining_containers_default(self, temp_config_file):
        """Test that shutdown_all_remaining_containers defaults to True."""
        config_data = """
containers:
  enabled: true
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.shutdown_all_remaining_containers is True

    @pytest.mark.unit
    def test_shutdown_all_remaining_containers_false(self, temp_config_file):
        """Test setting shutdown_all_remaining_containers to False."""
        config_data = """
containers:
  enabled: true
  shutdown_all_remaining_containers: false
  compose_files:
    - "/path/to/docker-compose.yml"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.shutdown_all_remaining_containers is False

    @pytest.mark.unit
    def test_empty_compose_files(self, temp_config_file):
        """Test empty compose_files list."""
        config_data = """
containers:
  enabled: true
  compose_files: []
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.compose_files == []

    @pytest.mark.unit
    def test_no_compose_files_key(self, temp_config_file):
        """Test missing compose_files key defaults to empty list."""
        config_data = """
containers:
  enabled: true
  runtime: docker
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.compose_files == []


