"""Structural defense against PR #23-class bugs.

The deb / rpm builds enumerate every ``src/eneru/**/*.py`` in
``nfpm.yaml`` explicitly -- they do not glob. Pip CI passes silently
when a module is missing because ``pyproject.toml`` autodiscovers, so
the gap only surfaces at install time on Debian/Ubuntu/RHEL with a
``ModuleNotFoundError``.

These tests catch that class of mistake before it ships.
"""

import re
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
NFPM_YAML = REPO_ROOT / "nfpm.yaml"
PKG_ROOT = REPO_ROOT / "src" / "eneru"
WRAPPER = REPO_ROOT / "packaging" / "eneru-wrapper.py"


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


def _nfpm_all_src_paths() -> set:
    """Return every ``src: src/eneru/...`` path listed in nfpm.yaml."""
    text = NFPM_YAML.read_text()
    return set(re.findall(r"src:\s*(src/eneru/[^\s]+)", text))


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
            + "\nAdd a `contents:` entry per the convention in src/eneru/AGENTS.md."
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

    @pytest.mark.unit
    def test_dashboard_and_completion_assets_are_packaged(self):
        """deb/rpm and wheel installs must both ship importlib.resources data."""
        required = {
            "src/eneru/web/__init__.py",
            "src/eneru/web/index.html",
            "src/eneru/web/app.js",
            "src/eneru/web/style.css",
            "src/eneru/web/favicon.svg",
            "src/eneru/completion/__init__.py",
            "src/eneru/completion/eneru.bash",
            "src/eneru/completion/eneru.zsh",
            "src/eneru/completion/eneru.fish",
        }
        in_nfpm = _nfpm_all_src_paths()
        missing = sorted(required - in_nfpm)
        assert not missing, (
            "nfpm.yaml is missing package data files:\n  "
            + "\n  ".join(missing)
        )

        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        assert '"eneru.web" = ["*.html", "*.css", "*.js", "*.svg"]' in pyproject
        assert '"eneru.completion" = ["*.bash", "*.zsh", "*.fish"]' in pyproject

    @pytest.mark.unit
    def test_every_web_asset_extension_has_a_wheel_glob(self):
        """ISS-011: generalize the guard so the next non-py web asset can't drift.

        Every on-disk extension under src/eneru/web/ (except .py, shipped by the
        package itself) must be covered by an ``eneru.web`` package-data glob, so
        a wheel/pip install serves it exactly as deb/rpm does."""
        import re
        web_dir = REPO_ROOT / "src" / "eneru" / "web"
        exts = {
            p.suffix.lstrip(".").lower()
            for p in web_dir.iterdir()
            if p.is_file() and p.suffix and p.suffix != ".py"
        }
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        m = re.search(r'"eneru\.web"\s*=\s*\[([^\]]*)\]', pyproject)
        assert m, "eneru.web package-data glob list not found in pyproject.toml"
        globs = set(re.findall(r"\*\.([A-Za-z0-9]+)", m.group(1)))
        uncovered = sorted(exts - {g.lower() for g in globs})
        assert not uncovered, (
            "src/eneru/web/ has asset extension(s) not covered by an "
            f"eneru.web package-data glob (wheel installs would 404 them): "
            f"{uncovered}"
        )


class TestPackageWrapper:
    """The EL8 entry point must find future Python 3.x interpreters safely."""

    @staticmethod
    def _load_wrapper():
        spec = importlib.util.spec_from_file_location("eneru_pkg_wrapper", WRAPPER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.mark.unit
    def test_dynamic_interpreter_discovery_prefers_el8_python39(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wrapper = self._load_wrapper()
        for name in ("python3.9", "python3.14", "python3.15", "python3.8"):
            candidate = tmp_path / name
            candidate.touch(mode=0o755)
        versions = {
            "python3.15": "3.8\n",   # misleading executable: reject it
            "python3.14": "3.14\n",
            "python3.9": "3.9\n",
        }
        check = MagicMock(
            side_effect=lambda argv, **_kw: versions[Path(argv[0]).name],
        )
        monkeypatch.setattr(wrapper.subprocess, "check_output", check)

        selected = wrapper._compatible_python_on_path(str(tmp_path))

        assert selected == str(tmp_path / "python3.9")
        assert [Path(call.args[0][0]).name for call in check.call_args_list] == [
            "python3.9",
        ]

    @pytest.mark.unit
    def test_dynamic_interpreter_discovery_falls_back_to_future_python(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wrapper = self._load_wrapper()
        for name in ("python3.14", "python3.15"):
            (tmp_path / name).touch(mode=0o755)
        check = MagicMock(side_effect=["3.8\n", "3.14\n"])
        monkeypatch.setattr(wrapper.subprocess, "check_output", check)

        assert wrapper._compatible_python_on_path(str(tmp_path)) == str(
            tmp_path / "python3.14"
        )

    @pytest.mark.unit
    def test_same_version_candidate_without_runtime_deps_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wrapper = self._load_wrapper()
        custom = tmp_path / "custom"
        packaged = tmp_path / "packaged"
        custom.mkdir()
        packaged.mkdir()
        (custom / "python3.9").touch(mode=0o755)
        (packaged / "python3.9").touch(mode=0o755)
        check = MagicMock(side_effect=[
            wrapper.subprocess.CalledProcessError(1, "python3.9"),
            "3.9\n",
        ])
        monkeypatch.setattr(wrapper.subprocess, "check_output", check)

        selected = wrapper._compatible_python_on_path(
            f"{custom}{wrapper.os.pathsep}{packaged}"
        )

        assert selected == str(packaged / "python3.9")
        assert "import sys, yaml" in check.call_args_list[0].args[0][2]

    @pytest.mark.unit
    def test_wrapper_reexec_argv_preserves_script_and_user_args(self) -> None:
        source = WRAPPER.read_text()
        assert "python3.13\", \"python3.12" not in source
        assert "[_interp, os.path.realpath(__file__)] + sys.argv[1:]" in source


class TestReleaseWorkflowContracts:
    """Static guards for distro routing and published-package smoke checks."""

    @pytest.mark.unit
    def test_el8_uses_python39_for_dependencies_and_validation(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/integration.yml").read_text()
        assert "python3.9 -m pip install --no-deps paho-mqtt" in workflow
        assert '[ "${{ matrix.version }}" = "8" ] && PYTHON_BIN=python3.9' in workflow

    @pytest.mark.unit
    def test_release_routes_el8_docs_and_requires_exact_code_version(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        for path in (
            "rpm/eneru.repo",
            "rpm/el8/eneru-el8.repo",
            "rpm/testing/eneru-testing.repo",
            "rpm/testing/el8/eneru-testing-el8.repo",
        ):
            assert path in workflow
        assert 'test "$ACTUAL" = "Eneru v${VERSION_FULL}"' in workflow
        assert 'CORE="${VERSION%%-*}"' not in workflow

    @pytest.mark.unit
    def test_release_artifact_selection_is_counted_and_nullglob_safe(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        assert "shopt -s nullglob" in workflow
        assert 'debs=(../*.deb)' in workflow
        assert 'rpms=(../*.rpm)' in workflow
        assert 'for r in "${rpms[@]}"' in workflow
