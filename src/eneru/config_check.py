"""`eneru config check`: inspect an Eneru config the way a building inspector would.

ELI5: the daemon is the building's tenant. It moves in, starts the lights, and
only then notices the fridge has no plug, the back door key doesn't fit, and a
note on the wall says "dry run". The inspector walks the building BEFORE move-in
day with a checklist and a torch: it reads the plans (YAML + validation), tries
every key in every door (NUT login, SSH, sudo), and hands back one list with red
(blocks startup), yellow (you will regret this later), and green (checked, fine).

Three layers, all read-only:

* **static** -- YAML parse, the loader's own validation (``validate_config``),
  the same startup preparation ``eneru run`` does (loopback synthesis, container
  and Kubernetes notices), privilege and binary dependency checks, and the one-off
  startup warnings operators tend to miss in the log (dry-run left on, API on a
  plain-HTTP LAN bind, MQTT without TLS, a shutdown slower than the runtime
  budget, ...).
* **probes** -- live checks that only READ: ``upsc`` / ``upscmd -l`` against NUT,
  one harmless SSH session per remote, ``command -v`` for every binary a shutdown
  step needs, a read-only listing (``docker ps``, ``virsh list``, ...) for known
  actions, and ``sudo -n -l <binary>`` to prove NOPASSWD sudo WITHOUT running the
  binary. Shutdown commands and custom commands are never executed.
* **preview** -- the "what happens on power loss" timeline, built from the same
  ``build_shutdown_plan`` the dashboard uses.

The TUI (``eneru config``) reuses these functions stage by stage.
"""

import contextlib
import io
import os
import re
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from eneru import runtime as _runtime_ctx
from eneru.config import (
    Config,
    ConfigLoader,
    ConfigSectionError,
    RemoteServerConfig,
    UPSGroupConfig,
    is_validation_error,
    resolve_energy_config,
)
from eneru.utils import command_exists, format_seconds, is_numeric, run_command

# Finding levels, most severe first.
LEVEL_ERROR = "error"
LEVEL_WARN = "warning"
LEVEL_INFO = "info"
LEVEL_OK = "ok"
LEVELS = (LEVEL_ERROR, LEVEL_WARN, LEVEL_INFO, LEVEL_OK)

# Report sections. The TUI maps its stages onto these keys.
SECTION_TITLES: Dict[str, str] = {
    "file": "Configuration file",
    "ups": "UPS & NUT",
    "safety": "Safety & shutdown triggers",
    "local": "This host (VMs, containers, filesystems)",
    "remote": "Remote servers",
    "redundancy": "Redundancy groups",
    "notifications": "Notifications",
    "features": "API, MQTT, statistics & other features",
    "runtime": "Runtime & dependencies",
}
SECTIONS = tuple(SECTION_TITLES)

# Slow-NUT threshold mirrors the daemon's slow-poll warning scale.
SLOW_NUT_MS = 2_000
# Per remote check budget (seconds) inside the one SSH session.
REMOTE_CHECK_TIMEOUT = 20
# Marker the remote script prints per check; unlikely to collide with output.
_REMOTE_MARKER = "__ENERU_CHECK__"

# Re-exported so tests and the TUI can patch one place.
_run = run_command


@dataclass
class Finding:
    """One inspection result."""

    level: str
    section: str
    message: str
    hint: str = ""
    # What the finding is about (UPS label, remote name, ...). Lets the TUI show
    # only the findings for the item the operator is editing.
    subject: str = ""


@dataclass
class CheckReport:
    """Everything `eneru config check` found, plus the power-loss preview."""

    path: Optional[str] = None
    findings: List[Finding] = field(default_factory=list)
    plan: List[str] = field(default_factory=list)

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f.level == level)

    @property
    def has_errors(self) -> bool:
        return self.count(LEVEL_ERROR) > 0


# ---------------------------------------------------------------------------
# Section classification for loader validation messages
# ---------------------------------------------------------------------------

# Ordered: the first matching rule wins. `remote_health` must beat `remote`,
# and trigger/local-resource keys must beat the `ups[...]` prefix they sit in.
_SECTION_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("features", re.compile(r"remote_health")),
    ("redundancy", re.compile(r"[Rr]edundancy")),
    ("remote", re.compile(
        r"remote_server|Remote server|pre_shutdown|shutdown_order|"
        r"is_host_loopback|host_identity|ssh_|use_sudo")),
    ("local", re.compile(
        r"virtual_machines|containers|filesystems|compose|unmount|\bdocker\b")),
    ("safety", re.compile(
        r"triggers|local_shutdown|behavior|dry_run|low_battery|"
        r"critical_runtime|depletion|extended_time|stabiliz")),
    ("notifications", re.compile(
        r"[Nn]otification|apprise|[Dd]iscord|voltage_hysteresis")),
    ("features", re.compile(
        r"\bapi\b|\bauth\b|mqtt|prometheus|statistics|reports|energy|"
        r"logging|syslog")),
    ("ups", re.compile(
        r"\bups\b|UPS|nut_control|self_test|battery_health|check_interval|"
        r"max_stale|connection_loss")),
)


def classify_message(message: str) -> str:
    """Map a free-form validation message onto a report section."""
    for section, pattern in _SECTION_RULES:
        if pattern.search(message):
            return section
    return "file"


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")


def clean(text: Any) -> str:
    """Strip terminal escapes/control characters from EXTERNAL output.

    Remote stderr or a NUT banner is printed to the operator's terminal; a
    compromised target must not be able to repaint it.
    """
    text = _ANSI.sub("", str(text))
    return "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in text)


def _strip_level(message: str) -> str:
    for prefix in ("ERROR:", "WARNING:", "INFO:"):
        if message.startswith(prefix):
            return message[len(prefix):].strip()
    return message.strip()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_raw(path: str) -> Tuple[Optional[dict], List[Finding]]:
    """Read + YAML-parse ``path``. Returns (mapping or None, findings)."""
    findings: List[Finding] = []
    p = Path(path)
    if not p.exists():
        findings.append(Finding(
            LEVEL_ERROR, "file", f"Config file not found: {path}",
            "Create one with `eneru config` or copy examples/config-reference.yaml."))
        return None, findings
    try:
        import yaml
        with open(p, "r") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        findings.append(Finding(
            LEVEL_ERROR, "file", f"YAML parse error in {path}: {exc}",
            "Fix the indentation/quoting at the reported line."))
        return None, findings
    if data is None:
        data = {}
    if not isinstance(data, dict):
        findings.append(Finding(
            LEVEL_ERROR, "file", f"Config root in {path} must be a YAML mapping."))
        return None, findings
    findings.append(Finding(LEVEL_OK, "file", f"YAML parsed: {path}"))
    return data, findings


