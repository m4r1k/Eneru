"""CLI entry point for Eneru."""

import argparse
import sys
from datetime import datetime

from eneru.version import __version__
from eneru.config import ConfigLoader
from eneru.monitor import UPSMonitor
from eneru.notifications import APPRISE_AVAILABLE

# Optional import for Apprise (needed for test notifications)
try:
    import apprise
except ImportError:
    apprise = None


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Eneru - Intelligent UPS Monitoring & Shutdown Orchestration for NUT"
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to configuration file (default: /etc/ups-monitor/config.yaml)",
        default=None
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (overrides config file setting)"
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate configuration file and exit"
    )
    parser.add_argument(
        "--test-notifications",
        action="store_true",
        help="Send a test notification and exit"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"Eneru v{__version__}"
    )
    parser.add_argument(
        "--exit-after-shutdown",
        action="store_true",
        help="Exit after completing shutdown sequence (useful for testing/scripting)"
    )

    args = parser.parse_args()

    # Load configuration
    config = ConfigLoader.load(args.config)

    # Override dry-run if specified on command line
    if args.dry_run:
        config.behavior.dry_run = True

    # Handle --validate-config and/or --test-notifications
    if args.validate_config or args.test_notifications:
        exit_code = 0

        # Validate config if requested
        if args.validate_config:
            print(f"Eneru v{__version__}")
            print("Configuration is valid.")
            print(f"  UPS: {config.ups.name}")
            print(f"  Dry-run: {config.behavior.dry_run}")
            print(f"  VMs enabled: {config.virtual_machines.enabled}")
            print(f"  Containers enabled: {config.containers.enabled}", end="")
            if config.containers.enabled:
                compose_count = len(config.containers.compose_files)
                if compose_count > 0:
                    print(f" (runtime: {config.containers.runtime}, {compose_count} compose file(s))")
                else:
                    print(f" (runtime: {config.containers.runtime})")
            else:
                print()
            print(f"  Filesystems sync: {config.filesystems.sync_enabled}", end="")
            if config.filesystems.unmount.enabled:
                mount_count = len(config.filesystems.unmount.mounts)
                print(f", unmount: {mount_count} mount(s)")
            else:
                print()
            print(f"  Remote servers: {len([s for s in config.remote_servers if s.enabled])}")

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
                    print(f"    ⚠️ Apprise not installed - notifications disabled")
                    print(f"    Install with: pip install apprise")
            else:
                print(f"    Disabled")

            # Run validation checks and print warnings/info
            messages = ConfigLoader.validate_config(config)
            if messages:
                print()
                for msg in messages:
                    print(f"  ℹ️ {msg}")

        # Test notifications if requested
        if args.test_notifications:
            if args.validate_config:
                print()  # Add separator between outputs
                print("-" * 50)
                print()

            print("Testing notifications...")

            if not config.notifications.enabled or not config.notifications.urls:
                print("❌ No notification URLs configured.")
                print("   Add URLs to the 'notifications.urls' section in your config file.")
                exit_code = 1
            elif not APPRISE_AVAILABLE:
                print("❌ Apprise is not installed.")
                print("   Install with: pip install apprise")
                exit_code = 1
            else:
                # Initialize Apprise
                apobj = apprise.Apprise()
                valid_urls = 0

                for url in config.notifications.urls:
                    if apobj.add(url):
                        valid_urls += 1
                        # Extract scheme without avatar params for display
                        scheme = url.split('://')[0] if '://' in url else 'unknown'
                        print(f"  ✅ Added: {scheme}://***")
                    else:
                        print(f"  ❌ Invalid URL: {url[:30]}...")

                if valid_urls == 0:
                    print("❌ No valid notification URLs found.")
                    exit_code = 1
                else:
                    print(f"\nSending test notification to {valid_urls} service(s)...")

                    if config.notifications.title:
                        print(f"  Title: {config.notifications.title}")
                    if config.notifications.avatar_url:
                        print(f"  Avatar: {config.notifications.avatar_url[:50]}...")

                    # Send test notification
                    test_body = (
                        "🧪 **Test Notification**\n"
                        "This is a test notification from Eneru.\n"
                        "If you see this, notifications are working correctly!\n"
                        f"\n---\n⚡ UPS: {config.ups.name}\n"
                        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}"
                    )

                    # Escape @ symbols to prevent Discord mentions (e.g., UPS@192.168.1.1)
                    escaped_body = test_body.replace("@", "@\u200B")  # Zero-width space after @

                    # Build notify kwargs
                    notify_kwargs = {
                        'body': escaped_body,
                        'notify_type': apprise.NotifyType.INFO,
                    }

                    # Only add title if configured
                    if config.notifications.title:
                        notify_kwargs['title'] = config.notifications.title

                    result = apobj.notify(**notify_kwargs)

                    if result:
                        print("✅ Test notification sent successfully!")
                    else:
                        print("❌ Failed to send test notification.")
                        print("   Check your notification URLs and network connectivity.")
                        exit_code = 1

        sys.exit(exit_code)

    # Run monitor
    monitor = UPSMonitor(config, exit_after_shutdown=args.exit_after_shutdown)
    monitor.run()


if __name__ == "__main__":
    main()
