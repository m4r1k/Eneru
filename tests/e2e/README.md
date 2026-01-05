# Eneru E2E Tests

End-to-end tests for Eneru using real NUT, SSH, and Docker services.

## Overview

These tests spin up a complete test environment:

- **NUT Server** with dummy driver (simulates UPS states)
- **SSH Target** (simulates remote server for shutdown commands)
- **Docker Containers** (targets for container shutdown)
- **tmpfs Mount** (for unmount testing)

## Running Locally

### Prerequisites

- Docker and Docker Compose
- Python 3.9+
- NUT client (`apt install nut-client` or `dnf install nut`)

### Quick Start

```bash
# From repository root
cd tests/e2e

# Generate SSH keys
ssh-keygen -t ed25519 -f /tmp/e2e-ssh-key -N ""
cp /tmp/e2e-ssh-key.pub ssh-target/authorized_keys

# Start environment
docker compose up -d --build

# Wait for services
sleep 10

# Verify NUT is working
upsc TestUPS@localhost:3493

# Run Eneru against the test environment
eneru --validate-config --config config-e2e-dry-run.yaml
```

### Simulating Scenarios

Apply different UPS states by copying scenario files:

```bash
# Normal operation
cp scenarios/online-charging.dev scenarios/apply.dev

# Power failure (triggers shutdown)
cp scenarios/low-battery.dev scenarios/apply.dev

# Forced shutdown
cp scenarios/fsd.dev scenarios/apply.dev
```

### Cleanup

```bash
docker compose down -v
```

## Test Scenarios

| File | Description | Triggers Shutdown? |
|------|-------------|-------------------|
| `online-charging.dev` | Normal operation, fully charged | No |
| `on-battery.dev` | On battery, battery OK | No |
| `low-battery.dev` | Battery below 20% threshold | Yes |
| `critical-runtime.dev` | Runtime below 600s threshold | Yes |
| `fsd.dev` | UPS signals Forced Shutdown | Yes |
| `avr-boost.dev` | AVR boosting low voltage | No |
| `brownout.dev` | Voltage below warning threshold | No |
| `overload.dev` | UPS overloaded | No |

## Configuration Files

| File | Purpose |
|------|---------|
| `config-e2e.yaml` | Full E2E testing (real execution) |
| `config-e2e-dry-run.yaml` | Detection testing (no real actions) |
| `config-e2e-notifications.yaml` | Notification testing |

## GitHub Actions

The E2E tests run automatically on push/PR to main via `.github/workflows/e2e.yml`.

### Notification Testing

To test notifications in CI, add the `E2E_NOTIFICATION_URL` secret to your repository.
This should be an Apprise-compatible URL (e.g., Discord webhook, Slack, etc.).

Example Discord URL format:
```
discord://webhook_id/webhook_token/
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Test Environment                          │
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │  NUT Server  │    │  SSH Target  │    │   Target     │   │
│  │  (dummy-ups) │    │   (sshd)     │    │  Containers  │   │
│  │  :3493       │    │   :2222      │    │              │   │
│  └──────────────┘    └──────────────┘    └──────────────┘   │
│         │                   │                   │            │
│         └───────────────────┼───────────────────┘            │
│                             │                                │
│                     ┌───────▼───────┐                        │
│                     │    Eneru      │                        │
│                     │  (under test) │                        │
│                     └───────────────┘                        │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```
