# Changelog

All notable changes to Eneru will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [4.1.0] - 2025-12-19

### Added
- Native Podman support alongside Docker
- Container runtime auto-detection (prefers Podman over Docker)
- New `containers.runtime` configuration option: `auto`, `docker`, or `podman`
- Support for stopping rootless Podman user containers (`include_user_containers`)
- Comprehensive "Shutdown Triggers Explained" documentation section
- Detailed depletion rate calculation explanation with examples
- Grace period rationale and behavior documentation
- Trigger interaction and overlap analysis
- Recommended configurations (conservative, balanced, aggressive)
- Trigger evaluation flowchart
- `.gitignore` for common editor and Python files
- "The Name" section explaining One Piece reference

### Changed
- Project rebranded from "UPS Tower" to "Eneru"
- Configuration section `docker` renamed to `containers` (backwards compatible)
- `--validate-config` output updated for containers section
- Diagram renamed to `eneru-diagram.png`
- Updated all documentation with Eneru branding
- CHANGELOG.md revamped to Keep a Changelog format with version comparisons

### Fixed
- `--validate-config` crash when referencing old `config.docker` attribute

### Backwards Compatibility
- Existing `docker:` configuration sections continue to work
- Technical paths unchanged (`ups-monitor.py`, `/opt/ups-monitor/`, `/etc/ups-monitor/`)

---

## [4.0.0] - 2025-12-17

### Added
- External YAML configuration file support (`/etc/ups-monitor/config.yaml`)
- Multiple remote server shutdown with per-server custom commands
- Command-line arguments: `--config`, `--dry-run`, `--validate-config`
- Graceful degradation when optional dependencies (PyYAML, requests) missing
- Modular configuration classes
- GitHub Actions workflow for syntax and configuration validation (Python 3.9-3.12)
- Comprehensive README with badges and architecture diagram
- Complete version history with detailed changelogs
- Installation guide with multi-distro support
- Configuration reference with all options documented
- Troubleshooting guide for common issues
- Security considerations and best practices
- CONTRIBUTING.md with:
  - Code style guidelines
  - Testing requirements
  - Commit message conventions
  - Development setup instructions
  - Pull request process
- Example configurations:
  - `config-minimal.yaml` - Basic single-server setup
  - `config-homelab.yaml` - VMs, Docker, NAS, Discord notifications
  - `config-enterprise.yaml` - Multi-server enterprise deployment
- GitHub Issue Templates:
  - Bug report template with environment details
  - Feature request template with use case format
  - Issue template chooser configuration
- GitHub Pull Request Template with testing checklist

### Changed
- Configuration now loaded from external file instead of source code
- All features independently toggleable via configuration
- Install script preserves existing configuration on upgrade
- Install script auto-detects package manager (dnf, apt, pacman)
- License changed from Apache 2.0 to MIT

### Removed
- Hardcoded configuration values in source code
- Single remote NAS limitation (now supports multiple servers)
- Apache 2.0 license (replaced with MIT)

---

## [3.0.0] - 2025-12-15

### Added
- Complete rewrite from Bash to Python 3.9+
- Native JSON handling via `requests` library
- Native math operations (no external dependencies)
- Python dataclass configuration with type hints
- In-memory state management with file persistence
- Full type hints throughout codebase
- Python exception-based error handling
- Python string formatting (replacing shell variable expansion)

### Changed
- Language: Bash 4.0+ → Python 3.9+
- Configuration: Shell variables → Python dataclass with type hints
- State management: File-based with shell parsing → In-memory with file persistence
- Error handling: Shell traps → Python exceptions

### Removed
- `jq` dependency (JSON now handled natively)
- `bc` dependency (math now handled natively)
- `awk` dependency (text processing now handled natively)
- `grep` dependency (pattern matching now handled natively)

### Dependencies
- Added: `python3-requests`
- Removed: `jq`, `bc`, `awk`, `grep`

---

## [2.0.0] - 2025-10-22

### Added
- Discord webhook integration with color-coded embeds
- Depletion rate grace period (prevents false triggers on power loss)
- Failsafe Battery Protection (FSB) - shutdown if connection lost while on battery
- FSD (Forced Shutdown) flag detection from UPS
- Configurable mount list with per-mount options (e.g., lazy unmount)
- Overload state tracking with resolution detection
- Bypass mode detection
- AVR (Automatic Voltage Regulation) Boost/Trim detection
- Service stop notifications
- Dynamic VM wait times (up to 30s with force destroy)
- Timeout-protected unmounting (hang-proof)
- Absolute threshold-based voltage monitoring
- Extended depletion tracking (300-second window, 30 samples)
- Stale data detection for connection handling
- Crisis reporting (elevated notifications during shutdown)
- Passwordless sudo configuration guide

