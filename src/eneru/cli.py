"""CLI entry point for Eneru."""

import argparse
import sys
from datetime import datetime

from eneru.version import __version__
from eneru.config import ConfigLoader
from eneru.monitor import UPSMonitor, MultiUPSCoordinator
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

    if config.multi_ups:
        coordinator = MultiUPSCoordinator(config, exit_after_shutdown=args.exit_after_shutdown)
        coordinator.run()
    else:
        monitor = UPSMonitor(config, exit_after_shutdown=args.exit_after_shutdown)
        monitor.run()


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

    # Only show local resources if is_local (or single-UPS legacy)
    if group.is_local or not multi_ups:
        print(f"{prefix}  VMs enabled: {group.virtual_machines.enabled}")
        containers = group.containers
        print(f"{prefix}  Containers enabled: {containers.enabled}", end="")
        if containers.enabled:
            compose_count = len(containers.compose_files)
            if compose_count > 0:
                print(f" (runtime: {containers.runtime}, {compose_count} compose file(s))")
            else:
                print(f" (runtime: {containers.runtime})")
        else:
            print()
        print(f"{prefix}  Filesystems sync: {group.filesystems.sync_enabled}", end="")
        if group.filesystems.unmount.enabled:
            mount_count = len(group.filesystems.unmount.mounts)
            print(f", unmount: {mount_count} mount(s)")
        else:
            print()

    enabled_servers = [s for s in group.remote_servers if s.enabled]
    print(f"{prefix}  Remote servers: {len(enabled_servers)}")


def _cmd_validate(args):
    """Validate configuration and print overview."""
    config = _load_config(args)
    exit_code = 0

    print(f"Eneru v{__version__}")
    print("Configuration is valid.")

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

    # Run validation checks
    messages = ConfigLoader.validate_config(config)
    if messages:
        print()
        for msg in messages:
            print(f"  {msg}")
            if msg.startswith("ERROR"):
                exit_code = 1

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
        run_once(config)
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
            "  eneru validate --config config.yaml\n"
            "  eneru monitor --config config.yaml\n"
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
