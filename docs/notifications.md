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
sudo python3 /opt/ups-monitor/ups_monitor.py --test-notifications

# Combine with config validation
sudo python3 /opt/ups-monitor/ups_monitor.py --validate-config --test-notifications
```

---

## Non-Blocking Architecture

During power outages, network connectivity is often unreliable or completely unavailable. Eneru uses a **fire-and-forget** notification system that ensures shutdown operations are never delayed by notification failures.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     ENERU NOTIFICATION ARCHITECTURE                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   MAIN THREAD (Critical Path)          WORKER THREAD (Best-Effort)          │
│   ═══════════════════════════          ════════════════════════════         │
│                                                                             │
│   ┌─────────────────────┐                                                   │
│   │ Shutdown Triggered  │                                                   │
│   └──────────┬──────────┘                                                   │
│              │                                                              │
│              ▼                                                              │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Queue Notification  │────────▶│  Notification Queue │                   │
│   │ (non-blocking)      │         │  ┌───┬───┬───┬───┐  │                   │
│   └──────────┬──────────┘         │  │ N │ N │ N │...│  │                   │
│              │                    │  └───┴───┴───┴───┘  │                   │
│              │ continues          └──────────┬──────────┘                   │
│              │ immediately                   │                              │
│              ▼                               ▼                              │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Stop VMs            │         │ Send to Discord     │──▶ Success/Fail   │
│   └──────────┬──────────┘         │ Send to Slack       │──▶ Success/Fail   │
│              │                    │ Send to Telegram    │──▶ Success/Fail   │
│              ▼                    └─────────────────────┘                   │
│   ┌─────────────────────┐                   │                               │
│   │ Stop Containers     │                   │ Network down?                 │
│   └──────────┬──────────┘                   │ Timeout? No problem!          │
│              │                              │ Worker handles it silently    │
│              ▼                              ▼                               │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Unmount Filesystems │         │ Thread terminates   │                   │
│   └──────────┬──────────┘         │ with process exit   │                   │
│              │                    └─────────────────────┘                   │
│              ▼                                                              │
│   ┌─────────────────────┐                                                   │
│   │ Shutdown Remote     │         ┌─────────────────────┐                   │
│   │ Servers             │         │    KEY BENEFITS     │                   │
│   └──────────┬──────────┘         ├─────────────────────┤                   │
│              │                    │ ✓ Zero blocking     │                   │
│              ▼                    │ ✓ Graceful failure  │                   │
│   ┌─────────────────────┐         │ ✓ Best-effort send  │                   │
│   │ 5-Second Grace      │         │ ✓ No data loss risk │                   │
│   │ (flush queue)       │         │ ✓ Daemon thread     │                   │
│   └──────────┬──────────┘         └─────────────────────┘                   │
│              │                                                              │
│              ▼                                                              │
│   ┌─────────────────────┐                                                   │
│   │ shutdown -h now     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Why Non-Blocking Matters

| Scenario | Blocking Notifications | Eneru (Non-Blocking) |
|----------|------------------------|----------------------|
| Network down during outage | ❌ Shutdown delayed by timeout (10-30s per notification) | ✅ Shutdown proceeds immediately |
| Discord rate-limited | ❌ Waits for retry | ✅ Continues without waiting |
| DNS resolution fails | ❌ Hangs until timeout | ✅ Worker handles silently |
| Multiple notification services | ❌ Sequential delays compound | ✅ All queued instantly |
| Power about to fail | ❌ Risk of incomplete shutdown | ✅ Critical operations prioritized |

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
