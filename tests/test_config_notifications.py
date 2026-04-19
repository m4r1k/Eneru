"""Tests for notifications config: legacy Discord conversion and avatar handling."""

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


class TestLegacyDiscordConfig:
    """Test legacy Discord configuration conversion."""

    @pytest.mark.unit
    def test_legacy_discord_webhook_conversion(self, temp_config_file):
        """Test that legacy Discord webhook is converted to Apprise format."""
        config_data = f"""
notifications:
  discord:
    webhook_url: "{TEST_DISCORD_WEBHOOK_URL}"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert len(config.notifications.urls) == 1
        assert config.notifications.urls[0].startswith("discord://")
        assert TEST_DISCORD_WEBHOOK_ID in config.notifications.urls[0]
        assert TEST_DISCORD_WEBHOOK_TOKEN in config.notifications.urls[0]

    @pytest.mark.unit
    def test_top_level_legacy_discord(self, temp_config_file):
        """Test top-level legacy Discord configuration."""
        config_data = f"""
discord:
  webhook_url: "{TEST_DISCORD_WEBHOOK_URL}"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert "discord://" in config.notifications.urls[0]

    @pytest.mark.unit
    def test_discord_webhook_to_apprise_format(self):
        """Test the webhook URL conversion function."""
        result = ConfigLoader._convert_discord_webhook_to_apprise(TEST_DISCORD_WEBHOOK_URL)
        assert result == f"discord://{TEST_DISCORD_WEBHOOK_ID}/{TEST_DISCORD_WEBHOOK_TOKEN}/"

    @pytest.mark.unit
    def test_non_discord_url_unchanged(self):
        """Test that non-Discord URLs are not modified."""
        result = ConfigLoader._convert_discord_webhook_to_apprise(TEST_SLACK_APPRISE_URL)
        assert result == TEST_SLACK_APPRISE_URL


class TestAvatarUrlAppending:
    """Test avatar URL appending to notification URLs."""

    @pytest.mark.unit
    def test_append_avatar_to_discord(self):
        """Test appending avatar to Discord URL."""
        avatar = "https://example.com/avatar.png"
        result = ConfigLoader._append_avatar_to_url(TEST_DISCORD_APPRISE_URL, avatar)

        assert "avatar_url=https%3A%2F%2Fexample.com%2Favatar.png" in result

    @pytest.mark.unit
    def test_append_avatar_to_slack(self):
        """Test appending avatar to Slack URL."""
        avatar = "https://example.com/icon.png"
        result = ConfigLoader._append_avatar_to_url(TEST_SLACK_APPRISE_URL, avatar)

        assert "avatar_url=" in result

    @pytest.mark.unit
    def test_no_avatar_for_unsupported_service(self):
        """Test that avatar is not appended to unsupported services."""
        url = "mailto://user:pass@smtp.example.com"
        avatar = "https://example.com/avatar.png"
        result = ConfigLoader._append_avatar_to_url(url, avatar)

        assert result == url
        assert "avatar_url" not in result

    @pytest.mark.unit
    def test_no_avatar_when_none(self):
        """Test that nothing is appended when avatar is None."""
        result = ConfigLoader._append_avatar_to_url(TEST_DISCORD_APPRISE_URL, None)
        assert result == TEST_DISCORD_APPRISE_URL

    @pytest.mark.unit
    def test_no_avatar_when_empty(self):
        """Test that nothing is appended when avatar is empty."""
        result = ConfigLoader._append_avatar_to_url(TEST_DISCORD_APPRISE_URL, "")
        assert result == TEST_DISCORD_APPRISE_URL


