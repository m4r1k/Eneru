# Notifications

Eneru uses [Apprise](https://github.com/caronc/apprise) to send notifications to 100+ services including Discord, Slack, Telegram, ntfy, Pushover, Email, and many more.

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

## Popular Services

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

### More Services

Apprise supports 100+ notification services. See the [Apprise Wiki](https://github.com/caronc/apprise/wiki) for the complete list and URL formats.

---

## Testing Notifications

Before relying on notifications during a power event, test them:

```bash
# Send a test notification
sudo python3 /opt/ups-monitor/eneru.py --test-notifications

# Combine with config validation
sudo python3 /opt/ups-monitor/eneru.py --validate-config --test-notifications
```

---

## Persistent Retry Architecture

During power outages, network connectivity is often temporarily unavailable. Eneru uses a **non-blocking persistent retry** notification system that:

1. **Never blocks shutdown operations** - main thread queues instantly and continues
2. **Retries until success** - worker thread persistently retries failed notifications
3. **Preserves order** - FIFO queue ensures messages arrive in the correct sequence

This design ensures you receive all notifications about power events, even during brief outages where the network recovers before the system shuts down.

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

### Why Persistent Retry Matters

| Scenario | Fire-and-Forget (v4.6) | Persistent Retry (v4.7+) |
|----------|------------------------|--------------------------|
| 30-second network blip | Notifications lost | Retried and delivered |
| Router reboot during outage | Notifications lost | Retried and delivered |
| Transient DNS failure | Notifications lost | Retried and delivered |
| Network down until shutdown | Messages dropped at exit | Same (logs in journalctl) |
| Multiple services configured | All fail simultaneously | Apprise retries to all |

### The 5-Second Grace Period

After all critical shutdown operations complete, Eneru waits 5 seconds before issuing the final `shutdown -h now` command. This grace period allows queued notifications to be sent if the network is available, without risking data loss if it's not.

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

### 30-Second Power Blip

During a brief power blip, power may be restored within seconds or minutes — well before any shutdown triggers fire. However, public Internet often remains unreachable for several more minutes while local network equipment boots up (router, modem, switches, WiFi APs etc).

The persistent retry architecture handles this gracefully: notifications queue instantly and the worker keeps retrying every `retry_interval` seconds. As soon as the network is back, all messages are delivered in order — giving you full visibility into what happened.

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

## Notification Events

Eneru sends notifications for:

| Event | Description |
|-------|-------------|
| Service Start | Eneru daemon started |
| Service Stop | Eneru daemon stopped (graceful) |
| Power Lost | UPS switched to battery |
| Power Restored | UPS back on line power |
| Shutdown Triggered | Emergency shutdown initiated |
| Voltage Events | Brownout, over-voltage, AVR activation |
| Overload | UPS load threshold exceeded |

---

## Troubleshooting

### Notifications Not Arriving

1. **Test Apprise directly:**
   ```bash
   python3 -c "
   import apprise
   ap = apprise.Apprise()
   ap.add('discord://webhook_id/webhook_token')
   result = ap.notify(body='Test from Apprise', title='Test')
   print('Success' if result else 'Failed')
   "
   ```

2. **Check URL format:** Each service has a specific URL format. Refer to the [Apprise Wiki](https://github.com/caronc/apprise/wiki).

3. **Verify network access:** Can the server reach the notification service?
   ```bash
   curl -I https://discord.com
   ```

4. **Check logs:** Notification errors are logged to `/var/log/ups-monitor.log`.

### Rate Limiting

Some services (especially Discord) have rate limits. If you're testing frequently, you may hit these limits. In production, power events are infrequent enough that this shouldn't be an issue.