def build_config(data: dict, *, path: Optional[str] = None
                 ) -> Tuple[Optional[Config], List[Finding]]:
    """Turn a raw mapping into a Config exactly as the daemon would."""
    findings: List[Finding] = []
    struct = ConfigLoader._schema_structural_errors(data)
    if struct:
        for msg in struct:
            findings.append(Finding(
                LEVEL_ERROR, classify_message(msg), _strip_level(msg)))
        return None, findings
    try:
        config = ConfigLoader._parse_config(data)
    except ConfigSectionError as exc:
        for msg in str(exc).splitlines():
            findings.append(Finding(
                LEVEL_ERROR, classify_message(msg), _strip_level(msg)))
        return None, findings
    except Exception as exc:  # defensive: parser crash is itself a finding
        findings.append(Finding(
            LEVEL_ERROR, "file", f"Config could not be parsed: {exc}"))
        return None, findings
    config.config_path = path
    from eneru import cli
    cli._rewrite_legacy_paths_for_container(config)
    return config, findings


@contextlib.contextmanager
def _captured_output():
    """Capture stdout+stderr prints from reused CLI helpers."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _findings_from_printed(text: str, default_section: str) -> List[Finding]:
    """Convert CLI helper prints (WARNING:/ERROR:/banner lines) to findings."""
    out: List[Finding] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("ERROR"):
            level = LEVEL_ERROR
        elif line.startswith("WARNING"):
            level = LEVEL_WARN
        else:
            level = LEVEL_INFO
        # Continuation lines ("  - reason") attach to the previous finding.
        if raw.startswith("  ") and out:
            out[-1].hint = (out[-1].hint + " " + line).strip()
            continue
        out.append(Finding(level, default_section, _strip_level(line)))
    return out


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def _all_groups(config: Config) -> List[Any]:
    return list(config.ups_groups) + list(config.redundancy_groups)


def _group_label(group: Any) -> str:
    ups = getattr(group, "ups", None)
    if ups is not None:
        return ups.label
    return f"redundancy group '{getattr(group, 'name', '') or '(unnamed)'}'"


def _redundancy_members(config: Config) -> Dict[str, str]:
    members: Dict[str, str] = {}
    for rg in config.redundancy_groups:
        for src in rg.ups_sources:
            members.setdefault(src, rg.name or "(unnamed)")
    return members


def poweroff_binary(command: str) -> Optional[str]:
    """argv[0] of a local poweroff command (parsed like the shutdown path)."""
    try:
        from eneru.monitor import poweroff_command_parts
        parts = poweroff_command_parts(command)
    except Exception:
        return None
    return parts[0] if parts else None


def _dependency_findings(config: Config) -> List[Finding]:
    """Mirror monitor._check_dependencies for every group, ahead of time."""
    out: List[Finding] = []
    runtime = _runtime_ctx._detect_runtime_context()
    in_container = _runtime_ctx._is_container_runtime(runtime)

    if command_exists("upsc"):
        out.append(Finding(LEVEL_OK, "runtime", "upsc (nut-client) is installed"))
    else:
        out.append(Finding(
            LEVEL_ERROR, "runtime", "Required command 'upsc' not found",
            "Install the NUT client package (nut-client / nut) - Eneru polls "
            "the UPS through it and refuses to start without it."))

    owner = _runtime_ctx._local_owner_group(config)
    delegating = _runtime_ctx._uses_loopback_delegate(config)
    implicit_local = not config.ups_groups and not config.redundancy_groups
    if ((owner is not None or implicit_local) and config.local_shutdown.enabled
            and not delegating):
        binary = poweroff_binary(config.local_shutdown.command)
        if binary and not command_exists(binary):
            out.append(Finding(
                LEVEL_ERROR, "safety",
                f"local_shutdown.command binary '{binary}' not found",
                "The daemon treats a missing poweroff binary as FATAL at "
                "startup. Fix local_shutdown.command or install the tool."))
    if delegating and not command_exists("ssh"):
        out.append(Finding(
            LEVEL_ERROR, "runtime",
            "'ssh' not found but local actions are delegated over the "
            "host-loopback SSH target"))
    if not in_container and not command_exists("logger"):
        out.append(Finding(
            LEVEL_WARN, "runtime", "'logger' not found",
            "Power events are still written to Eneru's own log; only the "
            "legacy syslog side-channel is skipped."))

    for group in _all_groups(config):
        label = _group_label(group)
        local = bool(getattr(group, "is_local", False)) or (
            group is (config.ups_groups[0] if config.ups_groups else None)
            and not config.multi_ups)
        if local and not delegating:
            if group.virtual_machines.enabled and not command_exists("virsh"):
                out.append(Finding(
                    LEVEL_WARN, "local",
                    f"{label}: 'virsh' not found but VM shutdown is enabled",
                    "The daemon disables the VM phase at startup: running VMs "
                    "would NOT be stopped. Install libvirt clients or disable it.",
                    subject=label))
            if group.containers.enabled:
                rt = group.containers.runtime
                candidates = ["podman", "docker"] if rt == "auto" else [rt]
                found = [c for c in candidates if command_exists(c)]
                if not found:
                    out.append(Finding(
                        LEVEL_WARN, "local",
                        f"{label}: no container runtime found "
                        f"({' / '.join(candidates)})",
                        "The daemon disables the container phase at startup.",
                        subject=label))
        servers = [s for s in group.remote_servers if s.enabled]
        if servers and not command_exists("ssh"):
            out.append(Finding(
                LEVEL_WARN, "remote",
                f"{label}: 'ssh' not found but remote servers are configured",
                "The daemon disables every remote shutdown at startup. "
                "Install the OpenSSH client.", subject=label))
    return out


def _optional_module_findings(config: Config) -> List[Finding]:
    out: List[Finding] = []
    if config.notifications.enabled and config.notifications.urls:
        try:
            import apprise  # noqa: F401
            out.append(Finding(
                LEVEL_OK, "notifications",
                f"Apprise installed; {len(config.notifications.urls)} "
                "notification URL(s) configured"))
        except ImportError:
            out.append(Finding(
                LEVEL_ERROR, "notifications",
                "Notifications are configured but the 'apprise' package is "
                "not installed", "Install apprise (deb: apprise, pip: "
                "eneru[notifications]); until then nothing is delivered."))
    if config.api.enabled and config.api.auth.enabled:
        try:
            import bcrypt  # noqa: F401
        except ImportError:
            out.append(Finding(
                LEVEL_ERROR, "features",
                "api.auth is enabled but 'bcrypt' is not installed",
                "Install python3-bcrypt or `pip install 'eneru[auth]'`."))
    if config.mqtt.enabled:
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            out.append(Finding(
                LEVEL_WARN, "features",
                "MQTT is enabled but 'paho-mqtt' is not installed",
                "The publisher disables itself at startup. Install "
                "python3-paho-mqtt or `pip install 'eneru[mqtt]'`."))
    return out


def _is_loopback_bind(bind: str) -> bool:
    return bind in ("127.0.0.1", "::1", "localhost") or bind.startswith("127.")


def _feature_findings(config: Config) -> List[Finding]:
    """One-off startup log lines that are easy to miss."""
    out: List[Finding] = []
    if config.api.enabled and not _is_loopback_bind(str(config.api.bind)):
        out.append(Finding(
            LEVEL_WARN, "features",
            f"API binds to {config.api.bind} over plain HTTP",
            "Logins and tokens cross the network unencrypted. Front it with "
            "a TLS reverse proxy or keep api.bind on 127.0.0.1."))
        if not config.api.auth.enabled:
            out.append(Finding(
                LEVEL_INFO, "features",
                "API is reachable off-host with auth disabled",
                "Read endpoints (status, topology, events) are open to "
                "anyone who can reach the port; writes stay closed."))
    if config.mqtt.enabled:
        broker = str(config.mqtt.broker or "")
        if broker and "://" not in broker:
            out.append(Finding(
                LEVEL_WARN, "features",
                f"MQTT broker '{broker}' has no mqtt:// or mqtts:// scheme",
                "Host/port fall back to the raw string and TLS stays off. "
                "Use mqtt://host:1883 or mqtts://host:8883."))
        elif broker.startswith("mqtt://") and "@" in broker.split("://", 1)[1]:
            out.append(Finding(
                LEVEL_WARN, "features",
                "MQTT credentials are set on a non-TLS broker",
                "The password is sent in cleartext. Use mqtts://host:8883."))
    return out


def _behavior_findings(config: Config) -> List[Finding]:
    out: List[Finding] = []
    if config.behavior.dry_run:
        out.append(Finding(
            LEVEL_WARN, "safety", "Dry-run is ON: nothing will be shut down",
            "Every shutdown step is only logged. Turn behavior.dry_run off "
            "once you have rehearsed the sequence."))
    else:
        out.append(Finding(
            LEVEL_OK, "safety",
            "Dry-run is off: shutdown steps execute for real"))

    owner = _runtime_ctx._local_owner_group(config)
    legacy_local = bool(config.ups_groups) and not config.multi_ups
    protects_host = owner is not None or legacy_local
    if protects_host and not config.local_shutdown.enabled:
        out.append(Finding(
            LEVEL_WARN, "safety",
            "A UPS powers this host but local_shutdown is disabled",
            "Eneru will drain VMs/containers/remotes, then leave this host "
            "running until the battery dies."))
    if config.multi_ups and owner is None:
        if config.local_shutdown.enabled and config.local_shutdown.trigger_on == "any":
            out.append(Finding(
                LEVEL_WARN, "safety",
                "No UPS is marked is_local, yet any group's shutdown powers "
                "off this host (local_shutdown.trigger_on: any)",
                "If this host has independent power, set trigger_on: none; "
                "otherwise mark the UPS that feeds it with is_local: true."))
        else:
            out.append(Finding(
                LEVEL_INFO, "safety",
                "Monitoring/remote-only: this host never powers itself off"))

    has_work = any(
        [s for s in g.remote_servers if s.enabled]
        or g.virtual_machines.enabled or g.containers.enabled
        or g.filesystems.unmount.enabled
        for g in _all_groups(config))
    if not has_work and not (protects_host and config.local_shutdown.enabled):
        out.append(Finding(
            LEVEL_INFO, "safety",
            "Monitoring-only config: no VMs, containers, remotes or local "
            "poweroff are configured"))

    if not (config.notifications.enabled and config.notifications.urls):
        out.append(Finding(
            LEVEL_INFO, "notifications",
            "No notification service configured",
            "Power events only reach the log. Add an Apprise URL "
            "(Discord, ntfy, Telegram, e-mail, ...) to be told about outages."))
    return out


def _estimate_seconds(config: Config, group: Any) -> Optional[float]:
    """Rough worst-case drain time for one group (sum of phase budgets)."""
    plan = _plan_for_group(config, group)
    return plan.get("totalEstimateS") if plan else None


def _budget_findings(config: Config) -> List[Finding]:
    """Flag a shutdown sequence that can outlast the runtime trigger."""
    out: List[Finding] = []
    for group in config.ups_groups:
        est = _estimate_seconds(config, group)
        threshold = group.triggers.critical_runtime_threshold
        if not est or not is_numeric(threshold):
            continue
        if est > float(threshold):
            out.append(Finding(
                LEVEL_WARN, "safety",
                f"{group.ups.label}: the shutdown sequence may need up to "
                f"{format_seconds(est)}, but the runtime trigger fires with "
                f"only {format_seconds(threshold)} of battery left",
                "Raise triggers.critical_runtime_threshold or shorten the "
                "VM/container/remote timeouts, or the host may lose power "
                "mid-sequence.", subject=group.ups.label))
    return out


def _path_findings(config: Config) -> List[Finding]:
    """Writable-path checks, only meaningful as the daemon's own user."""
    out: List[Finding] = []
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return out
    paths = [
        ("logging.file", config.logging.file),
        ("logging.state_file", config.logging.state_file),
        ("statistics.db_directory", str(Path(config.statistics.db_directory) / "x")),
    ]
    for key, value in paths:
        if not value:
            continue
        parent = Path(value).parent
        if parent.exists() and not os.access(parent, os.W_OK):
            out.append(Finding(
                LEVEL_WARN, "features", f"{key}: {parent} is not writable"))
    return out


