#!/usr/bin/env python3
"""
Wrapper script for Eneru deb/rpm package installation.
This script is installed as /opt/ups-monitor/eneru.py and invokes the CLI.
"""
import sys

# Eneru needs Python 3.9+. Every supported package target (Debian 12+,
# Ubuntu 22.04+, RHEL 9/10) ships a 3.9+ system ``python3``; this guard only
# turns a manual install on an older interpreter into a clear message instead
# of a SyntaxError deep inside the package. It runs BEFORE any ``eneru``
# import, so nothing 3.9-only is parsed under an old interpreter.
if sys.version_info < (3, 9):
    sys.stderr.write(
        "Eneru requires Python 3.9+, but this interpreter is %d.%d. "
        "On distributions without a 3.9+ system python3 (e.g. RHEL 8), run "
        "the Eneru container image (Docker/Podman) instead.\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    sys.exit(1)


def _main():
    """Load the packaged CLI only after interpreter compatibility is settled."""
    sys.path.insert(0, '/opt/ups-monitor')
    from eneru.cli import main
    main()


if __name__ == "__main__":
    _main()
