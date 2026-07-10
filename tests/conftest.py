"""Shared test fixtures and configuration."""

import pytest
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from collections import deque

# Add src directory to path for eneru package imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
# Add tests directory to path for test_constants
sys.path.insert(0, str(Path(__file__).parent))

from test_constants import TEST_DISCORD_APPRISE_URL

from eneru import (
    Config,
    UPSConfig,
    UPSGroupConfig,
    TriggersConfig,
    DepletionConfig,
    ExtendedTimeConfig,
    BehaviorConfig,
    LoggingConfig,
    NotificationsConfig,
    VMConfig,
    ContainersConfig,
    FilesystemsConfig,
    UnmountConfig,
    RemoteServerConfig,
    LocalShutdownConfig,
    MonitorState,
    ConfigLoader,
)
from eneru import config as eneru_config_module
from eneru import stats as eneru_stats_module


@pytest.fixture(autouse=True)
def isolate_stats_db_directory(request, tmp_path, monkeypatch):
    """Redirect every test's StatsConfig.db_directory default and any
    direct StatsStore(db_path=...) call to a per-test tmp_path, so no
    test leaks SQLite files into the real /var/lib/eneru.

    Tests that specifically need to verify the unmodified production
    default (e.g. asserting StatsConfig().db_directory == "/var/lib/
    eneru") can opt out via @pytest.mark.no_stats_isolation.

    Two layers of defense are required when the fixture is active:

    1. ``StatsConfig.__init__`` — the dataclass-generated ``__init__``
       captures the literal default ``"/var/lib/eneru"`` at class
       decoration time, so monkeypatching the class attribute is a
       no-op for new instances. We replace ``__init__`` with a
       wrapper that substitutes the isolated path when the caller
       didn't supply one, regardless of how the dataclass was
       generated.

    2. ``StatsStore.__init__`` — direct ``StatsStore(Path("/var/lib/
       eneru/foo.db"))`` calls (e.g. via TUI helpers) bypass
       StatsConfig entirely. Redirect any ``db_path`` whose parent is
       ``/var/lib/eneru`` into the isolated dir so it lands in
       tmp_path instead.
    """
    if request.node.get_closest_marker("no_stats_isolation"):
        yield None
        return

    isolated = tmp_path / "stats"
    isolated.mkdir(parents=True, exist_ok=True)
    isolated_str = str(isolated)
    real_dir = Path("/var/lib/eneru")

    # Layer 1: StatsConfig default.
    # Use *args/**kw rather than baking `db_directory` into the
    # signature. Today db_directory is the first dataclass field so
    # `StatsConfig("/path")` works; if a future field is added before
    # it, a positional call would misroute. Detect whether the caller
    # actually supplied db_directory and only inject the isolated
    # default when they didn't.
    original_cfg_init = eneru_config_module.StatsConfig.__init__

    def patched_cfg_init(self, *args, **kw):
        if "db_directory" not in kw and not args:
            kw["db_directory"] = isolated_str
        return original_cfg_init(self, *args, **kw)

    monkeypatch.setattr(
        eneru_config_module.StatsConfig, "__init__", patched_cfg_init,
    )

    # Layer 2: direct StatsStore instantiation.
    original_store_init = eneru_stats_module.StatsStore.__init__

    def patched_store_init(self, db_path, *args, **kw):
        try:
            p = Path(db_path)
        except TypeError:
            return original_store_init(self, db_path, *args, **kw)
        if p.parent == real_dir:
            p = isolated / p.name
        return original_store_init(self, p, *args, **kw)

    monkeypatch.setattr(
        eneru_stats_module.StatsStore, "__init__", patched_store_init,
    )

    yield isolated


@pytest.fixture(autouse=True)
def block_journal_side_channels(request, monkeypatch):
    """Stop unit tests from leaking ``logger(1)`` / ``wall(1)`` output
    into the host's systemd journal and the ttys of every logged-in
    user on the dev box.

    Production code in ``monitor.py`` shells out to
    ``logger -t eneru -p daemon.warning ...`` for every power event and
    to ``wall`` for some user broadcasts (see lines 670, 1097, 1305,
    1464, 1594). In an isolated unit-test environment those shells
    actually execute on the developer's host and produce lines like::

        May 18 18:40:10 host eneru[NNN]: ⚡  POWER EVENT: ON_BATTERY ...

    in ``journalctl -u eneru`` on the dev box, mixed in with real
    operational logs. That's the kind of test pollution that's quietly
    confusing to debug — the events look real.

    Intercept every ``run_command`` binding (mirroring the targets in
    ``patch_run_command_everywhere``) and short-circuit calls whose
    first arg is ``logger`` or ``wall`` to ``(0, "", "")``. All other
    calls pass through to the real implementation so tests that depend
    on real subprocess behavior keep working. Opt-out via
    ``@pytest.mark.no_journal_isolation`` if a test needs to assert the
    real logger/wall shell-out path.
    """
    if request.node.get_closest_marker("no_journal_isolation"):
        yield
        return

    from eneru import utils as _utils_module
    real_run_command = _utils_module.run_command

    BLOCKED = {"logger", "wall"}

    def wrapped_run_command(cmd, *args, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] in BLOCKED:
            return (0, "", "")
        return real_run_command(cmd, *args, **kw)

    # Same target set as patch_run_command_everywhere: each module that
    # did `from eneru.utils import run_command` binds the symbol at
    # import time, so patching only eneru.utils is a no-op for them.
    targets = [
        "eneru.utils.run_command",
        "eneru.monitor.run_command",
        "eneru.multi_ups.run_command",
        "eneru.shutdown.vms.run_command",
        "eneru.shutdown.containers.run_command",
        "eneru.shutdown.filesystems.run_command",
        "eneru.shutdown.remote.run_command",
    ]
    for target in targets:
        try:
            monkeypatch.setattr(target, wrapped_run_command)
        except AttributeError:
            # Module exists but didn't import run_command — skip.
            pass

    yield


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """ISS-032: the API login throttle is process-global module state; clear it
    around every test so failed-login tests can't bleed into unrelated ones.

    cubic P2: no blanket try/except — `eneru.api` is a core module and
    `_login_failures` is module-level state, so an ImportError or a rename
    SHOULD fail loudly here (a swallowed error would silently drop throttle
    isolation from every test), matching the other autouse fixtures above."""
    import eneru.api as _api
    _api._login_failures.clear()
    _api._global_login_failures.clear()  # F-040: reset the global ceiling too
    yield
    _api._login_failures.clear()
    _api._global_login_failures.clear()