def _privilege_findings(config: Config) -> List[Finding]:
    from eneru import cli
    out: List[Finding] = []
    reasons = cli._root_required_reasons(config)
    runtime = _runtime_ctx._detect_runtime_context()
    if not reasons:
        out.append(Finding(
            LEVEL_OK, "runtime", "Remote-only config: Eneru can run without root"))
        return out
    if _runtime_ctx._is_container_runtime(runtime) and cli._find_host_loopback(config):
        out.append(Finding(
            LEVEL_OK, "runtime",
            "Local actions are delegated to the host over loopback SSH "
            "(no root needed inside the container)"))
        return out
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() == 0:
        out.append(Finding(
            LEVEL_OK, "runtime", "Running as root, as local-host actions require"))
    else:
        out.append(Finding(
            LEVEL_WARN, "runtime",
            "The daemon must run as root for this config (the packaged "
            "systemd unit does)",
            "Because: " + "; ".join(reasons) + ". This check runs as uid "
            f"{geteuid()}, so local-host probes may under-report."))
    return out


def _loopback_contract_findings(config: Config) -> List[Finding]:
    from eneru import cli
    with _captured_output() as buf:
        try:
            cli._exit_on_missing_loopback_contract(config)
        except SystemExit:
            pass
    return _findings_from_printed(buf.getvalue(), "runtime")


