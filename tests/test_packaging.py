"""Structural defense against PR #23-class bugs.

The deb / rpm builds enumerate every ``src/eneru/**/*.py`` in
``nfpm.yaml`` explicitly -- they do not glob. Pip CI passes silently
when a module is missing because ``pyproject.toml`` autodiscovers, so
the gap only surfaces at install time on Debian/Ubuntu/RHEL with a
``ModuleNotFoundError``.

These tests catch that class of mistake before it ships.
"""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
NFPM_YAML = REPO_ROOT / "nfpm.yaml"
PKG_ROOT = REPO_ROOT / "src" / "eneru"


def _all_eneru_modules() -> set:
    """Return every ``.py`` in ``src/eneru/`` as a relative POSIX path."""
    return {
        p.relative_to(REPO_ROOT).as_posix()
        for p in PKG_ROOT.rglob("*.py")
    }


def _nfpm_src_paths() -> set:
    """Return every ``src:`` line that points to a ``.py`` under ``src/eneru/``."""
    text = NFPM_YAML.read_text()
    # Match `  - src: src/eneru/...py` or `    src: src/eneru/...py`.
    return set(re.findall(r"src:\s*(src/eneru/[^\s]+\.py)", text))


class TestNfpmModuleListing:

    @pytest.mark.unit
    def test_every_python_module_is_listed(self):
        """Every ``src/eneru/**/*.py`` must appear in nfpm.yaml's contents."""
        on_disk = _all_eneru_modules()
        in_nfpm = _nfpm_src_paths()
        missing = sorted(on_disk - in_nfpm)
        assert not missing, (
            f"Modules present in src/eneru/ but missing from nfpm.yaml:\n  "
            + "\n  ".join(missing)
            + "\nAdd a `contents:` entry per the convention in src/eneru/CLAUDE.md."
        )

    @pytest.mark.unit
    def test_no_dangling_src_paths_in_nfpm(self):
        """Every src: src/eneru/...py reference must exist on disk."""
        in_nfpm = _nfpm_src_paths()
        missing_files = sorted(
            p for p in in_nfpm if not (REPO_ROOT / p).exists()
        )
        assert not missing_files, (
            f"nfpm.yaml references files that don't exist:\n  "
            + "\n  ".join(missing_files)
        )

    @pytest.mark.unit
    def test_nfpm_creates_var_lib_eneru_directory(self):
        """The deb/rpm package must create /var/lib/eneru for the stats DBs.

        Pip installs handle this defensively in StatsStore.open(); deb/rpm
        rely on the directory entry being present in nfpm.yaml.
        """
        text = NFPM_YAML.read_text()
        # Look for the directory entry (dst: /var/lib/eneru, type: dir).
        match = re.search(
            r"dst:\s*/var/lib/eneru\s*\n\s*type:\s*dir",
            text,
        )
        assert match, (
            "nfpm.yaml does not declare /var/lib/eneru as a directory entry. "
            "Stats databases are written there; the deb/rpm package must "
            "create the directory at install time."
        )
