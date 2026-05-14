"""CLI entry point for Eneru."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from eneru.version import __version__
from eneru.config import Config, ConfigLoader, UPSConfig, UPSGroupConfig
from eneru.monitor import UPSGroupMonitor, compute_effective_order
from eneru.multi_ups import MultiUPSCoordinator
from eneru.notifications import APPRISE_AVAILABLE
from eneru.redundancy import RedundancyGroupExecutor
from eneru.remote_health import is_safe_probe_command, run_remote_probe

# Optional import for Apprise (needed for test notifications)
try:
    import apprise
except ImportError:
    apprise = None


class ConfigValidationLoadError(Exception):
    """Raised when raw YAML cannot be loaded for startup validation."""


def _non_negative_int(value: str) -> int:
    """argparse type for ``--length``: int >= 0 (0 = no cap)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--length must be a non-negative integer, got {value!r}"
        )
    if n < 0:
        raise argparse.ArgumentTypeError(
            f"--length must be >= 0 (0 = no cap), got {n}"
        )
    return n


def _load_config(args):
    """Load configuration from the --config path."""
    return ConfigLoader.load(getattr(args, 'config', None))


def _apply_run_overrides(config: Config, args) -> None:
    """Apply `eneru run` CLI overrides after YAML load, before validation."""
    if args.dry_run:
        config.behavior.dry_run = True

    if getattr(args, "api", False):
        config.api.enabled = True
    if getattr(args, "api_bind", None) is not None:
        config.api.enabled = True
        config.api.bind = args.api_bind
    if getattr(args, "api_port", None) is not None:
        config.api.enabled = True
        config.api.port = args.api_port


def _root_required_reasons(config: Config) -> list:
    """Return local-host features that require root at daemon startup."""
    reasons = []
    groups = list(config.ups_groups)
    if not groups:
        reasons.append("implicit single-UPS local-host mode")
    for group in groups:
        label = group.ups.label
        if group.is_local:
            reasons.append(f"UPS group '{label}' is marked is_local")
        if group.virtual_machines.enabled:
            reasons.append(f"UPS group '{label}' has virtual_machines enabled")
        if group.containers.enabled:
            reasons.append(f"UPS group '{label}' has containers enabled")
        if group.filesystems.unmount.enabled:
            reasons.append(f"UPS group '{label}' has filesystem unmount enabled")

    for group in config.redundancy_groups:
        label = group.name or "(unnamed)"
        if group.is_local:
            reasons.append(f"redundancy group '{label}' is marked is_local")
        if group.virtual_machines.enabled:
            reasons.append(f"redundancy group '{label}' has virtual_machines enabled")
        if group.containers.enabled:
            reasons.append(f"redundancy group '{label}' has containers enabled")
        if group.filesystems.unmount.enabled:
            reasons.append(f"redundancy group '{label}' has filesystem unmount enabled")

    has_local_owner = any(g.is_local for g in groups) or any(
        g.is_local for g in config.redundancy_groups
    )
    if config.local_shutdown.enabled and (
        has_local_owner or not groups or config.local_shutdown.trigger_on == "any"
    ):
        reasons.append("local_shutdown can power off the Eneru host")

    return sorted(set(reasons))


def _detect_runtime_context() -> str:
    """Best-effort runtime-context label for the current process.

    Returns one of:
      - ``"container (Docker)"`` when ``/.dockerenv`` exists.
      - ``"container (Podman)"`` when ``/run/.containerenv`` exists.
      - ``"container"`` for other OCI runtimes (lxc, systemd-nspawn, etc.)
        detected via the ``container`` env var or container paths in
        ``/proc/1/cgroup`` or ``/proc/self/mountinfo``.
      - ``"systemd service"`` when running under a systemd unit
        (``INVOCATION_ID`` env var) and not in a container.
      - ``"bare process"`` otherwise.

    Container detection takes precedence: a systemd unit running inside
    a container is reported as a container, since that is the user-visible
    fact when troubleshooting.
    """
    if Path("/.dockerenv").exists():
        return "container (Docker)"
    if Path("/run/.containerenv").exists():
        return "container (Podman)"

    container_env = os.environ.get("container", "").strip().lower()
    if container_env:
        return f"container ({container_env})"

    try:
        cgroup_text = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        cgroup_text = ""
    if any(marker in cgroup_text for marker in ("docker", "kubepods", "containerd", "lxc")):
        return "container"

    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        mountinfo = ""
    if "/docker/containers/" in mountinfo or "/containers/storage/overlay-containers/" in mountinfo:
        return "container"

    if os.environ.get("INVOCATION_ID"):
        return "systemd service"

    try:
        comm = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except OSError:
        comm = ""
    if comm == "systemd" and os.environ.get("JOURNAL_STREAM"):
        return "systemd service"

    return "bare process"