def _validation_findings(config: Config, raw: Optional[dict]) -> List[Finding]:
    out: List[Finding] = []
    messages = ConfigLoader.validate_config(config, raw_data=raw)
    for msg in messages:
        if is_validation_error(msg):
            level = LEVEL_ERROR
        elif msg.startswith("INFO"):
            level = LEVEL_INFO
        else:
            level = LEVEL_WARN
        out.append(Finding(level, classify_message(msg), _strip_level(msg)))
    if not any(f.level == LEVEL_ERROR for f in out):
        out.append(Finding(
            LEVEL_OK, "file", "Schema and semantic validation passed"))
    return out


def static_findings(config: Config, raw: Optional[dict]) -> List[Finding]:
    """All checks that don't touch the network or run tools."""
    out: List[Finding] = []
    # Same preparation `eneru run` performs, so loopback synthesis and the
    # container/Kubernetes notices appear here exactly as at startup.
    from eneru import cli
    with _captured_output() as buf:
        try:
            cli._prepare_runtime_config(config, strict_key_check=False)
        except SystemExit:
            pass
    out.extend(_findings_from_printed(buf.getvalue(), "runtime"))
    out.extend(_validation_findings(config, raw))
    out.extend(_loopback_contract_findings(config))
    out.extend(_privilege_findings(config))
    out.extend(_dependency_findings(config))
    out.extend(_optional_module_findings(config))
    out.extend(_behavior_findings(config))
    out.extend(_feature_findings(config))
    out.extend(_budget_findings(config))
    out.extend(_path_findings(config))
    return out


# ---------------------------------------------------------------------------
# Live probes: NUT
# ---------------------------------------------------------------------------

def _nut_host(name: str) -> str:
    if "@" in (name or ""):
        return name.split("@", 1)[1].strip() or "localhost"
    return "localhost"


def _nut_name(name: str) -> str:
    return (name or "").split("@", 1)[0].strip()


