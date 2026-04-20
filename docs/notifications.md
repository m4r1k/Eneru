# Notifications

Eneru uses [Apprise](https://github.com/caronc/apprise) to send notifications to 100+ services: Discord, Slack, Telegram, ntfy, Pushover, Email, and others.

---

## Configuration

```yaml
notifications:
  # Optional title for notifications
  title: "⚡ Homelab UPS"

  # Avatar/icon URL (supported by Discord, Slack, and others)
  avatar_url: "https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-avatar.png"

  # Timeout for notification delivery (seconds)
  timeout: 10

  # Seconds between retry attempts for failed notifications (default: 5)
  retry_interval: 5

  # Notification service URLs
  urls:
    - "discord://webhook_id/webhook_token"
    - "slack://token_a/token_b/token_c/#channel"
    - "telegram://bot_token/chat_id"
```

---

## Popular services

### Discord

1. In your Discord server, go to **Server Settings → Integrations → Webhooks**
2. Click **New Webhook** and copy the webhook URL
3. Extract the ID and token from the URL:
   ```
   https://discord.com/api/webhooks/1234567890/abcdefghijk...
                                    └─ ID ─┘   └─ Token ─┘
   ```
4. Add to config:
   ```yaml
   urls:
     - "discord://1234567890/abcdefghijk..."
   ```

### Slack

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Add **Incoming Webhooks** feature
3. Create a webhook for your channel
4. Use the webhook URL format:
   ```yaml
   urls:
     - "slack://TokenA/TokenB/TokenC/#channel"
   ```

### Telegram

1. Create a bot via [@BotFather](https://t.me/botfather) and get the token
2. Get your chat ID by messaging the bot and checking:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Add to config:
   ```yaml
   urls:
     - "telegram://bot_token/chat_id"
   ```

### ntfy

[ntfy](https://ntfy.sh/) is a simple pub-sub notification service:

```yaml
urls:
  - "ntfy://ntfy.sh/your-topic-name"
  # Or self-hosted:
  - "ntfy://your-server.com/your-topic"
```

### Pushover

```yaml
urls:
  - "pover://user_key@app_token"
```

### Email (SMTP)

```yaml
urls:
  - "mailto://user:password@smtp.gmail.com:587?to=recipient@example.com"
```

### More services

Apprise supports 100+ notification services. See the [Apprise Wiki](https://github.com/caronc/apprise/wiki) for the complete list and URL formats.

---

## Testing notifications

Test notifications before relying on them during a power event:

```bash
# Send a test notification
eneru test-notifications --config /etc/ups-monitor/config.yaml

# Validate configuration
eneru validate --config /etc/ups-monitor/config.yaml
```

---

## Persistent retry architecture

Network connectivity is often temporarily down during power outages. Eneru uses a non-blocking persistent retry system: the main thread queues notifications instantly and continues with shutdown operations, while a worker thread retries failed sends until they succeed. A FIFO queue preserves message order.

All notifications are delivered as long as the network recovers before the system shuts down.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                ENERU NOTIFICATION ARCHITECTURE.                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   MAIN THREAD (Critical Path)          WORKER THREAD (Persistent Retry)     │
│   ═══════════════════════════          ════════════════════════════════     │
│                                                                             │
│   ┌─────────────────────┐                                                   │
│   │ Shutdown Triggered  │                                                   │
│   └──────────┬──────────┘                                                   │
│              │                                                              │
│              ▼                                                              │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Queue Notification  │────────▶│  Notification Queue │                   │
│   │ (non-blocking)      │         │  ┌───┬───┬───┬───┐  │                   │
│   └──────────┬──────────┘         │  │ 1 │ 2 │ 3 │...│  │  FIFO Order       │
│              │                    │  └───┴───┴───┴───┘  │                   │
│              │ continues          └──────────┬──────────┘                   │
│              │ immediately                   │                              │
│              ▼                               ▼                              │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Stop VMs            │         │ Attempt Send        │                   │
│   └──────────┬──────────┘         └──────────┬──────────┘                   │
│              │                               │                              │
│              ▼                               ▼                              │
│   ┌─────────────────────┐              ┌─────────┐                          │
│   │ Stop Containers     │              │ Success?│                          │
│   └──────────┬──────────┘              └────┬────┘                          │
│              │                         YES/ \NO                             │
│              ▼                            /   \                             │
│   ┌─────────────────────┐         ┌─────┐     ┌──────────────┐              │
│   │ Unmount Filesystems │         │ ACK │     │ Wait & Retry │              │
│   └──────────┬──────────┘         │ ──▶ │     │ (5s default) │              │
│              │                    │Next │     └──────┬───────┘              │
│              ▼                    │ Msg │            │                      │
│   ┌─────────────────────┐         └─────┘            │                      │
│   │ Shutdown Remote     │                            ▼                      │
│   │ Servers             │                   ┌────────────────┐              │
│   └──────────┬──────────┘                   │ Network back?  │──▶ Retry     │
│              │                              └────────────────┘              │
│              ▼                                                              │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ 5-Second Grace      │         │    KEY BENEFITS     │                   │
│   │ (retry window)      │         ├─────────────────────┤                   │
│   └──────────┬──────────┘         │ ✓ Zero blocking     │                   │
│              │                    │ ✓ Persistent retry  │                   │
│              ▼                    │ ✓ FIFO ordering     │                   │
│   ┌─────────────────────┐         │ ✓ No message loss*  │                   │
│   │ shutdown -h now     │         │ ✓ Graceful stop     │                   │
│   └─────────────────────┘         └─────────────────────┘                   │
│                                   * until process exit                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Configuration

```yaml
notifications:
  # ... other settings ...

  # Seconds between retry attempts (default: 5)
  retry_interval: 5
```

### Comparison with fire-and-forget

| Scenario | Fire-and-Forget (v4.6) | Persistent Retry (v4.7+) |
|----------|------------------------|--------------------------|
| 30-second network blip | Notifications lost | Retried and delivered |
| Router reboot during outage | Notifications lost | Retried and delivered |
| Transient DNS failure | Notifications lost | Retried and delivered |
| Network down until shutdown | Messages dropped at exit | Same (logs in journalctl) |
| Multiple services configured | All fail simultaneously | Apprise retries to all |

### The 5-second grace period

After all shutdown operations complete, Eneru waits 5 seconds before issuing `shutdown -h now`. This gives queued notifications a window to send if the network is available.

```
Timeline (worst case - network down):
─────────────────────────────────────────────────────────────────
0s     │ Shutdown triggered, notification queued (instant)
0.1s   │ VMs stopping...
15s    │ VMs stopped, containers stopping...
30s    │ Containers stopped, filesystems synced...
45s    │ Remote servers notified...
50s    │ All critical operations complete
50-55s │ Grace period (notifications attempted)
55s    │ shutdown -h now executed
─────────────────────────────────────────────────────────────────
        Total: ~55 seconds (network issues added 0 seconds delay)
```

### 30-second power blip

During a brief power blip, power may return within seconds, well before any shutdown triggers fire. But the public Internet often stays unreachable for several minutes while local network equipment reboots (router, modem, switches, WiFi APs).

Notifications queue instantly and the worker retries every `retry_interval` seconds. Once the network is back, all messages are delivered in order.

```
Timeline (brief power blip, network slow to recover):
─────────────────────────────────────────────────────────────────
0s     │ Power lost, "Power Lost" notification queued
5s     │ Retry #1 - network down
10s    │ Retry #2 - still failing
15s    │ Retry #3 - still failing
20s    │ Retry #4 - still failing
28s    │ Power restored, "Power Restored" notification queued
35s    │ Retry #5 - still failing (switches coming up)
40s    | Retry #6 - still failing (router booting)
45s    │ Retry #7 - still failing (WiFi AP booting)
50s    │ Retry #8 - still failing (ISP modem syncing)
60s    │ Network is back!
60s    │ Retry #9 - SUCCESS! ✓ "Power Lost" delivered
60s    │ ✓ "Power Restored" delivered (next in queue)
─────────────────────────────────────────────────────────────────
        Both notifications delivered despite 60-second network outage
```

---

## Notification events

Eneru sends notifications for these events:

| Event | Description |
|-------|-------------|
| Service Start | Eneru daemon started |
| Service Stop | Eneru daemon stopped (graceful) |
| Power Lost | UPS switched to battery |
| Power Restored | UPS back on line power |
| Shutdown Triggered | Emergency shutdown initiated |
| Voltage Events | Brownout, over-voltage, AVR activation |
| Battery Anomaly | Charge dropped >20% while on line power (recalibration, aging, hardware fault). Must persist across 3 polls to filter jitter from APC, CyberPower, and Ubiquiti UniFi UPS units |
| Overload | UPS load threshold exceeded |

---

## Tuning alert noise

Issue [#27](https://github.com/m4r1k/Eneru/issues/27) reported "a lot
of email" from chatty UPS firmware on a US 120V grid. Eneru ships
two knobs to tune notification volume — both designed so a
misconfiguration cannot silence a real safety-critical event.

### `voltage_hysteresis_seconds` — debounce transient flaps

```yaml
notifications:
  voltage_hysteresis_seconds: 30   # default; 0 = legacy immediate
```

When `voltage_state` transitions to HIGH or LOW, the
`OVER_VOLTAGE_DETECTED` / `BROWNOUT_DETECTED` log line and SQLite
event row are written **immediately** (operational record is
sacred). The notification dispatch is held for
`voltage_hysteresis_seconds`. If the condition reverts inside the
window, no notification fires and a `VOLTAGE_FLAP_SUPPRESSED` event
is recorded for visibility:

```bash
sqlite3 /var/lib/eneru/<UPS>.db \
  "SELECT ts, event_type, detail FROM events
   WHERE event_type='VOLTAGE_FLAP_SUPPRESSED'
   ORDER BY ts DESC LIMIT 10;"
```

If the condition persists past the dwell, the notification fires
with a `(persisted Ns)` annotation so you can see it was held.

### `notifications.suppress` — mute specific event types

```yaml
notifications:
  suppress:
    - AVR_BOOST_ACTIVE
    - AVR_TRIM_ACTIVE
    - AVR_INACTIVE
```

Mutes notifications for the listed event types. The events still
land in the log file and the SQLite `events` table — only the
notification dispatch is gated. The events table records
`notification_sent=0` for muted events so you can audit later:

```bash
sqlite3 /var/lib/eneru/<UPS>.db \
  "SELECT event_type, COUNT(*) FROM events
   WHERE notification_sent = 0
   GROUP BY event_type;"
```

**Suppressible event names** (any combination is accepted; case-
insensitive):

- `POWER_RESTORED`
- `VOLTAGE_NORMALIZED`
- `AVR_BOOST_ACTIVE`, `AVR_TRIM_ACTIVE`, `AVR_INACTIVE`
- `BYPASS_MODE_INACTIVE`
- `OVERLOAD_RESOLVED`
- `CONNECTION_RESTORED`
- `VOLTAGE_AUTODETECT_MISMATCH`
- `VOLTAGE_FLAP_SUPPRESSED`

### Safety-critical blocklist

The following event names **cannot** appear in
`notifications.suppress`. The config validator rejects them with a
clear error pointing at `voltage_hysteresis_seconds` for
flap-debounce:

- `OVER_VOLTAGE_DETECTED`
- `BROWNOUT_DETECTED`
- `OVERLOAD_ACTIVE`
- `BYPASS_MODE_ACTIVE`
- `ON_BATTERY`
- `CONNECTION_LOST`
- Anything starting with `SHUTDOWN_`

These exist to alert you to potential hardware damage or imminent
service loss; silencing them defeats the safety contract. If you're
seeing frequent over-voltage warnings on a US grid, the right fix
is the auto-detect re-snap (see
[Triggers → Voltage thresholds](triggers.md#voltage-thresholds-auto-detected-not-user-configurable)),
not muting the alert.

---

## Troubleshooting

### Notifications not arriving

Test Apprise directly:

```bash
python3 -c "
import apprise
ap = apprise.Apprise()
ap.add('discord://webhook_id/webhook_token')
result = ap.notify(body='Test from Apprise', title='Test')
print('Success' if result else 'Failed')
"
```

Check that the URL format matches the service. Each service has its own format documented in the [Apprise Wiki](https://github.com/caronc/apprise/wiki).

Verify the server can reach the notification service:

```bash
curl -I https://discord.com
```

Notification errors are logged to `/var/log/ups-monitor.log`.

### Rate limiting

Some services (especially Discord) have rate limits. If you're testing frequently, you may hit these limits. In production, power events are infrequent enough that this shouldn't be an issue.
