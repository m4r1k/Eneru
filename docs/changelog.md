# Changelog

All notable changes to Eneru are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

!!! tip "Full Changelog"
    For the complete changelog with all version comparisons, see [CHANGELOG.md on GitHub](https://github.com/m4r1k/Eneru/blob/main/CHANGELOG.md).

---

## [4.4.0] - 2025-12-30

### Added
- **Read The Docs Integration:** Documentation now hosted on Read The Docs with MkDocs Material theme
- **Dark Mode Documentation:** Material theme with automatic dark mode support
- **Improved Search:** Full-text search across all documentation pages
- **Code Copy Buttons:** One-click copy for all code blocks in documentation
- **Tabbed Installation Instructions:** Distro-specific tabs for Debian/Ubuntu, RHEL/Fedora, and manual install
- **Upgrade/Uninstall Instructions:** Previously missing documentation for upgrading and removing Eneru
- **Dedicated Documentation Pages:** Getting Started, Configuration, Triggers, Notifications, Remote Servers, Troubleshooting

### Changed
- **README Slimmed Down:** Reduced from ~1200 lines to ~145 lines, linking to RTD for details

---

## [4.3.0] - 2025-12-29

### Added
- **Native Package Distribution:** Official `.deb` and `.rpm` packages for easy installation
- **APT/DNF Repository:** Packages available via GitHub Pages hosted repository for Debian, Ubuntu, RHEL, and Fedora
- **Version CLI Option:** New `-v`/`--version` flag to display current version
- **Version Display:** Version now shown at service startup and in notifications
- **nFPM Build System:** Automated package building using nFPM for both Debian and RPM formats
- **GitHub Release Automation:** Packages automatically built and published on GitHub releases
- **GPG Signed Repository:** Repository metadata is GPG signed for security

### Changed
- **Service Name:** Renamed from `ups-monitor.service` to `eneru.service` to avoid conflict with nut-client's service
- **Installation Method:** Package installation (deb/rpm) is now the recommended method
- **Service Behavior:** Packages install but do not auto-enable or auto-start the service

### Fixed
- **Discord Mention Prevention:** Added zero-width space after `@` symbols to prevent false mentions

---

## [4.2.0] - 2025-12-23

### Added
- **Apprise Integration:** Support for 100+ notification services (Discord, Slack, Telegram, ntfy, Pushover, Email, and more)
- **Non-Blocking Notification Architecture:** Notifications never delay critical shutdown operations
- **`--test-notifications` CLI Option:** Send test notification to verify configuration
- **Test Suite:** Comprehensive pytest test suite with 80+ unit and integration tests

### Changed
- **Notification System:** Migrated from native Discord webhooks to Apprise library
- **Script Filename:** Renamed `ups-monitor.py` to `ups_monitor.py` for Python module compatibility

---

## [4.1.0] - 2025-12-19

### Added
- Native Podman support alongside Docker
- Container runtime auto-detection (prefers Podman over Docker)
- Comprehensive "Shutdown Triggers Explained" documentation
- "The Name" section explaining One Piece reference

### Changed
- Project rebranded from "UPS Tower" to "Eneru"
- Configuration section `docker` renamed to `containers` (backwards compatible)

---

## [4.0.0] - 2025-12-17

### Added
- External YAML configuration file support
- Multiple remote server shutdown with per-server custom commands
- Command-line arguments: `--config`, `--dry-run`, `--validate-config`
- Comprehensive documentation and examples

### Changed
- Configuration now loaded from external file instead of source code
- License changed from Apache 2.0 to MIT

---

## Earlier Versions

For complete history including v3.0, v2.0, and v1.0, see the [full CHANGELOG on GitHub](https://github.com/m4r1k/Eneru/blob/main/CHANGELOG.md).