def parse_upsc(text: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip()
    return data


def probe_ups(config: Config, group: UPSGroupConfig) -> List[Finding]:
    """Read-only NUT inspection of one UPS group."""
    from eneru import cli, nut_control
    label = group.ups.label
    out: List[Finding] = []

    def add(level, message, hint=""):
        out.append(Finding(level, "ups", clean(message), hint, subject=label))

    if not command_exists("upsc"):
        add(LEVEL_ERROR, f"{label}: cannot inspect, 'upsc' is not installed")
        return out
    target = group.ups.name
    host = _nut_host(target)
    configured = _nut_name(target)
    env = {"NUT_QUIET_INIT_SSL": "true"}

    code, stdout, stderr = _run(["upsc", "-l", host], timeout=10,
                                env_overrides=env)
    if code != 0:
        add(LEVEL_ERROR,
            f"{label}: cannot reach the NUT server at {host}: "
            f"{(stderr or stdout).strip() or f'exit {code}'}",
            "Check that upsd is running there, LISTENs on a reachable "
            "address (upsd.conf) and that TCP 3493 is not firewalled.")
        return out
    names = [ln.strip() for ln in stdout.splitlines()
             if ln.strip() and " " not in ln.strip() and ":" not in ln.strip()]
    if configured not in names:
        avail = ", ".join(names) if names else "none"
        add(LEVEL_ERROR,
            f"{label}: UPS '{configured}' does not exist on {host} "
            f"(available: {avail})",
            "The part before '@' must be the UPS name from the server's "
            "ups.conf, not a NUT login username.")
        return out
    add(LEVEL_OK, f"{label}: NUT server {host} lists UPS '{configured}'")

    start = time.monotonic()
    code, stdout, stderr = _run(["upsc", target], timeout=10,
                                env_overrides=env)
    latency_ms = int((time.monotonic() - start) * 1000)
    if code != 0:
        add(LEVEL_ERROR, f"{label}: reading UPS variables failed: "
            f"{(stderr or stdout).strip() or f'exit {code}'}")
        return out
    data = parse_upsc(stdout)
    status = data.get("ups.status", "?")
    model = " ".join(x for x in (data.get("device.mfr") or data.get("ups.mfr"),
                                 data.get("device.model") or data.get("ups.model"))
                     if x)
    charge = data.get("battery.charge")
    runtime = data.get("battery.runtime")
    summary = [f"status {status}"]
    if charge is not None:
        summary.append(f"charge {charge}%")
    if runtime is not None:
        summary.append(f"runtime {format_seconds(runtime)}")
    add(LEVEL_OK, f"{label}: {model + ': ' if model else ''}"
        f"{', '.join(summary)} ({latency_ms} ms)")
    if latency_ms > SLOW_NUT_MS:
        add(LEVEL_WARN, f"{label}: NUT answered slowly ({latency_ms} ms)",
            "The daemon polls every check_interval; slow answers delay "
            "power-event detection.")
    if "OB" in status.split():
        add(LEVEL_WARN, f"{label}: the UPS is ON BATTERY right now")
    if charge is None:
        add(LEVEL_WARN, f"{label}: the UPS does not report battery.charge",
            "triggers.low_battery_threshold can never fire for this UPS.")
    if runtime is None:
        add(LEVEL_WARN, f"{label}: the UPS does not report battery.runtime",
            "triggers.critical_runtime_threshold can never fire; the "
            "charge, depletion and extended-time triggers still work.")
    elif is_numeric(runtime) and is_numeric(group.triggers.critical_runtime_threshold):
        if float(runtime) <= float(group.triggers.critical_runtime_threshold):
            add(LEVEL_WARN,
                f"{label}: at the current load the UPS reports "
                f"{format_seconds(runtime)} of runtime, at or below "
                "critical_runtime_threshold "
                f"({format_seconds(group.triggers.critical_runtime_threshold)})",
                "A power cut would start the shutdown immediately.")
    energy = resolve_energy_config(Config(ups_groups=[group], energy=config.energy))
    rated = data.get("ups.realpower.nominal")
    if (energy.nominal_power is not None and is_numeric(rated)
            and float(rated) > 0 and float(energy.nominal_power) > float(rated)):
        add(LEVEL_WARN,
            f"{label}: energy.nominal_power ({float(energy.nominal_power):g} W) "
            f"is above the UPS's ups.realpower.nominal ({float(rated):g} W)",
            "Check it isn't a VA rating.")

    if not command_exists("upscmd"):
        return out
    nc = cli._effective_nut_control(config, group)
    ok, commands, err = nut_control.list_commands(
        target, username=nc.username or "", password=nc.password or "",
        timeout=int(nc.timeout) if is_numeric(nc.timeout) else 10)
    if nc.username and nc.password:
        if ok:
            add(LEVEL_OK, f"{label}: NUT login as '{nc.username}' works; "
                f"{len(commands)} instant command(s) listed")
        else:
            add(LEVEL_ERROR, f"{label}: NUT login as '{nc.username}' failed: {err}",
                "Check the user in upsd.users (password, instcmds) and "
                "reload upsd.")
    elif ok:
        add(LEVEL_INFO, f"{label}: {len(commands)} instant command(s) listed "
            "anonymously")
    st = getattr(group, "self_test", None) or config.self_test
    if st.enabled and ok and commands:
        command = cli._resolve_self_test_command(config, group)
        if command not in commands:
            tests = [c for c in commands if c.startswith("test.")]
            add(LEVEL_WARN,
                f"{label}: self_test.command '{command}' is not exposed by "
                "this UPS",
                "Available test commands: " + (", ".join(tests) or "none"))
    return out


# ---------------------------------------------------------------------------
# Live probes: remote servers
# ---------------------------------------------------------------------------

_SUDO_ARG_OPTS = {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}
_POWER_BINARIES = {"shutdown", "poweroff", "halt", "reboot", "synoshutdown",
                   "systemctl", "init"}


def command_binary(command: str) -> Tuple[Optional[str], bool, List[str]]:
    """Return (binary, via_sudo, args) for the first simple command.

    Only the FIRST command of a pipeline/list is inspected; the rest belongs
    to the operator. ``sudo`` options are skipped to find the real binary;
    ``args`` are the binary's own arguments (sudoers rules may pin them).
    """
    head = re.split(r"[;&|]", command or "", maxsplit=1)[0]
    try:
        tokens = shlex.split(head)
    except ValueError:
        return None, False, []
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return None, False, []
    if os.path.basename(tokens[0]) != "sudo":
        return tokens[0], False, tokens[1:]
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        i += 2 if opt in _SUDO_ARG_OPTS else 1
    if i >= len(tokens):
        return None, True, []
    return tokens[i], True, tokens[i + 1:]


@dataclass
class RemoteCheck:
    """One shell probe executed inside the single SSH session."""

    label: str
    script: str
    # "exists" -> binary lookup, "run" -> read-only command, "sudo" -> sudo -l
    kind: str
    fail_level: str = LEVEL_ERROR
    fail_hint: str = ""
    # For exists/sudo checks: the binary, so a sudo failure can be muted when
    # the binary is simply missing (one root cause, one red line).
    binary: str = ""


def _q(value: str) -> str:
    return shlex.quote(value)


def _exists(binary: str, label: Optional[str] = None,
            hint: str = "") -> RemoteCheck:
    return RemoteCheck(label or f"'{binary}' is installed",
                       f"command -v {_q(binary)}", "exists", fail_hint=hint,
                       binary=binary)


def _sudo_allowed(binary: str, args: Optional[List[str]] = None) -> RemoteCheck:
    # `sudo -n -l <cmd> [args]` asks the policy "may I run exactly this
    # without a password?" and prints the resolved command. It NEVER executes
    # it. The arguments are passed because sudoers rules may pin them
    # (e.g. `NOPASSWD: /usr/syno/sbin/synoshutdown -s`), and the bare name is
    # resolved by sudo itself, exactly like the real `sudo -n <cmd>` run.
    shown = " ".join([binary] + list(args or []))
    return RemoteCheck(
        f"sudo allows '{shown}' without a password",
        "sudo -n -l " + " ".join(_q(a) for a in [binary] + list(args or [])),
        "sudo",
        fail_hint="Add a NOPASSWD sudoers rule for this exact command for the "
        "SSH user (if the rule pins arguments, they must match).",
        binary=binary)


def _runtime_script(sub: str, sudo: str) -> str:
    """Run a read-only `<docker|podman> <sub>` with whichever runtime exists."""
    return (f"if command -v docker >/dev/null 2>&1; then {sudo}docker {sub}; "
            f"elif command -v podman >/dev/null 2>&1; then {sudo}podman {sub}; "
            "else echo 'neither docker nor podman found'; exit 127; fi")


def _proc_mounts_escape(path: str) -> str:
    """Encode a path the way /proc/mounts prints it (octal for space & co)."""
    return (path.replace("\\", "\\134").replace(" ", "\\040")
            .replace("\t", "\\011").replace("\n", "\\012"))


def _mounted(path: str) -> RemoteCheck:
    # ENVIRON (not `awk -v`) so awk doesn't re-interpret the backslashes.
    return RemoteCheck(
        f"{path} is mounted",
        f"P={_q(_proc_mounts_escape(path))} awk "
        "'$2==ENVIRON[\"P\"]{f=1} END{exit !f}' /proc/mounts",
        "run", fail_level=LEVEL_WARN,
        fail_hint="Not mounted right now; the unmount step will be a no-op.")


def _mount_paths(mounts: List[Any]) -> List[str]:
    paths = []
    for m in mounts or []:
        p = m.get("path") if isinstance(m, dict) else m
        if isinstance(p, str) and p.strip():
            paths.append(p.strip())
    return paths


def action_checks(action: str, use_sudo: bool, *, path: str = "",
                  mounts: Optional[List[Any]] = None) -> List[RemoteCheck]:
    """Harmless probes proving a predefined action can run on the remote."""
    sudo = "sudo -n " if use_sudo else ""
    if action in ("stop_containers", "stop_compose"):
        checks = [RemoteCheck("docker or podman is installed",
                              "command -v docker || command -v podman", "exists")]
        if action == "stop_containers":
            checks.append(RemoteCheck("listing containers works",
                                      _runtime_script("ps -q", sudo), "run"))
        else:
            # Same preference as the stop_compose template: docker compose if
            # the plugin works, otherwise podman compose.
            checks.append(RemoteCheck(
                "compose works",
                f"if command -v docker >/dev/null 2>&1 && {sudo}docker compose "
                f"version >/dev/null 2>&1; then {sudo}docker compose version; "
                f"elif command -v podman >/dev/null 2>&1; then {sudo}podman "
                "compose version; else echo 'no docker compose or podman "
                "compose'; exit 127; fi", "run"))
            if path:
                checks.append(RemoteCheck(
                    f"compose file {path} exists", f"test -r {_q(path)}", "run",
                    fail_hint="stop_compose would fail: the file is missing "
                    "or unreadable for the SSH user."))
        return checks
    if action == "stop_containers_rootless":
        return [_exists("loginctl"), _exists("podman"),
                RemoteCheck("listing logged-in users works",
                            "loginctl list-users --no-legend", "run")]
    if action == "stop_vms":
        return [_exists("virsh"),
                RemoteCheck("listing running VMs works",
                            f"{sudo}virsh list --name --state-running", "run")]
    if action == "stop_proxmox_vms":
        return [_exists("qm"), RemoteCheck("sudo -n qm list works",
                                           "sudo -n qm list", "run")]
    if action == "stop_proxmox_cts":
        return [_exists("pct"), RemoteCheck("sudo -n pct list works",
                                            "sudo -n pct list", "run")]
    if action == "stop_xcpng_vms":
        return [_exists("xe"), RemoteCheck(
            "listing running VMs works",
            "xe vm-list power-state=running is-control-domain=false --minimal",
            "run")]
    if action == "stop_esxi_vms":
        return [_exists("vim-cmd"), RemoteCheck(
            "listing VMs works", "vim-cmd vmsvc/getallvms", "run")]
    if action == "unmount_filesystems":
        checks = [_exists("umount")]
        for mount in mounts or []:
            path = mount.get("path") if isinstance(mount, dict) else mount
            if not isinstance(path, str) or not path.strip():
                continue
            opts = (mount.get("options") or "") if isinstance(mount, dict) else ""
            checks.append(_mounted(path.strip()))
            if use_sudo:
                # Mirrors the template: `sudo -n umount $opts "$mp"`.
                checks.append(_sudo_allowed("umount", opts.split() + [path.strip()]))
        return checks
    if action == "sync":
        return [_exists("sync")]
    return [RemoteCheck(f"unknown action '{action}'", "exit 2", "run")]


def command_checks(command: str, use_sudo: bool, *,
                   final: bool = False, user: str = "") -> Tuple[List[RemoteCheck], List[str]]:
    """Presence + sudo-permission checks for a command we must NOT run.

    Returns (checks, notes). Custom commands and the final shutdown command
    are never executed: Eneru only proves the binary is on the PATH and, when
    sudo is involved, that sudo would allow it without a password.

    ``use_sudo`` mirrors the runtime exactly: it prefixes ONLY the final
    shutdown command (and the built-in actions), never a custom
    pre_shutdown command, which runs verbatim.
    """
    notes: List[str] = []
    effective = command
    stripped = (command or "").lstrip()
    if final and use_sudo and not stripped.startswith("sudo "):
        effective = f"sudo -n {command}"
    binary, via_sudo, args = command_binary(effective)
    if not binary:
        notes.append(f"could not parse '{command}'; nothing was checked")
        return [], notes
    if not final and use_sudo and not via_sudo:
        notes.append(
            f"use_sudo does not apply to custom command '{command}': it runs "
            "as the SSH user. Prefix it with 'sudo -n' if it needs root.")
    checks = [_exists(binary, hint=(
        "Not found on the remote PATH (Eneru adds /usr/sbin, /sbin, "
        "/usr/local/sbin and Synology's /usr/syno/sbin)."))]
    if via_sudo:
        checks.append(_sudo_allowed(binary, args))
    elif (final and user and user != "root"
          and os.path.basename(binary) in _POWER_BINARIES):
        notes.append(
            f"'{binary}' runs without sudo as non-root user '{user}'; most "
            "systems refuse that. Enable use_sudo or prefix the command with sudo.")
    return checks, notes


def build_remote_script(checks: List[RemoteCheck]) -> str:
    """One shell script that runs every check and reports rc + first line."""
    from eneru.shutdown.remote import REMOTE_PATH_PREFIX
    parts = [REMOTE_PATH_PREFIX,
             'T=""; command -v timeout >/dev/null 2>&1 && '
             f'T="timeout {REMOTE_CHECK_TIMEOUT}"; ']
    for idx, check in enumerate(checks):
        parts.append(
            f"out=$($T sh -c {_q(check.script)} </dev/null 2>&1); rc=$?; "
            f"printf '%s %s %s %s\\n' {_REMOTE_MARKER} {idx} \"$rc\" "
            "\"$(printf '%s' \"$out\" | head -n 1 | cut -c1-200)\"; ")
    parts.append("exit 0")
    return "".join(parts)


def parse_remote_output(text: str) -> Dict[int, Tuple[int, str]]:
    results: Dict[int, Tuple[int, str]] = {}
    for line in (text or "").splitlines():
        if not line.startswith(_REMOTE_MARKER + " "):
            continue
        bits = line.split(" ", 3)
        if len(bits) < 3 or not bits[1].isdigit():
            continue
        try:
            rc = int(bits[2])
        except ValueError:
            continue
        results[int(bits[1])] = (rc, bits[3] if len(bits) > 3 else "")
    return results


def _remote_owner_mounts(config: Config, server: RemoteServerConfig) -> List[Any]:
    """Loopback delegates unmount the LOCAL owner's mounts, when enabled."""
    owner = _runtime_ctx._local_owner_group(config)
    if owner is None or not owner.filesystems.unmount.enabled:
        return []
    return list(owner.filesystems.unmount.mounts or [])


def remote_checks(config: Config, server: RemoteServerConfig
                  ) -> Tuple[List[RemoteCheck], List[str]]:
    checks: List[RemoteCheck] = []
    notes: List[str] = []
    for cmd in server.pre_shutdown_commands:
        if cmd.action:
            mounts = cmd.mounts
            if cmd.action == "unmount_filesystems" and server.is_host_loopback:
                mounts = _remote_owner_mounts(config, server)
            elif cmd.action == "unmount_filesystems" and not _mount_paths(mounts):
                notes.append("unmount_filesystems has no `mounts` listed: the "
                             "step does nothing and is reported as failed")
            checks.extend(action_checks(
                cmd.action, server.use_sudo, path=cmd.path or "", mounts=mounts))
        elif cmd.command:
            c, n = command_checks(cmd.command, server.use_sudo)
            checks.extend(c)
            notes.extend(n)
            notes.append(f"custom command '{cmd.command}': only its binary was "
                         "checked, it was not executed")
    c, n = command_checks(server.shutdown_command, server.use_sudo,
                          final=True, user=server.user)
    checks.extend(c)
    notes.extend(n)
    return checks, notes


def probe_remote(config: Config, server: RemoteServerConfig, *,
                 owner: str = "") -> List[Finding]:
    """SSH reachability + harmless per-command checks for one remote."""
    from eneru.remote_health import (
        build_ssh_probe_command,
        is_safe_probe_command,
        run_loopback_identity_probe,
        run_remote_probe,
    )
    name = server.name or server.host
    out: List[Finding] = []

    def add(level, message, hint=""):
        out.append(Finding(level, "remote", clean(f"{name}: {message}"), hint,
                           subject=name))

    if not server.enabled:
        add(LEVEL_INFO, "disabled, not probed")
        return out
    if not command_exists("ssh"):
        add(LEVEL_ERROR, "cannot probe, the 'ssh' client is not installed")
        return out
    probe = config.remote_health.probe_command
    if not is_safe_probe_command(probe):
        probe = "true"
    try:
        ok, err, latency = run_remote_probe(server, probe)
    except ValueError as exc:
        add(LEVEL_ERROR, str(exc))
        return out
    if not ok:
        add(LEVEL_ERROR, f"SSH to {server.user}@{server.host} failed: {err}",
            "Eneru uses key-based, non-interactive SSH (BatchMode). Check the "
            "key (ssh_key_path), authorized_keys on the target, and that the "
            "host key is accepted.")
        return out
    add(LEVEL_OK, f"SSH as {server.user}@{server.host} works ({latency} ms)")
    if server.is_host_loopback is True:
        id_ok, id_err, _ = run_loopback_identity_probe(server)
        if id_ok:
            add(LEVEL_OK, "host identity matches (loopback reaches THIS host)")
        else:
            add(LEVEL_ERROR, id_err)

    checks, notes = remote_checks(config, server)
    for note in notes:
        warn = ("most systems refuse" in note or "does not apply" in note
                or "no `mounts` listed" in note)
        add(LEVEL_WARN if warn else LEVEL_INFO, note)
    if not checks:
        return out
    script = build_remote_script(checks)
    code, stdout, stderr = _run(
        build_ssh_probe_command(server, script),
        timeout=server.connect_timeout + REMOTE_CHECK_TIMEOUT * len(checks) + 10)
    results = parse_remote_output(stdout)
    if not results:
        add(LEVEL_ERROR, "the command checks did not run: "
            f"{stderr.strip() or f'exit {code}'}")
        return out
    missing = set()
    for idx, check in enumerate(checks):
        rc, first = results.get(idx, (-1, "no result"))
        if rc == 0:
            add(LEVEL_OK, check.label)
            continue
        if check.kind == "exists" and check.binary:
            missing.add(check.binary)
        if check.kind == "sudo" and check.binary in missing:
            continue
        detail = f" ({first})" if first else ""
        if check.kind == "exists":
            add(check.fail_level, f"{check.label.replace('is installed', 'is NOT installed')}"
                f"{detail}", check.fail_hint)
        elif check.kind == "sudo":
            add(check.fail_level, f"sudo refuses it without a password: "
                f"{check.label}{detail}", check.fail_hint)
        else:
            add(check.fail_level, f"failed: {check.label}{detail}",
                check.fail_hint)
    return out


# ---------------------------------------------------------------------------
# Live probes: this host
# ---------------------------------------------------------------------------

def probe_local(config: Config) -> List[Finding]:
    """Read-only checks for the local drain phases (not when delegated)."""
    out: List[Finding] = []
    owner = _runtime_ctx._local_owner_group(config)
    if owner is None and config.ups_groups and not config.multi_ups:
        owner = config.ups_groups[0]
    if owner is None or _runtime_ctx._uses_loopback_delegate(config):
        return out
    # The daemon runs these as root; as another user a failure may only mean
    # "not allowed for me", so it is a warning, not a verdict.
    geteuid = getattr(os, "geteuid", None)
    as_root = geteuid is None or geteuid() == 0
    hard = LEVEL_ERROR if as_root else LEVEL_WARN
    note = "" if as_root else " (checked as non-root; re-run as root to be sure)"
    if owner.virtual_machines.enabled and command_exists("virsh"):
        code, stdout, stderr = _run(
            ["virsh", "list", "--name", "--state-running"], timeout=15)
        if code == 0:
            running = [ln for ln in stdout.splitlines() if ln.strip()]
            out.append(Finding(LEVEL_OK, "local",
                               f"virsh works: {len(running)} VM(s) running"))
        else:
            out.append(Finding(hard, "local",
                               clean(f"virsh list failed: {stderr.strip() or code}")
                               + note,
                               "Is libvirtd running? The daemon runs as root."))
    c = owner.containers
    if c.enabled:
        rts = ["podman", "docker"] if c.runtime == "auto" else [c.runtime]
        rt = next((r for r in rts if command_exists(r)), None)
        if rt:
            code, _, stderr = _run([rt, "ps", "-q"], timeout=15)
            if code == 0:
                out.append(Finding(LEVEL_OK, "local", f"{rt} ps works"))
            else:
                out.append(Finding(LEVEL_WARN, "local", clean(
                    f"{rt} ps failed: {stderr.strip() or code}") + note))
            if c.compose_files:
                code, _, _ = _run([rt, "compose", "version"], timeout=15)
                if code != 0:
                    out.append(Finding(
                        LEVEL_WARN, "local",
                        f"'{rt} compose' is not available; compose stacks "
                        "will be skipped"))
        for cf in c.compose_files:
            if cf.path and not Path(cf.path).exists():
                out.append(Finding(hard, "local",
                                   f"compose file not found: {cf.path}{note}"))
    um = owner.filesystems.unmount
    if um.enabled:
        for p in _mount_paths(um.mounts):
            if os.path.ismount(p):
                out.append(Finding(LEVEL_OK, "local", f"{p} is mounted"))
            else:
                out.append(Finding(
                    LEVEL_WARN, "local", f"{p} is not a mount point right now",
                    "The unmount step will be a no-op for it."))
    return out


def probe_findings(config: Config, *, max_workers: int = 8) -> List[Finding]:
    """Run every live probe in parallel; results keep a stable order."""
    jobs: List[Callable[[], List[Finding]]] = []
    for group in config.ups_groups:
        jobs.append(lambda g=group: probe_ups(config, g))
    jobs.append(lambda: probe_local(config))
    for group in _all_groups(config):
        for server in group.remote_servers:
            jobs.append(lambda s=server: probe_remote(config, s))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(job) for job in jobs]
        results: List[Finding] = []
        for fut in futures:
            try:
                results.extend(fut.result())
            except Exception as exc:  # a probe crash must not hide the rest
                results.append(Finding(LEVEL_ERROR, "runtime",
                                       f"probe crashed: {exc}"))
    return results


