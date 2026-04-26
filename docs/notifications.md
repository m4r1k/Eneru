# Notifications

Eneru sends notifications through [Apprise](https://github.com/caronc/apprise), so one config format covers Discord, Slack, Telegram, ntfy, Pushover, email, Matrix, Gotify, Home Assistant, and many more services.

Notifications are not on the shutdown critical path. Eneru queues the message and continues shutting things down.

## Basic config

```yaml
notifications:
  title: "Homelab UPS"
  avatar_url: "https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-avatar.png"
  timeout: 10
  urls:
    - "discord://webhook_id/webhook_token"
    - "ntfy://ntfy.sh/my-ups-topic"
```

If `urls` is empty, notifications are disabled.

## Test delivery

Package install:

```bash
sudo eneru test-notifications --config /etc/ups-monitor/config.yaml
```

PyPI install:

```bash
eneru test-notifications --config /etc/ups-monitor/config.yaml
```

Also run validation. It catches missing Apprise support and malformed config values:

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
```

## Common service URLs

### Discord

Create a webhook in Discord under Server Settings, Integrations, Webhooks. Convert the webhook URL:

```text
https://discord.com/api/webhooks/1234567890/abcdefghijklmnopqrstuvwxyz
                                 webhook id / webhook token
```

```yaml
notifications:
  urls:
    - "discord://1234567890/abcdefghijklmnopqrstuvwxyz"
```

### Slack

Create an incoming webhook in a Slack app and use the token components from the webhook URL.

```yaml
notifications:
  urls:
    - "slack://TokenA/TokenB/TokenC/#channel"
```

### Telegram

Create a bot with BotFather, message it once, then get your chat ID with `getUpdates`.

```yaml
notifications:
  urls:
    - "telegram://bot_token/chat_id"
```

### ntfy

```yaml
notifications:
  urls:
    - "ntfy://ntfy.sh/my-topic"
    - "ntfy://user:password@ntfy.example.com/my-topic"
```

### Pushover

```yaml
notifications:
  urls:
    - "pover://user_key@app_token"
```

### Email

```yaml
notifications:
  urls:
    - "mailto://user:password@smtp.example.com:587?to=admin@example.com"
```

Apprise documents the full URL catalog in its [service wiki](https://github.com/caronc/apprise/wiki).

## Persistent retry

Eneru stores pending notifications in the per-UPS SQLite database. If the network is down, the worker retries with exponential backoff. If Eneru restarts before delivery, the next process resumes the queue.

```text
ENERU NOTIFICATION PATH

 +----------------------+      insert pending       +----------------------+
 | Main monitor thread  +-------------------------->| Per-UPS SQLite DB    |
 | power/lifecycle      |                           | notifications table  |
 | event occurs         |                           | status = pending     |
 +----------+-----------+                           +----------+-----------+
            |                                                  ^
            | continue safety work                             |
            v                                                  | update row
 +----------+-----------+                                      |
 | Shutdown sequence    |                           +----------+-----------+
 | VMs, containers, SSH |                           | Worker thread       |
 | filesystems, local   |                           | oldest due row      |
 | poweroff             |                           | Apprise delivery    |
 +----------------------+                           +----------+-----------+
                                                               |
                                                +--------------+--------------+
                                                |                             |
                                                v                             v
                                      +---------+----------+       +----------+---------+
                                      | success            |       | failure            |
                                      | mark sent          |       | attempts += 1      |
                                      +--------------------+       | schedule backoff   |
                                                                   +--------------------+
```

```yaml
notifications:
  retry_interval: 5
  retry_backoff_max: 300
  max_attempts: 0
  max_age_days: 30
  max_pending: 10000
  retention_days: 7
```

| Key | Default | Meaning |
|-----|---------|---------|
| `retry_interval` | `5` | First retry delay in seconds |
| `retry_backoff_max` | `300` | Maximum delay after exponential backoff |
| `max_attempts` | `0` | Attempt cap. `0` means unlimited until age/backlog policy cancels the row |
| `max_age_days` | `30` | Pending rows older than this are cancelled as `too_old` |
| `max_pending` | `10000` | Pending backlog cap. Oldest rows are cancelled as `backlog_overflow` |
| `retention_days` | `7` | Sent and cancelled row retention |

`max_attempts` defaults to unlimited because Apprise returns one boolean for success or failure. Eneru cannot reliably tell a bad URL from a temporary internet outage.

### Notification architecture history

| Scenario | Fire-and-forget (v4.6) | Memory retry (v4.7+) | SQLite queue (v5.2+) |
|----------|------------------------|----------------------|----------------------|
| Short network blip | Message could be lost | Retried while process stayed alive | Retried until delivered |
| Router reboot during outage | Message could be lost | Retried while process stayed alive | Retried after network returns |
| Daemon restart mid-outage | In-memory message lost | In-memory message lost | Pending row survives restart |
| Host powers off before delivery | Message lost | Message lost | Row remains for next boot if disk survives |
| Multi-day endpoint outage | No queue | Repeated retries in memory | Per-message backoff capped by `retry_backoff_max` |
| Brief power outage with delayed internet | Separate messages or losses | Separate messages if delivered | Pending `ON_BATTERY` + `POWER_RESTORED` can coalesce |
| `systemctl restart eneru` | Stop/start noise | Stop/start noise | Single restart notification when classified |
| Package upgrade | Stop/start noise | Stop/start noise | Single upgraded notification when classified |
| Backlog growth | Not applicable | Process memory growth | Bounded by `max_pending` |
| Stale pending messages | Not applicable | Lost on process exit | Aged out by `max_age_days` |

The v5.2 queue is intentionally boring: SQLite row first, network later. Shutdown code should not block on Discord, email, DNS, or a phone push service during a power event.

### Retry timeline

The retry timer is per message. A failed send does not block newer shutdown work, and it does not spin in a tight loop while the network is down. The first attempt is due immediately because a newly inserted row has no backoff entry. After a failure, `retry_interval * 2^(attempts-1)` is used, capped by `retry_backoff_max`.

| Time | Worker action | Row state |
|------|---------------|-----------|
| 0s | Event inserts notification | `pending`, `attempts=0`; worker is woken |
| Immediately or next 1s worker tick | First delivery attempt fails | `attempts=1`, next attempt in 5s by default |
| About 5s later | Second attempt fails | `attempts=2`, next attempt in 10s |
| About 10s later | Third attempt fails | `attempts=3`, next attempt in 20s |
| About 20s later | Fourth attempt fails | Backoff continues, capped by `retry_backoff_max` |
| Network returns | Next due attempt succeeds | Row becomes `sent` |
| `max_age_days` reached and prune runs | Message is too old | Row becomes `cancelled`, reason `too_old` |

## Coalescing

Eneru folds related pending events before delivery when doing so gives the operator a clearer message.

| Pattern | Result |
|---------|--------|
| Power lost and restored while both messages are still pending | One brief-outage summary |
| Shutdown summary pending when the daemon starts after power recovery | One recovery notification with downtime and reason |
| Stop message pending and a new daemon starts quickly | One restarted, upgraded, or recovered notification |

Already-delivered messages are not rewritten.

### Brief outage coalescing timeline

This is the common "power came back fast, internet came back slowly" case. Coalescing only happens if both rows are still `pending` when the worker sees them; already-sent rows are left alone.

| Time | Event | Notification result |
|------|-------|---------------------|
| 0s | UPS goes on battery | `ON_BATTERY` row is inserted as pending |
| 5s | Worker tries delivery | Network is still down, so the row remains pending |
| 28s | Power returns | `POWER_RESTORED` row is inserted as pending |
| Later worker pass | Network or endpoint is usable again | Worker sees both rows still pending |
| Same pass | Coalescer runs before delivery | Original rows are cancelled as `coalesced` and a summary row is inserted |
| Same or next pass | Summary row is due | One brief-outage notification is delivered |

## Lifecycle notifications

Eneru classifies daemon starts instead of blindly sending "stopped" and "started" every time.

| Event | How Eneru recognizes it |
|-------|-------------------------|
| Package upgrade | Package marker from install scripts or a stored version change |
| Restart | Previous shutdown marker with short downtime |
| Recovery after UPS-triggered shutdown | Shutdown-state marker with sequence-complete reason |
| Crash recovery | Missing or fatal shutdown marker |
| First start | No previous lifecycle metadata |

The goal is one useful lifecycle message per transition.

### Restart classification timeline

This is the systemd restart path. The deferred timer delay is `DEFAULT_DEFER_SECS = 15` in `src/eneru/deferred_delivery.py`; plain `systemctl stop` and non-systemd foreground runs send the stop notification eagerly instead.

| Time | Event | Notification behavior |
|------|-------|-----------------------|
| 0s | Old daemon exits under systemd restart or unknown intent | Stop row is queued and a 15s `systemd-run` delivery timer is scheduled |
| Before 15s | Replacement daemon starts | Startup classifier reads the previous shutdown marker |
| Before 15s | Classifier recognizes restart, upgrade, or recovery | Pending stop row is cancelled as `superseded` |
| Before 15s | Replacement notification is queued | A single restarted, upgraded, or recovered message is sent |
| 15s | No replacement cancelled the original row | Deferred delivery timer fires and the stopped message is sent |

## Alert noise tuning

### Voltage hysteresis

```yaml
notifications:
  voltage_hysteresis_seconds: 30
```

Voltage events are logged immediately. The notification waits until the condition has persisted for the configured number of seconds. If voltage returns to normal before the dwell expires, Eneru records `VOLTAGE_FLAP_SUPPRESSED` and sends no alert.

Severe deviations beyond +/- 15% of nominal bypass the dwell and notify immediately.

### Voltage hysteresis timeline

The default dwell is 30 seconds. The log and `events` table are immediate; only notification delivery waits.

| Time | Voltage state | Notification behavior |
|------|---------------|-----------------------|
| 0s | Input crosses warning threshold | Log and SQLite event are written immediately |
| 10s | Voltage returns to normal | No notification is sent; `VOLTAGE_FLAP_SUPPRESSED` is recorded |
| 0s | Input crosses warning threshold again | New dwell window starts |
| 30s | Still outside threshold | Notification is sent with a persisted-duration note |
| Any time | Severe deviation exceeds +/- 15% nominal | Notification bypasses dwell and sends immediately |

### Suppression list

```yaml
notifications:
  suppress:
    - AVR_BOOST_ACTIVE
    - AVR_TRIM_ACTIVE
    - AVR_INACTIVE
```

Suppression mutes notifications only. The log and SQLite `events` table still record the event with `notification_sent=0`.

Allowed event names:

| Event |
|-------|
| `POWER_RESTORED` |
| `VOLTAGE_NORMALIZED` |
| `AVR_BOOST_ACTIVE` |
| `AVR_TRIM_ACTIVE` |
| `AVR_INACTIVE` |
| `BYPASS_MODE_INACTIVE` |
| `OVERLOAD_RESOLVED` |
| `CONNECTION_RESTORED` |
| `VOLTAGE_AUTODETECT_MISMATCH` |
| `VOLTAGE_FLAP_SUPPRESSED` |

Safety-critical events cannot be suppressed. Validation rejects them:

| Blocked event |
|---------------|
| `OVER_VOLTAGE_DETECTED` |
| `BROWNOUT_DETECTED` |
| `OVERLOAD_ACTIVE` |
| `BYPASS_MODE_ACTIVE` |
| `ON_BATTERY` |
| `CONNECTION_LOST` |
| Any event starting with `SHUTDOWN_` |

## Inspect notification history

Each UPS database contains notification rows. The exact file name is the sanitized UPS label.

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  "SELECT ts, status, attempts, cancel_reason, title FROM notifications ORDER BY ts DESC LIMIT 20;"
```

Muted events are in the `events` table:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  "SELECT event_type, COUNT(*) FROM events WHERE notification_sent = 0 GROUP BY event_type;"
```

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Test command says Apprise is missing | Install the native package with notification dependencies, or use `eneru[notifications]` for PyPI |
| URL rejected | Compare the URL against the Apprise wiki for that service |
| Test sends but production events do not arrive | Check `notifications.suppress`, service rate limits, and outbound HTTPS/DNS |
| Messages arrive late | Check network recovery during outages and pending rows in SQLite |
| Duplicate lifecycle messages | Check whether multiple Eneru instances are running against the same config |