### Changed
- VM shutdown: fixed 10s wait → dynamic wait up to 30s with force destroy
- NAS authentication: password in script (`sshpass`) → SSH key-based (no passwords stored)
- Voltage monitoring: relative change detection → absolute threshold-based detection
- Depletion rate: 60-second window, 15 samples → 300-second window, 30 samples with grace period
- Connection handling: basic retry → stale data detection with failsafe shutdown
- Shutdown triggers: 4 triggers → 4 triggers + FSD flag detection
- Configuration: minimal → comprehensive with mount options

### Security
- Removed `sshpass` and password storage
- SSH key-based authentication for NAS
- Passwordless sudo configuration guide

### Dependencies
- Added: `jq` (for robust JSON generation)
- Removed: `sshpass`
- Required: Bash 4.0+ (for associative arrays)

---

## [1.0.0] - 2025-10-18

### Added
- Initial implementation in Bash
- Basic UPS monitoring via NUT (Network UPS Tools)
- Battery depletion tracking (60-second window, 15 samples)
- Shutdown sequence:
  - Virtual Machines (libvirt/KVM)
  - Docker containers
  - Remote NAS (via SSH with `sshpass`)
- Basic logging
- systemd service integration
- 4 shutdown triggers:
  - Low battery threshold
  - Critical runtime threshold
  - Depletion rate threshold
  - Extended time on battery

---

## Version Comparison

### v4.1 vs v4.0

| Feature | v4.0 | v4.1 |
|---------|------|------|
| Container Runtime | Docker only | Docker + Podman |
| Runtime Detection | N/A | Auto-detect (prefers Podman) |
| Rootless Containers | No | Yes (Podman) |
| Project Name | UPS Tower | Eneru |
| Trigger Documentation | Basic | Comprehensive with examples |
| Changelog Format | Basic | Keep a Changelog format |

### v4.0 vs v3.0

| Feature | v3.0 | v4.0 |
|---------|------|------|
| Configuration | Python dataclass in code | External YAML file |
| Remote Servers | Single hardcoded NAS | Multiple configurable servers |
| Shutdown Commands | Hardcoded per system type | Customizable per server |
| Feature Toggles | Edit source code | Enable/disable in config |
| CLI Options | None | `--config`, `--dry-run`, `--validate-config` |
| Dependencies | Hard failure if missing | Graceful degradation |
| Installation | Overwrites config | Preserves existing config |
| Documentation | Basic README | Comprehensive docs, examples, guides |
| Community | None | Issue templates, PR templates, CI |
| License | Apache 2.0 | MIT |

### v3.0 vs v2.0

| Feature | v2.0 (Bash) | v3.0 (Python) |
|---------|-------------|---------------|
| Language | Bash 4.0+ | Python 3.9+ |
| JSON Handling | External `jq` | Native (`requests`) |
| Math Operations | External `bc` | Native |
| Configuration | Shell variables | Python dataclass |
| State Management | File-based with shell parsing | In-memory + file |
| Type Safety | None | Full type hints |
| Error Handling | Shell traps | Python exceptions |
| String Formatting | Shell variable expansion | Python f-strings |

### v2.0 vs v1.0

| Feature | v1.0 | v2.0 |
|---------|------|------|
| Notifications | None | Discord webhooks with rich embeds |
| Event Tracking | Basic logging | Stateful tracking (prevents spam) |
| VM Shutdown | Fixed 10s wait | Dynamic up to 30s + force destroy |
| NAS Auth | Password (`sshpass`) | SSH keys (no passwords) |
| Unmounting | Basic | Timeout-protected (hang-proof) |
| Voltage Monitoring | Relative | Absolute thresholds |
| Depletion Rate | 60s window, 15 samples | 300s window, 30 samples + grace period |
| AVR Monitoring | None | Boost/Trim detection |
| Connection Handling | Basic retry | Stale detection + failsafe |
| Crisis Reporting | None | Elevated notifications during shutdown |
| Shutdown Triggers | 4 triggers | 4 triggers + FSD flag |
| Bypass/Overload | None | Full detection + resolution tracking |
