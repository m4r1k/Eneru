"""CLI entry point for Eneru."""

import argparse
import sys
from datetime import datetime

from eneru.version import __version__
from eneru.config import ConfigLoader
from eneru.monitor import UPSGroupMonitor, compute_effective_order
from eneru.multi_ups import MultiUPSCoordinator
from eneru.notifications import APPRISE_AVAILABLE

# Optional import for Apprise (needed for test notifications)
try:
    import apprise
except ImportError:
    apprise = None


def _load_config(args):
    """Load configuration from the --config path."""
    return ConfigLoader.load(getattr(args, 'config', None))


def _cmd_run(args):
    """Start the monitoring daemon."""
    config = _load_config(args)

    if args.dry_run:
        config.behavior.dry_run = True

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

    # Run validation checks (pass raw YAML data for top-level resource warnings)
    raw_data = None
    if config.multi_ups and args.config:
        try:
            import yaml
            with open(args.config, 'r') as f:
                raw_data = yaml.safe_load(f) or {}
        except Exception:
            pass
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


def _cmd_version(args):
    """Print version and exit."""
    print(f"Eneru v{__version__}")


def _cmd_monitor(args):
    """Launch the TUI dashboard."""
    config = _load_config(args)

    from eneru.tui import run_tui, run_once

    if args.once:
        run_once(
            config,
            graph_metric=getattr(args, "graph", None),
            time_range=getattr(args, "time", "1h"),
            events_only=getattr(args, "events_only", False),
        )
    else:
        run_tui(config, interval=args.interval)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Eneru - Intelligent UPS Monitoring & Shutdown Orchestration for NUT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "subcommands:\n"
            "  run                  Start the monitoring daemon\n"
            "  validate             Validate configuration and show overview\n"
            "  monitor              Launch real-time TUI dashboard\n"
            "  test-notifications   Send a test notification\n"
            "  version              Show version information\n"
            "\nExamples:\n"
            "  eneru run --config /etc/ups-monitor/config.yaml\n"
            "  eneru validate --config /etc/ups-monitor/config.yaml\n"
            "  eneru monitor --config /etc/ups-monitor/config.yaml\n"
        ),
    )

    subparsers = parser.add_subparsers(dest="command")

    # --- run ---
    run_parser = subparsers.add_parser("run", help="Start the monitoring daemon")
    run_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    run_parser.add_argument("--dry-run", action="store_true",
                            help="Run in dry-run mode (overrides config)")
    run_parser.add_argument("--exit-after-shutdown", action="store_true",
                            help="Exit after completing shutdown sequence")
    run_parser.set_defaults(func=_cmd_run)

    # --- validate ---
    val_parser = subparsers.add_parser("validate", help="Validate configuration and show overview")
    val_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    val_parser.set_defaults(func=_cmd_validate)

    # --- monitor ---
    mon_parser = subparsers.add_parser("monitor", help="Launch real-time TUI dashboard")
    mon_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    mon_parser.add_argument("--once", action="store_true",
                            help="Print status snapshot and exit (no TUI)")
    mon_parser.add_argument("--interval", type=int, default=5,
                            help="Refresh interval in seconds (default: 5)")
    mon_parser.add_argument("--graph",
                            choices=["charge", "load", "voltage", "runtime"],
                            help="With --once: render an ASCII/Braille graph for the metric")
    mon_parser.add_argument("--time", default="1h",
                            help="With --once + --graph: time range (1h/6h/24h/7d/30d)")
    mon_parser.add_argument("--events-only", action="store_true",
                            help="With --once: print only the events list (SQLite, log-tail fallback)")
    mon_parser.set_defaults(func=_cmd_monitor)

    # --- test-notifications ---
    tn_parser = subparsers.add_parser("test-notifications",
                                      help="Send a test notification and exit")
    tn_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    tn_parser.set_defaults(func=_cmd_test_notifications)

    # --- version ---
    ver_parser = subparsers.add_parser("version", help="Show version information")
    ver_parser.set_defaults(func=_cmd_version)

    args = parser.parse_args()

    # No subcommand provided -> show help
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