def _exit_on_privilege_errors(config: Config) -> None:
    """Refuse non-root startup when config declares local-host ownership."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() == 0:
        return

    reasons = _root_required_reasons(config)
    if not reasons:
        return

    print("ERROR: Eneru must run as root for local-host orchestration.")
    for reason in reasons:
        print(f"  - {reason}")
    print(
        "For remote-only container/Kubernetes deployments, use multi-UPS "
        "configuration with is_local: false and set local_shutdown.enabled: false."
    )
    sys.exit(1)


def _load_raw_config_for_validation(args):
    """Load the YAML mapping used for unknown-key validation."""
    config_path = getattr(args, 'config', None)
    path = Path(config_path) if config_path else None
    if path is None:
        for candidate in ConfigLoader.DEFAULT_CONFIG_PATHS:
            if candidate.exists():
                path = candidate
                break
    if path is None or not path.exists():
        return None
    try:
        import yaml
        with open(path, 'r') as f:
            raw_data = yaml.safe_load(f)
        if raw_data is None:
            return {}
        if not isinstance(raw_data, dict):
            raise ConfigValidationLoadError(
                f"ERROR: Config root in {path} must be a YAML mapping."
            )
        return raw_data
    except Exception as exc:
        if isinstance(exc, ConfigValidationLoadError):
            raise
        raise ConfigValidationLoadError(
            f"ERROR: Failed to parse {path} for validation: {exc}"
        ) from exc


def _exit_on_config_errors(config, args):
    """Prevent daemon startup when validation reports hard errors."""
    try:
        raw_data = _load_raw_config_for_validation(args)
    except ConfigValidationLoadError as exc:
        print(exc)
        sys.exit(1)
    messages = ConfigLoader.validate_config(
        config, raw_data=raw_data,
    )
    errors = [m for m in messages if m.startswith("ERROR:")]
    if not errors:
        return
    for msg in errors:
        print(msg)
    sys.exit(1)


def _cmd_run(args):
    """Start the monitoring daemon."""
    config = _load_config(args)
    _apply_run_overrides(config, args)

    _exit_on_config_errors(config, args)
    _exit_on_privilege_errors(config)

    if config.multi_ups or config.redundancy_groups:
        coordinator = MultiUPSCoordinator(config, exit_after_shutdown=args.exit_after_shutdown)
        coordinator.run()
    else:
        monitor = UPSGroupMonitor(config, exit_after_shutdown=args.exit_after_shutdown)
        monitor.run()


def _print_shutdown_sequence(group, enabled_servers, has_local, prefix):
    """Print the shutdown sequence tree for a UPS group."""
    print(f"{prefix}  Shutdown sequence:")
    step = 1
    indent = f"{prefix}    "

    if has_local:
        if group.virtual_machines.enabled:
            print(f"{indent}{step}. Virtual machines")
            step += 1
        if group.containers.enabled:
            containers = group.containers
            compose_count = len(containers.compose_files)
            detail = f" ({containers.runtime}"
            if compose_count > 0:
                detail += f", {compose_count} compose file(s)"
            detail += ")"
            print(f"{indent}{step}. Containers{detail}")
            step += 1
        if group.filesystems.sync_enabled or group.filesystems.unmount.enabled:
            parts = []
            if group.filesystems.sync_enabled:
                parts.append("sync")
            if group.filesystems.unmount.enabled:
                mount_count = len(group.filesystems.unmount.mounts)
                parts.append(f"unmount {mount_count} mount(s)")
            print(f"{indent}{step}. Filesystem {' + '.join(parts)}")
            step += 1

    if enabled_servers:
        ordered = compute_effective_order(enabled_servers)
        phases = {}
        for effective, server in ordered:
            phases.setdefault(effective, []).append(server)
        sorted_keys = sorted(phases.keys())
        num_phases = len(sorted_keys)

        # Detect legacy mode: no server has explicit shutdown_order
        is_legacy = all(s.shutdown_order is None for s in enabled_servers)

        if num_phases == 1:
            names = ", ".join(s.name or s.host for s in enabled_servers)
            if len(enabled_servers) == 1:
                print(f"{indent}{step}. Remote server: {names}")
            else:
                print(f"{indent}{step}. Remote servers ({len(enabled_servers)}): {names}")
        else:
            print(f"{indent}{step}. Remote servers ({len(enabled_servers)}, {num_phases} phases):")
            for phase_idx, key in enumerate(sorted_keys, 1):
                phase_servers = phases[key]
                names = ", ".join(s.name or s.host for s in phase_servers)
                if is_legacy:
                    # Legacy mode: label by execution style
                    if key < 0:
                        print(f"{indent}   Sequential: {names}")
                    else:
                        print(f"{indent}   Parallel: {names}")
                else:
                    print(f"{indent}   Phase {phase_idx} (order={key}): {names}")
        step += 1
    else:
        print(f"{indent}(no remote servers)")

    if has_local:
        print(f"{indent}{step}. Local shutdown")


def _print_group_summary(group, idx, multi_ups):
    """Print a single UPS group summary for validate output."""
    label = group.ups.label
    prefix = f"  Group {idx}: " if multi_ups else "  "

    print(f"{prefix}UPS: {label}", end="")
    if group.ups.display_name:
        print(f" ({group.ups.name})", end="")
    if group.is_local:
        print(" [is_local]", end="")
    print()

    # Shutdown sequence tree
    enabled_servers = [s for s in group.remote_servers if s.enabled]
    has_local = group.is_local or not multi_ups
    _print_shutdown_sequence(group, enabled_servers, has_local, prefix)


def _cmd_validate(args):
    """Validate configuration and print overview."""
    config = _load_config(args)
    exit_code = 0

    print(f"Eneru v{__version__}")
    print(f"  Runtime context: {_detect_runtime_context()}")

    multi_ups = config.multi_ups
    if multi_ups:
        print(f"  Mode: multi-UPS ({len(config.ups_groups)} groups)")
        local_groups = [g for g in config.ups_groups if g.is_local]
        if local_groups:
            print(f"  Local UPS: {local_groups[0].ups.label}")
        else:
            print(f"  Local UPS: none (Eneru host has independent power)")
        print(f"  Drain on local shutdown: {config.local_shutdown.drain_on_local_shutdown}")
        print()

    for idx, group in enumerate(config.ups_groups, 1):
        _print_group_summary(group, idx, multi_ups)
        if idx < len(config.ups_groups):
            print()

    if config.redundancy_groups:
        print()
        print(f"  Redundancy groups ({len(config.redundancy_groups)}):")
        for rg_idx, rg in enumerate(config.redundancy_groups, 1):
            label = rg.name or "(unnamed)"
            tags = []
            if rg.is_local:
                tags.append("is_local")
            tag_suffix = f" [{', '.join(tags)}]" if tags else ""
            sources = ", ".join(rg.ups_sources) if rg.ups_sources else "(none)"
            print(f"    {rg_idx}. {label}{tag_suffix}")
            print(f"       Sources ({len(rg.ups_sources)}): {sources}")
            print(
                f"       Quorum: min_healthy={rg.min_healthy} "
                f"(degraded→{rg.degraded_counts_as}, unknown→{rg.unknown_counts_as})"
            )
            enabled_servers = [s for s in rg.remote_servers if s.enabled]
            if enabled_servers:
                names = ", ".join(s.name or s.host for s in enabled_servers)
                print(f"       Remote servers ({len(enabled_servers)}): {names}")
            local_parts = []
            if rg.is_local and rg.virtual_machines.enabled:
                local_parts.append("VMs")
            if rg.is_local and rg.containers.enabled:
                local_parts.append("containers")
            if rg.is_local and (rg.filesystems.sync_enabled or rg.filesystems.unmount.enabled):
                local_parts.append("filesystems")
            if local_parts:
                print(f"       Local resources: {', '.join(local_parts)}")

    print(f"  Dry-run: {config.behavior.dry_run}")

    # Notification status
    print(f"  Notifications:")
    if config.notifications.enabled and config.notifications.urls:
        if APPRISE_AVAILABLE:
            print(f"    Enabled: {len(config.notifications.urls)} service(s)")
            for url in config.notifications.urls:
                if '://' in url:
                    scheme = url.split('://')[0]
                    print(f"      - {scheme}://***")
                else:
                    print(f"      - {url[:20]}...")
            if config.notifications.title:
                print(f"    Title: {config.notifications.title}")
            else:
                print(f"    Title: (none)")
            if config.notifications.avatar_url:
                print(f"    Avatar URL: {config.notifications.avatar_url[:50]}...")
            print(f"    Retry interval: {config.notifications.retry_interval}s")
        else:
            print(f"    Apprise not installed - notifications disabled")
            print(f"    Install with: pip install apprise")
    else:
        print(f"    Disabled")

    # Re-parse the raw YAML so validation can catch misspelled safety keys
    # that ConfigLoader intentionally ignores while building dataclasses.
    raw_data = None
    try:
        raw_data = _load_raw_config_for_validation(args)
    except ConfigValidationLoadError as exc:
        print()
        print(f"  {exc}")
        exit_code = 1
    messages = ConfigLoader.validate_config(config, raw_data=raw_data)
    if messages:
        print()
        for msg in messages:
            print(f"  {msg}")
            if msg.startswith("ERROR"):
                exit_code = 1

    print()
    if exit_code == 0:
        print("Configuration is valid.")
    else:
        print("Configuration is INVALID — fix the ERROR(s) above and re-run.")

    sys.exit(exit_code)


def _cmd_test_notifications(args):
    """Send a test notification and exit."""
    config = _load_config(args)
    exit_code = 0

    print("Testing notifications...")

    if not config.notifications.enabled or not config.notifications.urls:
        print("No notification URLs configured.")
        print("   Add URLs to the 'notifications.urls' section in your config file.")
        sys.exit(1)

    if not APPRISE_AVAILABLE:
        print("Apprise is not installed.")
        print("   Install with: pip install apprise")
        sys.exit(1)

    apobj = apprise.Apprise()
    valid_urls = 0

    for url in config.notifications.urls:
        if apobj.add(url):
            valid_urls += 1
            scheme = url.split('://')[0] if '://' in url else 'unknown'
            print(f"  Added: {scheme}://***")
        else:
            print(f"  Invalid URL: {url[:30]}...")

    if valid_urls == 0:
        print("No valid notification URLs found.")
        sys.exit(1)

    print(f"\nSending test notification to {valid_urls} service(s)...")

    if config.notifications.title:
        print(f"  Title: {config.notifications.title}")
    if config.notifications.avatar_url:
        print(f"  Avatar: {config.notifications.avatar_url[:50]}...")

    # Build test body with all UPS names
    ups_lines = []
    for group in config.ups_groups:
        ups_lines.append(f"  {group.ups.label}")

    test_body = (
        "**Test Notification**\n"
        "This is a test notification from Eneru.\n"
        "If you see this, notifications are working correctly!\n"
        f"\n---\nUPS monitored:\n" + "\n".join(ups_lines) + "\n"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    escaped_body = test_body.replace("@", "@\u200B")

    notify_kwargs = {
        'body': escaped_body,
        'notify_type': apprise.NotifyType.INFO,
    }
    if config.notifications.title:
        notify_kwargs['title'] = config.notifications.title

    result = apobj.notify(**notify_kwargs)

    if result:
        print("Test notification sent successfully!")
    else:
        print("Failed to send test notification.")
        print("   Check your notification URLs and network connectivity.")
        exit_code = 1

    sys.exit(exit_code)


class _CLILogger:
    """Small logger adapter used by one-shot CLI drills."""

    def __init__(self, log_file: Optional[Path] = None) -> None:
        self.log_file = Path(log_file) if log_file else None

    def log(self, message: str, **_extra) -> None:
        # ``**_extra`` mirrors UPSLogger.log(message, **extra). The drill is
        # one-shot and prints to stdout / appends to a flat log file, so
        # structured kwargs (category, event_type, group, ...) are accepted
        # for signature compatibility but otherwise ignored — they're
        # meaningful only under the JSON formatter that the daemon uses.
        print(message)
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            # encoding="utf-8" so emoji and non-ASCII names in server.host /
            # server.user round-trip cleanly into the log file regardless
            # of the user's LANG/LC_ALL.
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(message + "\n")


def _iter_remote_server_owners(config):
    """Yield ``(owner_label, owner_name, server)`` for every remote server."""
    for group in config.ups_groups:
        for server in group.remote_servers:
            yield group.ups.label, group.ups.name, server
    for group in config.redundancy_groups:
        label = f"redundancy:{group.name}"
        for server in group.remote_servers:
            yield label, group.name, server


def _format_remote_list_table(rows: list) -> str:
    """Format remote-target rows as a fixed-width ASCII table.

    Width is computed per-column from the actual data so long names
    don't truncate; each column has a minimum that keeps the header
    readable when every row is short.
    """
    headers = ("NAME", "GROUP", "KIND", "HOST", "ENABLED", "ORDER")
    minimums = (10, 10, 10, 10, 7, 5)
    columns = list(zip(*[headers, *rows])) if rows else [(h,) for h in headers]
    widths = [
        max(minimums[i], max(len(str(cell)) for cell in column))
        for i, column in enumerate(columns)
    ]
    last = len(widths) - 1

    def fmt_row(row):
        return "  ".join(
            str(cell) if i == last else str(cell).ljust(widths[i])
            for i, cell in enumerate(row)
        )

    lines = [fmt_row(headers)]
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def _build_remote_list_rows_for_group(group, group_name: str, kind: str) -> tuple:
    """Build display rows for one group's remote_servers.

    Returns ``(rows, enabled_count)``. ``group_name`` is the value an
    operator passes to ``--group`` to address this group — keeping the
    GROUP column 1-to-1 with the CLI flag avoids the v5.3-rc disagreement
    where the column showed ``name (label)`` but ``--group`` only
    accepted the raw token.

    Effective order is computed on ``enabled`` servers only so the
    printed ORDER matches what the daemon would actually use during
    shutdown (the daemon also filters before computing). Disabled rows
    show ``—`` since they don't participate in the rotation at all.
    """
    enabled_servers = [s for s in group.remote_servers if s.enabled]
    order_by_id = {
        id(s): effective for effective, s in compute_effective_order(enabled_servers)
    }
    rows = []
    for server in group.remote_servers:
        host = f"{server.user}@{server.host}" if server.user else server.host
        if server.enabled:
            order_text = str(order_by_id[id(server)])
        else:
            order_text = "—"
        rows.append((
            server.name or server.host,
            group_name,
            kind,
            host,
            "yes" if server.enabled else "no",
            order_text,
        ))
    return rows, len(enabled_servers)


def _cmd_remote_list(args):
    """List configured remote shutdown targets across all groups."""
    config = _load_config(args)
    _exit_on_config_errors(config, args)

    # Stable per-group ordering so users see related rows next to each
    # other. Within a group, the helper sorts by daemon-effective
    # shutdown order so the printed sequence matches a real shutdown.
    rows = []
    enabled_count = 0
    for group in config.ups_groups:
        # Use the canonical name when present so the GROUP column is
        # exactly the string `eneru shutdown group --group ...` accepts;
        # fall back to the label only when name is empty.
        group_name = group.ups.name or group.ups.label or "(unnamed)"
        group_rows, group_enabled = _build_remote_list_rows_for_group(
            group, group_name, "ups",
        )
        rows.extend(group_rows)
        enabled_count += group_enabled
    for group in config.redundancy_groups:
        group_rows, group_enabled = _build_remote_list_rows_for_group(
            group, group.name, "redundancy",
        )
        rows.extend(group_rows)
        enabled_count += group_enabled

    if not rows:
        print("No remote targets configured.")
        sys.exit(1)

    print(f"REMOTE TARGETS ({len(rows)} configured, {enabled_count} enabled)")
    print()
    print(_format_remote_list_table(rows))


def _select_remote_server(config, server_ref: str, group_ref: str = None):
    """Select exactly one remote server from config."""
    matches = []
    for owner_label, owner_name, server in _iter_remote_server_owners(config):
        if not server.enabled:
            continue
        names = {server.name, server.host, server.name or server.host}
        if server_ref in names:
            if group_ref and group_ref not in {owner_label, owner_name}:
                continue
            matches.append((owner_label, owner_name, server))
    if not matches:
        raise SystemExit(f"ERROR: enabled remote server {server_ref!r} not found")
    if len(matches) > 1:
        owners = ", ".join(owner for owner, _, _ in matches)
        raise SystemExit(
            f"ERROR: remote server {server_ref!r} is ambiguous. "
            f"Use --group. Matches: {owners}"
        )
    return matches[0]


def _cmd_shutdown_remote(args):
    """Run a manual one-server remote shutdown drill."""
    config = _load_config(args)
    _exit_on_config_errors(config, args)

    if not args.dry_run and not args.confirm:
        print(
            "ERROR: real remote shutdown requires "
            "--i-really-want-to-proceed-with-remote-shutdown"
        )
        sys.exit(2)

    owner_label, owner_name, server = _select_remote_server(
        config, args.server, args.group,
    )

    logger = _CLILogger(args.log_file)
    logger.log(f"Manual remote shutdown drill: {server.name or server.host}")
    logger.log(f"  Group: {owner_label}")
    logger.log(f"  Host: {server.user}@{server.host}")
    logger.log(f"  Mode: {'dry-run' if args.dry_run else 'REAL SHUTDOWN'}")

    if args.connectivity_check:
        probe = config.remote_health.probe_command
        if not is_safe_probe_command(probe):
            logger.log("  Connectivity check: skipped (unsafe probe command rejected)")
        else:
            ok, error, latency = run_remote_probe(server, probe)
            if ok:
                logger.log(f"  Connectivity check: OK ({latency} ms)")
            else:
                logger.log(f"  Connectivity check: FAILED ({error})")

    drill_config = Config(
        ups_groups=[
            UPSGroupConfig(
                ups=UPSConfig(name=owner_name or owner_label,
                              display_name=owner_label),
                remote_servers=[server],
                is_local=False,
            )
        ],
        behavior=config.behavior,
        logging=config.logging,
        notifications=config.notifications,
        local_shutdown=config.local_shutdown,
        statistics=config.statistics,
        api=config.api,
        prometheus=config.prometheus,
        remote_health=config.remote_health,
        mqtt=config.mqtt,
    )
    # PRECEDENCE NOTE: the drill follows the CLI flag, NOT the config-level
    # ``behavior.dry_run`` setting. The drill is a per-invocation operator
    # tool gated by ``--i-really-want-to-proceed-with-remote-shutdown``;
    # that explicit confirmation flag is the safety contract, and the
    # ``--dry-run`` flag is what the operator picks per drill. Config-level
    # ``behavior.dry_run: true`` does NOT silently downgrade a confirmed
    # drill to dry-run — see docs/remote-servers.md "Manual remote
    # shutdown drill" for the full precedence rationale. If you're
    # auditing this for safety: the drill cannot run real commands
    # without ``--i-really-want-to-proceed-with-remote-shutdown`` (line
    # 426-431 above).
    drill_config.behavior.dry_run = bool(args.dry_run)

    monitor = UPSGroupMonitor(drill_config)
    monitor.logger = logger
    monitor._notification_worker = None

    if args.dry_run:
        logger.log("  Dry-run: configured remote commands will not be executed.")
    monitor._shutdown_remote_server(server)
    logger.log("Manual remote shutdown drill complete.")


def _resolve_group_for_rehearsal(config, group_ref: str):
    """Locate a UPS or redundancy group by name for the rehearsal command.

    Returns ``(kind, group)`` where ``kind`` is ``"ups"`` or
    ``"redundancy"``. Matches UPS groups against the friendly label
    first and the canonical name second; redundancy groups against
    ``RedundancyGroupConfig.name``. Raises SystemExit with an
    operator-friendly message when nothing matches or the name is
    ambiguous (within or across kinds).
    """
    matches = []
    for group in config.ups_groups:
        if group_ref in {group.ups.label, group.ups.name}:
            matches.append(("ups", group))
    for group in config.redundancy_groups:
        if group_ref == group.name:
            matches.append(("redundancy", group))
    if not matches:
        raise SystemExit(
            f"ERROR: group {group_ref!r} not found. "
            f"Use 'eneru remote list' to see configured names."
        )
    if len(matches) > 1:
        described = ", ".join(
            f"{(g.ups.label if kind == 'ups' else g.name) or '(unnamed)'} ({kind})"
            for kind, g in matches
        )
        raise SystemExit(
            f"ERROR: group {group_ref!r} matches multiple groups: "
            f"{described}. Rename one of them or open an issue if you "
            f"need a --kind flag."
        )
    return matches[0]


def _cmd_shutdown_group(args):
    """Run a manual full-sequence shutdown rehearsal for one named group."""
    import atexit
    import shutil
    import tempfile
    import threading

    config = _load_config(args)
    _exit_on_config_errors(config, args)

    if not args.dry_run and not args.confirm:
        print(
            "ERROR: real group shutdown requires "
            "--i-really-want-to-proceed-with-group-shutdown"
        )
        sys.exit(2)

    kind, group = _resolve_group_for_rehearsal(config, args.group)

    logger = _CLILogger(args.log_file)
    label = group.ups.label if kind == "ups" else group.name
    logger.log(f"Manual group shutdown rehearsal: {label}")
    logger.log(f"  Kind: {kind}")
    logger.log(f"  Mode: {'dry-run' if args.dry_run else 'REAL SHUTDOWN'}")
    if kind == "redundancy":
        # The coordinator's local-poweroff callback isn't wired in this
        # one-shot path. Calling it would let an operator confirm a
        # "rehearsal" and accidentally halt the host because the
        # callback bypasses the per-rehearsal flag isolation. The
        # executor still drains is_local resources (VMs, containers,
        # filesystems) before that gated step, so a confirmed rehearsal
        # of an is_local redundancy group really does stop them.
        is_local_redundancy = bool(getattr(group, "is_local", False))
        if is_local_redundancy and not args.dry_run and args.confirm:
            logger.log(
                "  WARNING: this is_local redundancy group WILL stop "
                "local VMs/containers and unmount configured filesystems "
                "on this host. Only the final poweroff command is "
                "suppressed by the rehearsal."
            )
        else:
            logger.log(
                "  Note: redundancy rehearsal does not fire local "
                "poweroff even with confirm flag."
            )

    # Isolate per-rehearsal state files so the rehearsal can never
    # collide with a running daemon's flag/state files (which would
    # block the daemon from re-firing its own shutdowns). The
    # atexit hook is belt-and-braces: try/finally below covers normal
    # control flow, atexit covers an unexpected interpreter shutdown
    # before the finally runs (e.g. an unhandled SystemExit raised
    # deep in a mixin). Neither path covers SIGKILL — accepted, the
    # tempdir holds no secrets and is mode 0700.
    rehearsal_dir = Path(tempfile.mkdtemp(prefix="eneru-rehearsal-"))
    atexit.register(shutil.rmtree, str(rehearsal_dir), True)
    try:
        config.logging.shutdown_flag_file = str(
            rehearsal_dir / "rehearsal.shutdown-flag"
        )
        config.logging.battery_history_file = str(
            rehearsal_dir / "rehearsal.battery-history"
        )
        config.logging.state_file = str(
            rehearsal_dir / "rehearsal.state"
        )
        config.behavior.dry_run = bool(args.dry_run)

        if kind == "ups":
            drill_config = Config(
                ups_groups=[group],
                behavior=config.behavior,
                logging=config.logging,
                notifications=config.notifications,
                local_shutdown=config.local_shutdown,
                statistics=config.statistics,
                api=config.api,
                prometheus=config.prometheus,
                remote_health=config.remote_health,
                mqtt=config.mqtt,
            )
            monitor = UPSGroupMonitor(drill_config)
            monitor.logger = logger
            monitor._notification_worker = None
            # Detect container runtime + compose support before the
            # shutdown sequence runs. The daemon does this in
            # _initialize() before its main loop; the rehearsal goes
            # straight to _execute_shutdown_sequence(), so without this
            # call _container_runtime stays None and the containers
            # phase is silently skipped — defeating the point of a
            # rehearsal. Mirrors what RedundancyGroupExecutor does in
            # its own __init__.
            monitor._check_dependencies()
            monitor._execute_shutdown_sequence()
        else:
            executor = RedundancyGroupExecutor(
                group,
                base_config=config,
                logger=logger,
                stop_event=threading.Event(),
                notification_worker=None,
                local_shutdown_callback=None,
            )
            executor.shutdown(reason="manual rehearsal via CLI")
    finally:
        shutil.rmtree(rehearsal_dir, ignore_errors=True)

    logger.log("Manual group shutdown rehearsal complete.")


def _cmd_version(args):
    """Print version and exit."""
    print(f"Eneru v{__version__}")


def _cmd_completion(args):
    """Print a self-contained shell completion script to stdout.

    Scripts live inside the ``eneru.completion`` subpackage so they ship
    with both pip and deb/rpm installs and can be read via
    ``importlib.resources`` regardless of how the package was installed.
    nfpm.yaml additionally drops them at the canonical FHS paths so the
    host shell auto-loads them when bash-completion / zsh / fish is
    present. PyPI users source the runtime output directly:
    ``source <(eneru completion bash)``.
    """
    import importlib.resources

    shell = args.shell
    filename = {"bash": "eneru.bash",
                "zsh":  "eneru.zsh",
                "fish": "eneru.fish"}[shell]
    try:
        text = (importlib.resources.files("eneru.completion") / filename).read_text()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        print(f"Error: completion script for '{shell}' not found ({exc})",
              file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(text)


def _cmd_deliver_stop(args):
    """v5.2.1 internal subcommand — invoked by the systemd-run timer
    scheduled at the previous daemon's exit. Idempotent:

    - If the lifecycle 'Service Stopped' row was already cancelled by
      the next daemon's classifier (`status='cancelled'`,
      `cancel_reason='superseded'`), we exit silently — the user gets
      a single Restarted/Upgraded/Recovered notification.
    - If the row is still `pending` (no replacement daemon came up),
      we deliver via Apprise and mark the row `sent` — the user gets
      a single Stopped notification.

    The subcommand name is prefixed with `_` to mark it as internal;
    it's intentionally absent from the `--help` listing.
    """
    from pathlib import Path
    from eneru.deferred_delivery import deliver_pending_stop

    config = _load_config(args)
    sys.exit(deliver_pending_stop(
        notification_id=int(args.notification_id),
        db_path=Path(args.db_path),
        config=config,
    ))


def _cmd_monitor(args):
    """Launch the TUI dashboard."""
    config = _load_config(args)

    from eneru.tui import run_tui, run_once, EVENTS_MAX_ROWS_NORMAL

    if args.once:
        run_once(
            config,
            graph_metric=getattr(args, "graph", None),
            time_range=getattr(args, "time", "1h"),
            events_only=getattr(args, "events_only", False),
            verbose=getattr(args, "verbose", False),
            length=getattr(args, "length", EVENTS_MAX_ROWS_NORMAL),
        )
    else:
        run_tui(
            config,
            interval=args.interval,
            initial_graph=getattr(args, "graph", None),
            initial_time_range=getattr(args, "time", "1h"),
            verbose=getattr(args, "verbose", False),
        )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Eneru - Intelligent UPS Monitoring & Shutdown Orchestration for NUT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "subcommands:\n"
            "  run                  Start the monitoring daemon\n"
            "  remote list          List configured remote shutdown targets\n"
            "  shutdown remote      Manually drill one configured remote shutdown\n"
            "  shutdown group       Rehearse the full shutdown sequence for one group\n"
            "  validate             Validate configuration and show overview\n"
            "  monitor / tui        Launch real-time TUI dashboard\n"
            "  test-notifications   Send a test notification\n"
            "  completion           Print shell completion script (bash/zsh/fish)\n"
            "  version              Show version information\n"
            "\nExamples:\n"
            "  eneru run --config /etc/ups-monitor/config.yaml\n"
            "  eneru remote list --config /etc/ups-monitor/config.yaml\n"
            "  eneru shutdown group --group rack-a --dry-run --config /etc/ups-monitor/config.yaml\n"
            "  eneru validate --config /etc/ups-monitor/config.yaml\n"
            "  eneru monitor --config /etc/ups-monitor/config.yaml\n"
            "  eneru tui --config /etc/ups-monitor/config.yaml\n"
        ),
    )

    subparsers = parser.add_subparsers(dest="command")

    # --- run ---
    run_parser = subparsers.add_parser("run", help="Start the monitoring daemon")
    run_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    run_parser.add_argument("--dry-run", action="store_true",
                            help="Run in dry-run mode (overrides config)")
    run_parser.add_argument("--api", action="store_true",
                            help="Enable the embedded read-only API")
    run_parser.add_argument("--api-bind",
                            help="API listen address (implies --api)")
    run_parser.add_argument("--api-port", type=int,
                            help="API listen port (implies --api)")
    run_parser.add_argument("--exit-after-shutdown", action="store_true",
                            help="Exit after completing shutdown sequence")
    run_parser.set_defaults(func=_cmd_run)

    # --- shutdown remote ---
    shutdown_parser = subparsers.add_parser("shutdown", help="Manual shutdown drills")
    shutdown_sub = shutdown_parser.add_subparsers(dest="shutdown_command")
    remote_parser = shutdown_sub.add_parser(
        "remote",
        help="Run a manual shutdown drill for one configured remote server",
    )
    remote_parser.add_argument("-c", "--config", help="Path to configuration file",
                               default=None)
    remote_parser.add_argument("--server", required=True,
                               help="Remote server name or host from config")
    remote_parser.add_argument("--group",
                               help="UPS/redundancy group when server name is ambiguous")
    remote_parser.add_argument("--dry-run", action="store_true",
                               help="Do not execute configured remote commands")
    remote_parser.add_argument(
        "--i-really-want-to-proceed-with-remote-shutdown",
        dest="confirm",
        action="store_true",
        help="Required for real remote command execution",
    )
    remote_parser.add_argument("--connectivity-check", dest="connectivity_check",
                               action="store_true", default=True,
                               help="Run harmless SSH probe first (default)")
    remote_parser.add_argument("--no-connectivity-check", dest="connectivity_check",
                               action="store_false",
                               help="Skip harmless SSH probe")
    remote_parser.add_argument("--log-file",
                               help="Optional file to append this drill log to")
    remote_parser.set_defaults(func=_cmd_shutdown_remote)

    # --- shutdown group ---
    group_parser = shutdown_sub.add_parser(
        "group",
        help="Rehearse the full configured shutdown sequence for one group",
    )
    group_parser.add_argument("-c", "--config", help="Path to configuration file",
                              default=None)
    group_parser.add_argument("--group", required=True,
                              help="UPS group label/name or redundancy group name")
    group_parser.add_argument("--dry-run", action="store_true",
                              help="Log every phase without executing real commands")
    group_parser.add_argument(
        "--i-really-want-to-proceed-with-group-shutdown",
        dest="confirm",
        action="store_true",
        help=(
            "Required for real execution. For UPS groups this WILL halt the "
            "host if local_shutdown.enabled. Redundancy groups never fire "
            "local poweroff from the CLI rehearsal."
        ),
    )
    group_parser.add_argument("--log-file",
                              help="Optional file to append this rehearsal log to")
    group_parser.set_defaults(func=_cmd_shutdown_group)

    # --- remote list ---
    remote_top_parser = subparsers.add_parser(
        "remote", help="Inspect configured remote shutdown targets",
    )
    remote_top_sub = remote_top_parser.add_subparsers(dest="remote_command")
    remote_list_parser = remote_top_sub.add_parser(
        "list", help="List configured remote shutdown targets",
    )
    remote_list_parser.add_argument(
        "-c", "--config", help="Path to configuration file", default=None,
    )
    remote_list_parser.set_defaults(func=_cmd_remote_list)

    # --- validate ---
    val_parser = subparsers.add_parser("validate", help="Validate configuration and show overview")
    val_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    val_parser.set_defaults(func=_cmd_validate)

    # --- monitor / tui ---
    # `tui` is an alias for `monitor` -- same handler, same options. We
    # register two parsers (rather than argparse `aliases=`) so each shows
    # up as a first-class entry in the top-level help and gets its own
    # `--help` page that names the subcommand the user actually typed.
    def _add_monitor_args(p):
        p.add_argument("-c", "--config", help="Path to configuration file", default=None)
        p.add_argument("--once", action="store_true",
                       help="Print status snapshot and exit (no TUI)")
        p.add_argument("--interval", type=int, default=5,
                       help="Refresh interval in seconds (default: 5)")
        p.add_argument("--graph",
                       choices=["charge", "load", "voltage", "runtime"],
                       help="Initial graph metric. With --once renders a Braille snapshot; "
                            "in interactive TUI pre-selects the metric (still cycle with <G>)")
        # IMPORTANT: --time is GRAPH-ONLY in 5.2.2+. It must NOT be
        # threaded into the events query. Events are sparse and a fixed
        # window made the panel silently empty for normal homelab usage
        # (the events panel then fell back to log parsing without the
        # operator noticing). Use --length to size the events list.
        p.add_argument("--time", default="1h",
                       help="Graph time range (1h/6h/24h/7d/30d). Applies only to the "
                            "graph -- the events list is independent (use --length to size it)")
        p.add_argument("--events-only", action="store_true",
                       help="With --once: print only the events list (SQLite, log-tail fallback)")
        p.add_argument("--verbose", "-v", action="count", default=0,
                       help="Increase event verbosity. Default shows Power Events only; "
                            "-v adds Diagnostics; -vv adds Lifecycle. Applies to both "
                            "--once and the interactive TUI; cycle in-session with <V>")
        p.add_argument("--length", type=_non_negative_int, default=30,
                       metavar="N",
                       help="With --once: max events to print (default: 30, 0 = no cap). "
                            "Power events are always preserved within the cap; diagnostics "
                            "fill next, lifecycle fills last")
        p.set_defaults(func=_cmd_monitor)

    mon_parser = subparsers.add_parser("monitor", help="Launch real-time TUI dashboard")
    _add_monitor_args(mon_parser)

    tui_parser = subparsers.add_parser(
        "tui", help="Alias for 'monitor' -- launch real-time TUI dashboard")
    _add_monitor_args(tui_parser)

    # --- test-notifications ---
    tn_parser = subparsers.add_parser("test-notifications",
                                      help="Send a test notification and exit")
    tn_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    tn_parser.set_defaults(func=_cmd_test_notifications)

    # --- version ---
    ver_parser = subparsers.add_parser("version", help="Show version information")
    ver_parser.set_defaults(func=_cmd_version)

    # --- completion ---
    comp_parser = subparsers.add_parser(
        "completion",
        help="Print shell completion script (source it: source <(eneru completion bash))")
    comp_parser.add_argument("shell", choices=["bash", "zsh", "fish"],
                             help="Shell to emit completion for")
    comp_parser.set_defaults(func=_cmd_completion)

    # --- _deliver-stop (internal, v5.2.1) ---
    # Hidden from the --help listing on purpose: this is invoked by a
    # systemd-run transient timer scheduled by the previous daemon's
    # _cleanup_and_exit / _handle_signal, never by users directly.
    ds_parser = subparsers.add_parser("_deliver-stop", help=argparse.SUPPRESS)
    ds_parser.add_argument("--notification-id", required=True, type=int)
    ds_parser.add_argument("--db-path", required=True)
    ds_parser.add_argument("-c", "--config", required=True,
                           help="Path to configuration file")
    ds_parser.set_defaults(func=_cmd_deliver_stop)

    args = parser.parse_args()

    # No subcommand provided -> show help
    if args.command is None or not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
