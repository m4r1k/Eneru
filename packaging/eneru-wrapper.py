#!/usr/bin/env python3
"""
Wrapper script for Eneru deb/rpm package installation.
This script is installed as /opt/ups-monitor/eneru.py and invokes the CLI.
"""
import os
import re
import subprocess
import sys


_PYTHON_NAME = re.compile(r"^python3\.(\d+)$")


def _compatible_python_on_path(path=None):
    """Return a compatible executable Python 3.9+ found on ``PATH``.

    Candidate names are discovered instead of capped at the newest Python that
    existed when this wrapper was written. Each binary reports its real version
    before selection, so a misleading old-version symlink cannot re-exec the
    wrapper into a loop. Prefer 3.9 because the EL8 RPM guarantees its runtime
    dependencies for the ``python39`` module; use newer interpreters only as a
    fallback for non-RPM/manual wrapper deployments.
    """
    candidates = {}
    # The EL8 RPM owns this interpreter and installs PyYAML for it. Prefer it
    # over a same-named /usr/local shim that happens to appear earlier on PATH.
    system_python39 = "/usr/bin/python3.9"
    if (os.path.isfile(system_python39)
            and os.access(system_python39, os.X_OK)):
        candidates.setdefault(9, []).append(system_python39)
    for directory in (path if path is not None else
                      os.environ.get("PATH", "")).split(os.pathsep):
        directory = directory or os.curdir
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            match = _PYTHON_NAME.match(name)
            if match is None or int(match.group(1)) < 9:
                continue
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                bucket = candidates.setdefault(int(match.group(1)), [])
                if candidate not in bucket:
                    bucket.append(candidate)

    ordered_minors = sorted(
        candidates,
        key=lambda minor: (minor != 9, -minor),
    )
    for _minor in ordered_minors:
        for candidate in candidates[_minor]:
            try:
                actual = subprocess.check_output(
                    [candidate, "-c", (
                        "import sys, yaml; print('%d.%d' % "
                        "(sys.version_info[0], sys.version_info[1]))"
                    )],
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    timeout=5,
                ).strip()
                major, minor = (int(part) for part in actual.split(".", 1))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if (major, minor) >= (3, 9):
                return candidate
    return None

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
    _interp = _compatible_python_on_path()
    if _interp:
        # Only fixed interpreter/script paths become argv; user input remains
        # data in sys.argv[1:], never shell text. Bandit S606 is therefore safe.
        os.execv(  # noqa: S606
            _interp,
            [_interp, os.path.realpath(__file__)] + sys.argv[1:],
        )
    sys.stderr.write(
        "Eneru requires Python 3.9+, but this interpreter is %d.%d and no "
        "newer python3.x was found on PATH. On RHEL 8, install the python39 "
        "module (the el8 package depends on it).\n"
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