# ---------------------------------------------------------------------------
# Power-loss preview
# ---------------------------------------------------------------------------

def _group_config(config: Config, group: Any) -> Config:
    if isinstance(group, UPSGroupConfig):
        ugc = group
    else:
        ugc = UPSGroupConfig(
            remote_servers=list(group.remote_servers),
            virtual_machines=group.virtual_machines,
            containers=group.containers,
            filesystems=group.filesystems,
            is_local=group.is_local,
        )
    return Config(ups_groups=[ugc], behavior=config.behavior,
                  local_shutdown=config.local_shutdown)


def _plan_for_group(config: Config, group: Any) -> Dict[str, Any]:
    from eneru.shutdown.plan import build_shutdown_plan
    is_ups = isinstance(group, UPSGroupConfig)
    multi = config.multi_ups or bool(config.redundancy_groups)
    is_local = group.is_local or (is_ups and not config.multi_ups)
    delegated = _runtime_ctx._uses_loopback_delegate(config, group)
    handoff = None
    if multi and is_ups:
        handoff = is_local or (
            config.local_shutdown.trigger_on == "any"
            and not any(g.is_local for g in config.ups_groups))
    return build_shutdown_plan(
        _group_config(config, group), is_local=is_local, delegated=delegated,
        coordinator_mode=multi, coordinator_handoff=handoff,
        include_final_sync=is_ups, reveal_commands=True)


