#!/usr/bin/env python3
"""
Wrapper script for Eneru deb/rpm package installation.
This script is installed as /opt/ups-monitor/eneru.py and invokes the CLI.
"""
import sys

# F-013: on RHEL 8 the system ``python3`` is 3.6, but Eneru needs 3.9+ (it uses
# dataclasses and other 3.7+ stdlib). The el8 RPM depends on the ``python39``
# module, so a suitable interpreter IS installed -- but the systemd unit's
# ``ExecStart=/usr/bin/python3`` and any ``python3 /opt/ups-monitor/eneru.py``
# invocation still resolve to 3.6 and would crash on the first 3.7+ import.
# ELI5: someone handed us the wrong-sized wrench (3.6); before touching a single
# bolt, walk to the toolbox and grab the right one (3.9+), then redo the job with
# it. Re-exec into the newest python3.x on PATH. This runs BEFORE any ``eneru``
# import, so nothing 3.7+ is parsed under 3.6. On 3.9+ it is a no-op.
if sys.version_info < (3, 9):
    import os
    import shutil

    for _candidate in (
        "python3.13", "python3.12", "python3.11", "python3.10", "python3.9",
    ):
        _interp = shutil.which(_candidate)
        if _interp:
            # Replace this process with the newer interpreter, preserving argv
            # (argv[0] is this script path, so it re-runs the wrapper under 3.9+).
            os.execv(_interp, [_interp] + sys.argv)
    sys.stderr.write(
        "Eneru requires Python 3.9+, but this interpreter is %d.%d and no "
        "newer python3.x was found on PATH. On RHEL 8, install the python39 "
        "module (the el8 package depends on it).\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    sys.exit(1)

# Add the package directory to Python path so 'eneru' module can be found
sys.path.insert(0, '/opt/ups-monitor')

from eneru.cli import main

if __name__ == "__main__":
    main()