@pytest.fixture(autouse=True)
def _reset_runtime_context_cache():
    """F-049: _detect_runtime_context is now lru_cache-memoized (its inputs —
    container/runtime identity — are fixed for a process's life). But tests fake
    those inputs (patching /proc, /.dockerenv, env vars) to simulate DIFFERENT
    runtimes, and a cached fake would otherwise bleed into unrelated tests: e.g.
    a test that primed the cache with a container label would make the monitor's
    wall(1)/logger checks think they're containerized. Clear it around every
    test so each starts from a clean cache — same isolation contract as the
    login-throttle reset above."""
    from eneru.runtime import _detect_runtime_context
    _detect_runtime_context.cache_clear()
    yield
    _detect_runtime_context.cache_clear()


def make_api_handler(
    config: Any,
    *,
    source: Any = None,
    path: str = "/",
    method: Optional[str] = None,
    body: bytes = b"",
    token: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    auth_store: Any = None,
    sessions: Any = None,
    logs: Optional[List[str]] = None,
) -> Any:
    """Construct a bare ``EneruAPIHandler`` for unit tests (F-063).

    ELI5: five different test files each hand-rolled the same "fake HTTP
    handler" — same object, five slightly-different recipes. When one
    recipe needed a tweak (e.g. Commit 4's ``Host: localhost`` default),
    the others silently drifted. This is the one shared kitchen: every
    caller orders from the same menu, so a change lands everywhere at
    once.

    The handler is built with ``object.__new__`` (bypassing the real
    ``BaseHTTPRequestHandler.__init__``, which wants a live socket) and
    seeded with just the attributes the routing/auth code reads.

    Parameters mirror the union of the old builders:

    - ``source``     -> ``api_source`` (defaults to a ``MagicMock``)
    - ``path``       -> ``h.path`` (request path)
    - ``method``     -> ``h.command`` (HTTP verb), only if provided
    - ``body``       -> request body bytes (also the ``rfile`` stream)
    - ``token``      -> adds ``Authorization: Bearer <token>``
    - ``headers``    -> explicit header dict (see F-016 note below)
    - ``auth_store`` -> ``api_auth``
    - ``sessions``   -> ``api_sessions``
    - ``logs``       -> ``api_log`` appends here when a list is passed

    F-016: when ``headers`` is left unset we default ``Host: localhost``
    so tests that drive ``do_*()`` / ``_dispatch`` / ``_route`` clear the
    DNS-rebinding guard. Passing an explicit ``headers`` dict takes full
    control — including omitting ``Host`` on purpose to exercise the 421
    reject path.
    """
    from eneru.api import EneruAPIHandler

    h = object.__new__(EneruAPIHandler)
    h.path = path
    h.api_config = config
    h.api_source = source if source is not None else MagicMock()
    h.api_auth = auth_store
    h.api_sessions = sessions
    h.api_log = logs.append if logs is not None else (lambda m: None)
    if method is not None:
        h.command = method
    if headers is None:
        hdrs = {"Host": "localhost"}
    else:
        hdrs = dict(headers)
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body and "Content-Length" not in hdrs:
        hdrs["Content-Length"] = str(len(body))
    h.headers = hdrs
    h.rfile = BytesIO(body)
    return h


@pytest.fixture
def api_handler_factory() -> Any:
    """Fixture wrapper around :func:`make_api_handler` (F-063)."""
    return make_api_handler


@pytest.fixture
def default_config() -> Config:
    """Create a default configuration for testing."""
    return Config(ups_groups=[UPSGroupConfig()])