def _trigger_line(t: Any) -> str:
    parts = [f"battery <= {t.low_battery_threshold}%",
             f"runtime <= {format_seconds(t.critical_runtime_threshold)}",
             f"drain > {t.depletion.critical_rate}%/min "
             f"(after {format_seconds(t.depletion.grace_period)})"]
    if t.extended_time.enabled:
        parts.append(f"{format_seconds(t.extended_time.threshold)} on battery")
    parts.append("UPS signals FSD")
    return " | ".join(parts)


def power_loss_plan(config: Config) -> List[str]:
    """Human-readable "what happens on power loss" timeline."""
    lines: List[str] = []
    if config.behavior.dry_run:
        lines.append("DRY-RUN: every step below is only logged, nothing executes.")
    members = _redundancy_members(config)
    for group in _all_groups(config):
        is_ups = isinstance(group, UPSGroupConfig)
        if is_ups:
            label = group.ups.label
            role = ("protects THIS host" if (group.is_local or not config.multi_ups)
                    else "monitoring / remote-only")
            lines.append(f"UPS {label} ({role})")
            if group.ups.name in members:
                lines.append(
                    f"  Its triggers are advisory: redundancy group "
                    f"'{members[group.ups.name]}' decides when to act.")
            else:
                lines.append(f"  Starts when: {_trigger_line(group.triggers)}")
        else:
            label = group.name or "(unnamed)"
            lines.append(f"Redundancy group {label}")
            lines.append(
                f"  Starts when fewer than {group.min_healthy} of "
                f"{len(group.ups_sources)} UPS ({', '.join(group.ups_sources)}) "
                "are healthy")
        plan = _plan_for_group(config, group)
        step = 0
        for phase in plan["phases"]:
            if not phase["enabled"]:
                continue
            step += 1
            est = phase.get("estimateS")
            budget = f" (up to {format_seconds(est)})" if est else ""
            mode = " [parallel]" if phase.get("mode") == "parallel" else ""
            lines.append(f"  {step}. {phase['title']}{mode}{budget}")
            for s in phase["steps"]:
                detail = f" - {s['detail']}" if s.get("detail") else ""
                lines.append(f"       {s['label']}{detail}")
        if step == 0:
            lines.append("  (nothing to shut down: notify only)")
        if plan.get("note"):
            lines.append(f"  Note: {plan['note']}")
        total = plan.get("totalEstimateS")
        if total:
            lines.append(f"  Worst case: about {format_seconds(total)} "
                         "plus sync/poweroff time")
    return lines


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def check_mapping(data: dict, *, path: Optional[str] = None,
                  probes: bool = True) -> CheckReport:
    """Inspect an already-parsed mapping (used by the TUI on unsaved edits)."""
    report = CheckReport(path=path)
    config, findings = build_config(data, path=path)
    report.findings.extend(findings)
    if config is None:
        return report
    report.findings.extend(static_findings(config, data))
    if probes:
        report.findings.extend(probe_findings(config))
    report.plan = power_loss_plan(config)
    return report


