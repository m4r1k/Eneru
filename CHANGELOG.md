## Version History

### Version 4.0 (Current) - External Configuration & Open Source Release
Complete refactoring to support external YAML configuration file, making the program fully generic and configurable without code changes. Full open source release with comprehensive documentation and community contribution infrastructure.

### Version 3.0 - Python Rewrite
Complete rewrite from Bash to Python, offering improved reliability, maintainability, and native handling of JSON, math operations, and complex data structures.

### Version 2.0 - Enhanced Bash
Major improvements including Discord notifications, stateful event tracking, dynamic VM wait times, hang-proof unmounting, and SSH key-based authentication for remote NAS shutdown.

### Version 1.0 - Original Bash
Initial implementation with basic UPS monitoring, battery depletion tracking, and shutdown sequence for VMs, Docker, and remote NAS.

---

## What's New in Each Version

### Version 4.0 Changes (from 3.0)

| Feature | Version 3.0 | Version 4.0 |
|---------|-------------|-------------|
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

**New Features in 4.0:**
- External YAML configuration file (`/etc/ups-monitor/config.yaml`)
- All features independently enable/disable without code changes
- Multiple remote servers with individual settings
- Custom shutdown commands per remote server
- Per-server SSH options and timeouts
- Command-line argument support
- Configuration validation (`--validate-config`)
- Graceful degradation when optional dependencies missing
- Modular configuration classes
- Enhanced install script with package manager detection
- Config preservation on reinstall

**Documentation in 4.0:**
- Comprehensive README with badges and architecture diagram
- Complete version history with detailed changelogs
- Installation guide with multi-distro support
- Configuration reference with all options documented
- Troubleshooting guide for common issues
- Security considerations and best practices

**Example Configurations in 4.0:**
- `config-minimal.yaml` - Basic single-server setup
- `config-homelab.yaml` - VMs, Docker, NAS, Discord notifications
- `config-enterprise.yaml` - Multi-server enterprise deployment

**Community & Contribution Infrastructure in 4.0:**
- `CONTRIBUTING.md` - Full contributor guide with:
  - Code style guidelines
  - Testing requirements
  - Commit message conventions
  - Development setup instructions
  - Pull request process
- GitHub Issue Templates:
  - Bug report template with environment details
  - Feature request template with use case format
  - Issue template chooser configuration
- GitHub Pull Request Template with testing checklist
- GitHub Actions CI workflow:
  - Syntax validation across Python 3.9-3.12
  - Configuration file validation
  - Example config validation

**Removed in 4.0:**
- Hardcoded configuration values in source code
- Single remote NAS limitation
- Apache 2.0 license (now MIT)

### Version 3.0 Changes (from 2.0)

| Feature | Version 2.0 (Bash) | Version 3.0 (Python) |
|---------|-------------------|---------------------|
| Language | Bash 4.0+ | Python 3.9+ |
| JSON Handling | External `jq` dependency | Native Python (`requests` library) |
| Math Operations | External `bc` dependency | Native Python |
| Configuration | Shell variables | Python dataclass with type hints |
| State Management | File-based with shell parsing | In-memory with file persistence |
| Type Safety | None | Full type hints |
| Error Handling | Shell traps | Python exceptions |
| String Formatting | Shell variable expansion | Python string concatenation |

**Removed Dependencies in 3.0:**
- `jq` - JSON now handled natively
- `bc` - Math now handled natively
- `awk` - Text processing now handled natively
- `grep` - Pattern matching now handled natively

**New Dependencies in 3.0:**
- `python3-requests` - For Discord webhook notifications

### Version 2.0 Changes (from 1.0)

| Feature | Version 1.0 | Version 2.0 |
|---------|-------------|-------------|
| Notifications | None | Discord webhooks with rich embeds |
| Event Tracking | Basic logging | Stateful tracking (prevents log spam) |
| VM Shutdown | Fixed 10s wait | Dynamic wait up to 30s with force destroy |
| NAS Authentication | Password in script (`sshpass`) | SSH key-based (no passwords stored) |
| Unmounting | Basic unmount | Timeout-protected unmount (hang-proof) |
| Voltage Monitoring | Relative change detection | Absolute threshold-based detection |
| Depletion Rate | 60-second window, 15 samples | 300-second window, 30 samples, grace period |
| AVR Monitoring | None | Boost/Trim detection |
| Connection Handling | Basic retry | Stale data detection, failsafe shutdown |
| Crisis Reporting | None | Elevated notifications during shutdown |
| Shutdown Triggers | 4 triggers | 4 triggers + FSD flag detection |
| Configuration | Minimal | Comprehensive with mount options |

**New Features in 2.0:**
- Discord webhook integration with color-coded embeds
- Depletion rate grace period (prevents false triggers on power loss)
- Failsafe Battery Protection (FSB) - shutdown if connection lost while on battery
- FSD (Forced Shutdown) flag detection from UPS
- Configurable mount list with per-mount options (e.g., lazy unmount)
- Overload state tracking with resolution detection
- Bypass mode detection
- Service stop notifications
- Bash 4.0+ requirement for associative arrays
- `jq` requirement for robust JSON generation

**Security Improvements in 2.0:**
- Removed `sshpass` and password storage
- SSH key-based authentication for NAS
- Passwordless sudo configuration guide
