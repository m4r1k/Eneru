# Changelog

All notable changes to Eneru are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

!!! tip "Full Changelog"
    For the complete changelog with all version comparisons, see [CHANGELOG.md on GitHub](https://github.com/m4r1k/Eneru/blob/main/CHANGELOG.md).

---

## [4.7.0] - 2026-01-03

### Added
- **Persistent Notification Retry:** Notifications are now retried until successful delivery
    - Worker thread persistently retries failed notifications instead of dropping them
    - FIFO queue ensures message order is preserved
    - New `notifications.retry_interval` configuration option (default: 5 seconds)
    - Guaranteed delivery during transient network outages (e.g., 30-second power blip)
- **Expanded Test Suite:** 185 tests (7 new for notification retry)

### Changed
- **Notification Architecture:** Evolved from "fire-and-forget" to "persistent retry with ACK"
    - Main thread still queues instantly (zero blocking on shutdown operations)
    - Worker thread now retries each message until success before moving to next

---

## [4.6.0] - 2025-12-31

### Added
- **Modern Python Packaging:** Added `pyproject.toml` for PEP 517/518 compliant packaging
    - Can now be installed via `pip install .` from repository root
    - Entry point: `eneru` command available after pip install
    - Optional dependencies: `[notifications]`, `[dev]`, `[docs]`
- **Package Structure:** Reorganized codebase into proper Python package
    - Source code moved to `src/eneru/` directory
    - `__init__.py` exports all public APIs
    - `__main__.py` enables `python -m eneru` invocation
- **Comprehensive Test Suite:** Expanded to 178 tests

### Changed
- **Script Renamed:** `ups_monitor.py` → `src/eneru/monitor.py`
- **Installed Path:** `/opt/ups-monitor/ups_monitor.py` → `/opt/ups-monitor/eneru.py`

---

## [4.5.0] - 2025-12-30

### Added
- **Docker/Podman Compose Shutdown:** Ordered shutdown of compose stacks before individual containers
    - New `containers.compose_files` configuration for defining compose files to stop
    - Per-file timeout override support (`stop_timeout`)
    - New `containers.shutdown_all_remaining_containers` option (default: true)
- **Remote Server Pre-Shutdown Commands:** Execute commands on remote servers before shutdown
    - New `remote_servers[].pre_shutdown_commands` configuration
    - Predefined actions: `stop_containers`, `stop_vms`, `stop_proxmox_vms`, `stop_proxmox_cts`, `stop_xcpng_vms`, `stop_esxi_vms`, `stop_compose`, `sync`
    - Custom command support with per-command timeout
- **Parallel Remote Server Shutdown:** Concurrent shutdown of multiple remote servers using threads
    - New `remote_servers[].parallel` option (default: true)
    - Servers with `parallel: false` shutdown sequentially first, then parallel batch runs concurrently

### Changed
- **Sync Hardening:** Added 2-second sleep after `os.sync()` for storage controller cache flush
- **Notification Worker:** Now logs pending message count when stopping during shutdown

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