def check_file(path: str, *, probes: bool = True) -> CheckReport:
    data, findings = load_raw(path)
    if data is None:
        report = CheckReport(path=path)
        report.findings.extend(findings)
        return report
    report = check_mapping(data, path=path, probes=probes)
    report.findings[:0] = findings
    return report


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------

_ICONS = {LEVEL_ERROR: "✗", LEVEL_WARN: "!", LEVEL_INFO: "i", LEVEL_OK: "✓"}
_COLORS = {LEVEL_ERROR: "31", LEVEL_WARN: "33", LEVEL_INFO: "36", LEVEL_OK: "32"}


def _paint(text: str, code: str, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def format_report(report: CheckReport, *, color: bool = False,
                  verbose: bool = True) -> str:
    """Render a report grouped by section (errors first inside each)."""
    from eneru.version import __version__
    lines = [f"Eneru v{__version__} - configuration check"]
    if report.path:
        lines.append(f"  File: {report.path}")
    lines.append(f"  Runtime: {_runtime_ctx._detect_runtime_context()}")
    order = {lvl: i for i, lvl in enumerate(LEVELS)}
    for section in SECTIONS:
        items = [f for f in report.findings if f.section == section
                 and (verbose or f.level != LEVEL_OK)]
        if not items:
            continue
        lines.append("")
        lines.append(_paint(f"== {SECTION_TITLES[section]} ==", "1", color))
        for f in sorted(items, key=lambda x: order.get(x.level, 9)):
            tag = {LEVEL_ERROR: "ERROR ", LEVEL_WARN: "WARN  ",
                   LEVEL_INFO: "INFO  ", LEVEL_OK: "OK    "}[f.level]
            lines.append("  " + _paint(f"{_ICONS[f.level]} {tag}{f.message}",
                                       _COLORS[f.level], color))
            if f.hint:
                lines.append(f"      -> {f.hint}")
    if report.plan:
        lines.append("")
        lines.append(_paint("== What happens on power loss ==", "1", color))
        lines.extend(f"  {line}" for line in report.plan)
    lines.append("")
    errors = report.count(LEVEL_ERROR)
    warns = report.count(LEVEL_WARN)
    oks = report.count(LEVEL_OK)
    summary = f"Summary: {errors} error(s), {warns} warning(s), {oks} check(s) passed."
    code = "31" if errors else ("33" if warns else "32")
    lines.append(_paint(summary, code, color))
    if errors:
        lines.append("Eneru would refuse to start or would misbehave: fix the "
                     "ERROR(s) above.")
    return "\n".join(lines)


def use_color(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())