@pytest.fixture
def minimal_config() -> Config:
    """Create a minimal configuration."""
    return Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="TestUPS@localhost"),
            virtual_machines=VMConfig(enabled=False),
            containers=ContainersConfig(enabled=False),
            filesystems=FilesystemsConfig(unmount=UnmountConfig(enabled=False)),
            is_local=True,
        )],
        behavior=BehaviorConfig(dry_run=True),
        notifications=NotificationsConfig(enabled=False),
        local_shutdown=LocalShutdownConfig(enabled=False),
    )


@pytest.fixture
def full_config() -> Config:
    """Create a fully-configured configuration for testing."""
    return Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(
                name="UPS@192.168.1.100",
                check_interval=1,
                max_stale_data_tolerance=3,
            ),
            triggers=TriggersConfig(
                low_battery_threshold=20,
                critical_runtime_threshold=600,
                depletion=DepletionConfig(
                    window=300,
                    critical_rate=15.0,
                    grace_period=90,
                ),
                extended_time=ExtendedTimeConfig(
                    enabled=True,
                    threshold=900,
                ),
            ),
            virtual_machines=VMConfig(enabled=True, max_wait=30),
            containers=ContainersConfig(
                enabled=True,
                runtime="auto",
                stop_timeout=60,
            ),
            filesystems=FilesystemsConfig(
                sync_enabled=True,
                unmount=UnmountConfig(
                    enabled=True,
                    timeout=15,
                    mounts=[
                        {"path": "/mnt/test1", "options": ""},
                        {"path": "/mnt/test2", "options": "-l"},
                    ],
                ),
            ),
            remote_servers=[
                RemoteServerConfig(
                    name="Test Server",
                    enabled=True,
                    host="192.168.1.50",
                    user="admin",
                    shutdown_command="sudo shutdown -h now",
                ),
            ],
            is_local=True,
        )],
        behavior=BehaviorConfig(dry_run=True),
        notifications=NotificationsConfig(
            enabled=True,
            urls=[TEST_DISCORD_APPRISE_URL],
            title="Test UPS",
            timeout=10,
        ),
        local_shutdown=LocalShutdownConfig(
            enabled=True,
            command="shutdown -h now",
            message="Test shutdown",
        ),
    )


@pytest.fixture
def monitor_state() -> MonitorState:
    """Create a fresh monitor state."""
    return MonitorState()


@pytest.fixture
def temp_config_file(tmp_path) -> Path:
    """Create a temporary config file."""
    config_file = tmp_path / "config.yaml"
    return config_file


@pytest.fixture
def sample_ups_data() -> Dict[str, str]:
    """Sample UPS data as returned by upsc."""
    return {
        "ups.status": "OL CHRG",
        "battery.charge": "100",
        "battery.runtime": "1800",
        "ups.load": "25",
        "input.voltage": "230.5",
        "output.voltage": "230.0",
        "input.voltage.nominal": "230",
        "input.transfer.low": "170",
        "input.transfer.high": "280",
    }


@pytest.fixture
def sample_ups_data_on_battery() -> Dict[str, str]:
    """Sample UPS data when on battery."""
    return {
        "ups.status": "OB DISCHRG",
        "battery.charge": "85",
        "battery.runtime": "1200",
        "ups.load": "30",
        "input.voltage": "0.0",
        "output.voltage": "230.0",
    }


@pytest.fixture
def mock_run_command():
    """Mock the run_command function."""
    with patch("eneru.monitor.run_command") as mock:
        mock.return_value = (0, "", "")
        yield mock


@pytest.fixture
def patch_run_command_everywhere():
    """Patch ``run_command`` in every module that imported it under its
    own name. ``from eneru.utils import run_command`` binds the symbol
    at import time, so ``patch("eneru.utils.run_command")`` is a no-op
    for already-imported modules — tests that go through a shutdown
    mixin (vms/containers/filesystems/remote) must patch each binding
    explicitly or the mixin's call will hit real ``virsh``/``umount``/
    ``ssh`` despite the test's intent.

    Yields a dict mapping the module path → MagicMock so a test can
    assert against any specific binding. Each mock returns
    ``(0, "", "")`` by default; override per-test as needed.
    """
    targets = [
        # eneru.utils is the home of `run_command`; patching it here too
        # catches indirect callers that resolve through utils at call
        # time (e.g. command_exists() in eneru.utils, which would
        # otherwise still shell out during shutdown tests).
        "eneru.utils.run_command",
        "eneru.monitor.run_command",
        "eneru.multi_ups.run_command",
        "eneru.shutdown.vms.run_command",
        "eneru.shutdown.containers.run_command",
        "eneru.shutdown.filesystems.run_command",
        "eneru.shutdown.remote.run_command",
    ]
    patchers = [patch(t) for t in targets]
    mocks = {t: p.start() for t, p in zip(targets, patchers)}
    for m in mocks.values():
        m.return_value = (0, "", "")
    try:
        yield mocks
    finally:
        for p in patchers:
            p.stop()


@pytest.fixture
def mock_apprise():
    """Mock the Apprise library."""
    with patch("eneru.notifications.apprise") as mock:
        mock_instance = MagicMock()
        mock.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.notify.return_value = True
        mock_instance.__len__ = lambda self: 1
        yield mock
