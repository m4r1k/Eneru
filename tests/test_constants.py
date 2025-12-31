"""Test constants - Fake credentials for testing (following Apprise patterns)."""

# =============================================================================
# Discord webhook IDs (realistic lengths but obviously fake)
# =============================================================================
TEST_DISCORD_WEBHOOK_ID = "A" * 18  # Discord webhook IDs are ~18 digits
TEST_DISCORD_WEBHOOK_TOKEN = "B" * 68  # Discord tokens are ~68 chars

# Pre-built URLs for convenience
TEST_DISCORD_APPRISE_URL = f"discord://{TEST_DISCORD_WEBHOOK_ID}/{TEST_DISCORD_WEBHOOK_TOKEN}/"
TEST_DISCORD_WEBHOOK_URL = (
    f"https://discord.com/api/webhooks/{TEST_DISCORD_WEBHOOK_ID}/{TEST_DISCORD_WEBHOOK_TOKEN}"
)

# =============================================================================
# Slack test tokens
# =============================================================================
TEST_SLACK_TOKEN_A = "C" * 9
TEST_SLACK_TOKEN_B = "D" * 9
TEST_SLACK_TOKEN_C = "E" * 24
TEST_SLACK_APPRISE_URL = f"slack://{TEST_SLACK_TOKEN_A}/{TEST_SLACK_TOKEN_B}/{TEST_SLACK_TOKEN_C}/"

# =============================================================================
# Generic test URL for services that don't need specific format
# =============================================================================
TEST_JSON_WEBHOOK_URL = "json://localhost:8080/webhook"
