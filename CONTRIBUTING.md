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
- **Relevant logs** from `journalctl -u ups-monitor.service`
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

# Create virtual environment (optional but recommended)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install pyyaml requests

# Run in dry-run mode for testing
python3 ups_monitor.py --dry-run --config config.yaml
```

## Testing Checklist

Before submitting a PR, ensure:

- [ ] `--validate-config` passes
- [ ] `--dry-run` mode works correctly
- [ ] No Python syntax errors (`python3 -m py_compile ups_monitor.py`)
- [ ] Existing features still work
- [ ] New configuration options are documented

## Questions?

Feel free to open an issue for any questions about contributing!
