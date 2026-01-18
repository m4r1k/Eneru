# Contributing to Eneru

First off, thank you for considering contributing to Eneru! 🎉

## How Can I Contribute?

### 🐛 Reporting Bugs

Before creating bug reports, please check existing issues. When creating a bug report, include:

- **Clear title** describing the issue
- **Steps to reproduce** the behavior
- **Expected behavior** vs what actually happened
- **Environment details:**
  - OS and version
  - Python version (`python3 --version`)
  - NUT version (`upsc -V`)
  - UPS model
- **Relevant logs** from `journalctl -u eneru.service`
- **Configuration** (sanitize sensitive data like webhook URLs)

### 💡 Suggesting Features

Feature requests are welcome! Please include:

- **Use case** - Why do you need this feature?
- **Proposed solution** - How do you envision it working?
- **Alternatives considered** - Other approaches you've thought about

### 🔧 Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Test thoroughly (use `--dry-run` mode)
5. Commit with clear messages (`git commit -m 'Add amazing feature'`)
6. Push to your branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

#### Code Style

- Follow PEP 8 for Python code
- Use type hints where possible
- Add docstrings for new functions/classes
- Keep configuration options well-documented

### 📖 Documentation

Improvements to documentation are always welcome:

- Fix typos or unclear explanations
- Add examples for different use cases
- Translate documentation
- Add troubleshooting tips

## Development Setup

```bash
# Clone your fork
git clone https://github.com/m4r1k/Eneru.git
cd Eneru

# Create virtual environment using uv (recommended for speed)
uv venv /tmp/eneru-venv
source /tmp/eneru-venv/bin/activate

# Install package with all dev dependencies
uv pip install -e ".[dev,notifications,docs]"

# Run in dry-run mode for testing
python -m eneru --dry-run --config config.yaml
# Or use the entry point:
eneru --dry-run --config config.yaml
```

### Project Structure (v4.10+)

Eneru uses a modular architecture with focused modules:

```
src/eneru/
  __init__.py         # Public API exports
  __main__.py         # CLI entry point (python -m eneru)
  version.py          # Version string
  config.py           # Configuration dataclasses + ConfigLoader
  state.py            # MonitorState dataclass
  logger.py           # TimezoneFormatter + UPSLogger
  notifications.py    # NotificationWorker (Apprise integration)
  utils.py            # Helper functions (run_command, etc.)
  actions.py          # REMOTE_ACTIONS templates
  monitor.py          # UPSMonitor class (core daemon)
  cli.py              # CLI argument parsing + main()
```

## Testing Checklist

Before submitting a PR, ensure:

- [ ] All tests pass (`pytest`)
- [ ] `--validate-config` passes (`python -m eneru --validate-config`)
- [ ] `--dry-run` mode works correctly
- [ ] No Python syntax errors (`python -m py_compile src/eneru/*.py`)
- [ ] Existing features still work
- [ ] New configuration options are documented

## Questions?

Feel free to open an issue for any questions about contributing!
