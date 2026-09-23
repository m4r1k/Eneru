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
    """The package entry point refuses pre-3.9 interpreters with a clear hint."""

    @staticmethod
    def _load_wrapper():
        spec = importlib.util.spec_from_file_location("eneru_pkg_wrapper", WRAPPER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.mark.unit
    def test_wrapper_loads_on_supported_interpreter(self) -> None:
        wrapper = self._load_wrapper()
        assert callable(wrapper._main)

    @pytest.mark.unit
    def test_wrapper_rejects_old_python_without_reexec(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        import sys
        monkeypatch.setattr(sys, "version_info", (3, 6, 8, "final", 0))
        with pytest.raises(SystemExit) as exc:
            self._load_wrapper()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "requires Python 3.9+" in err
        assert "container image" in err

    @pytest.mark.unit
    def test_wrapper_has_no_el8_interpreter_discovery(self) -> None:
        source = WRAPPER.read_text()
        assert "python39" not in source
        assert "os.execv" not in source


class TestReleaseWorkflowContracts:
    """Static guards for distro routing and published-package smoke checks."""

    @pytest.mark.unit
    def test_rhel8_is_no_longer_built_or_tested(self) -> None:
        integration = (REPO_ROOT / ".github/workflows/integration.yml").read_text()
        release = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        for workflow in (integration, release):
            assert "ubi8" not in workflow
            assert "nfpm-el8.yaml" not in workflow
            assert "eneru-el8.repo" not in workflow

    @pytest.mark.unit
    def test_release_keeps_frozen_el8_repo_out_of_default_metadata(self) -> None:
        """Existing 6.1.x el8 RPMs stay on gh-pages but must never be indexed
        into the RHEL 9/10 repo (they require the python39 module)."""
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        assert "createrepo_c --excludes='testing/*' --excludes='el8/*' ." in workflow
        assert workflow.count("--excludes='el8/*'") == 2

    @pytest.mark.unit
    def test_release_routes_docs_and_requires_exact_code_version(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        for path in (
            "rpm/eneru.repo",
            "rpm/testing/eneru-testing.repo",
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

    @pytest.mark.unit
    def test_release_oci_build_exports_name_and_version_metadata(self) -> None:
        """The release image tag and OCI version label must not be empty/stale."""
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        image_name_step = workflow.split("- name: Prepare IMAGE_NAME", 1)[1].split(
            "- name: Prepare version.py", 1
        )[0]
        build_step = workflow.split("- name: Build and push", 1)[1].split(
            "- name: Verify Multi-Arch Manifest", 1
        )[0]

        assert 'echo "IMAGE_NAME=$IMAGE_NAME" >> "$GITHUB_OUTPUT"' in image_name_step
        assert "$GITHUB_ENV" not in image_name_step
        assert "build-args:" in build_step
        assert "VERSION=${{ steps.version.outputs.VERSION }}" in build_step

    @pytest.mark.unit
    def test_integration_uses_native_arm64_and_required_aggregator(self) -> None:
        """PR CI must gate both native architectures without QEMU emulation."""
        workflow = (REPO_ROOT / ".github/workflows/integration.yml").read_text()
        amd64_job = workflow.split("  test-oci-image-amd64:\n", 1)[1].split(
            "\n  test-oci-image-arm64:\n", 1
        )[0]
        arm64_job = workflow.split("  test-oci-image-arm64:\n", 1)[1].split(
            "\n  test-oci-image:\n", 1
        )[0]
        required_gate = workflow.split("\n  test-oci-image:\n", 1)[1]

        assert "runs-on: ubuntu-latest" in amd64_job
        assert "Verify OCI image with Podman" in amd64_job
        assert "dashboard asset present in image" in amd64_job

        assert "runs-on: ubuntu-24.04-arm" in arm64_job
        assert "docker/setup-qemu-action@" not in arm64_job
        assert "docker/setup-buildx-action@" not in arm64_job
        assert "docker/build-push-action@" not in arm64_job
        assert "docker build --build-arg VERSION=" in arm64_job
        assert "validate --config /etc/ups-monitor/config.yaml" in arm64_job
        assert "import bcrypt" in arm64_job
        assert "org.opencontainers.image.version" in arm64_job
        assert 'test "$ARCH" = "arm64"' in arm64_job

        assert "test-oci-image-amd64" in required_gate
        assert "test-oci-image-arm64" in required_gate
        assert "if: always()" in required_gate
        assert 'test "$AMD64_RESULT" = "success"' in required_gate
        assert 'test "$ARM64_RESULT" = "success"' in required_gate


class TestGithubActionReferences:
    """Keep action dependencies readable under the repository's tag policy."""

    @pytest.mark.unit
    def test_third_party_actions_do_not_use_commit_shas(self) -> None:
        """Every third-party ``uses:`` reference must avoid opaque SHA refs."""
        action_files = sorted((REPO_ROOT / ".github").rglob("*.yml"))
        action_files += sorted((REPO_ROOT / ".github").rglob("*.yaml"))
        sha_ref = re.compile(r"^\s*(?:-\s*)?uses:\s*[^\s#]+@[0-9a-f]{40}(?:\s|$)")
        pinned = []

        for path in action_files:
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if sha_ref.match(line):
                    pinned.append(
                        f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}"
                    )

        assert not pinned, "Opaque GitHub Action SHA references remain:\n" + "\n".join(
            pinned
        )
