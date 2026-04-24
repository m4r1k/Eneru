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

  # Initial retry wait for failed sends. Doubles on each failure up to
  # retry_backoff_max (see below). Default: 5
  retry_interval: 5

  # v5.2 persistent-queue knobs. Defaults are sized for "long weekend
  # with the internet down" — the queue survives process death and
  # prolonged outages, then drains once the endpoint returns.
  retention_days: 7         # keep sent/cancelled rows for forensics
  max_attempts: 0           # 0 = unlimited (default, see below)
  max_age_days: 30          # only TTL on pending rows
  max_pending: 10000        # backlog overflow cap
  retry_backoff_max: 300    # 5-min ceiling on the exponential backoff

  # Notification service URLs
  urls:
    - "discord://webhook_id/webhook_token"
    - "slack://token_a/token_b/token_c/#channel"
    - "telegram://bot_token/chat_id"
```

`max_attempts` defaults to `0` (unlimited) on purpose: Apprise's
success/fail signal is a single bool — Eneru cannot tell "bad URL" from
"internet down". Capping attempts risks dropping legitimate messages
during a long outage. Use this only as a poison-message kill switch.

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

Network connectivity is often temporarily down during power outages, and the daemon process itself may stop and restart mid-incident (`systemctl restart`, package upgrade, host reboot). Eneru's notification queue is **SQLite-backed and lossless across process death**: the main thread inserts a `pending` row in the per-UPS stats DB and returns immediately; a worker thread reads pending rows and ships them via Apprise; failed sends stay `pending` and retry with **per-message exponential backoff** (starts at `retry_interval`, doubles up to `retry_backoff_max`).

If the daemon dies while rows are still pending, the next start picks them up and continues delivery — nothing is lost in process memory. Only `pending` rows are subject to a TTL (`max_age_days`, default 30 days); `sent` and `cancelled` rows are kept for `retention_days` (default 7) for forensic inspection via `sqlite3`.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                  ENERU v5.2 NOTIFICATION ARCHITECTURE                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   MAIN THREAD (Critical Path)         WORKER THREAD (Persistent Retry)      │
│   ═══════════════════════════         ═════════════════════════════════     │
│                                                                             │
│   ┌─────────────────────┐                                                   │
│   │  Event happens      │                                                   │
│   │  (power, lifecycle) │                                                   │
│   └──────────┬──────────┘                                                   │
│              │                                                              │
│              ▼                                                              │
│   ┌─────────────────────┐         ┌──────────────────────────────┐          │
│   │ Insert pending row  │────────▶│   Per-UPS SQLite stats DB    │          │
│   │ (non-blocking)      │         │   notifications table:       │          │
│   └──────────┬──────────┘         │   id │ ts │ body │ status   │          │
│              │                    │   ──┴────┴──────┴──────────  │          │
│              │ continues          │   pending → sent / cancelled │          │
│              │ immediately        └──────────┬───────────────────┘          │
│              ▼                               │                              │
│   ┌─────────────────────┐                    ▼                              │
│   │ Stop VMs            │         ┌──────────────────────────────┐          │
│   └──────────┬──────────┘         │ Read oldest `pending` row    │          │
│              ▼                    │ Try Apprise send             │          │
│   ┌─────────────────────┐         └──────────┬───────────────────┘          │
│   │ Stop Containers     │                    │                              │
│   └──────────┬──────────┘               YES ┌┴┐ NO                          │
│              ▼                              │ │                             │
│   ┌─────────────────────┐         ┌─────────┘ └──────────┐                  │
│   │ Unmount Filesystems │         │ Mark `sent`           │ Increment       │
│   └──────────┬──────────┘         │ Move to next pending  │ attempts        │
│              ▼                    │                       │ Wait `interval` │
│   ┌─────────────────────┐         └───────────────────────┘ × 2^N           │
│   │ Shutdown Remote     │                                  (cap = 300s)     │
│   │ Servers             │                                                   │
│   └──────────┬──────────┘                                                   │
│              ▼                                                              │
│   ┌─────────────────────┐         ┌──────────────────────────────┐          │
│   │ flush(timeout=5)    │         │       KEY GUARANTEES          │         │
│   │ wait for pending=0  │         ├──────────────────────────────┤          │
│   │ OR 5s, whichever    │         │ ✓ Survives process death     │          │
│   │ comes first         │         │ ✓ Survives long outages      │          │
│   └──────────┬──────────┘         │ ✓ FIFO within UPS            │          │
│              ▼                    │ ✓ Per-message backoff        │          │
│   ┌─────────────────────┐         │ ✓ No flush burns the CPU on  │          │
│   │ shutdown -h now     │         │   an unreachable endpoint    │          │
│   └─────────────────────┘         └──────────────────────────────┘          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Comparison with prior architectures

| Scenario | Fire-and-Forget (v4.6) | Persistent Retry (v4.7+) | Stateful, Lossless (v5.2+) |
|----------|------------------------|--------------------------|-----------------------------|
| 30-second network blip | Notifications lost | Retried and delivered | Retried and delivered |
| Router reboot during outage | Notifications lost | Retried and delivered | Retried and delivered |
| Transient DNS failure | Notifications lost | Retried and delivered | Retried and delivered |
| Daemon crashes mid-outage | Messages lost at exit | Messages lost at exit | Pending rows survive in SQLite; next start resumes |
| `systemctl restart` mid-outage | Messages lost at exit | Messages lost at exit | Resumed transparently |
| Multi-day internet outage | All retries hammer the network | Same | Per-message exponential backoff, capped at `retry_backoff_max` (default 5 min) |
| `systemctl restart` (no event) | Stop + Start (2 unrelated messages) | Stop + Start (2 unrelated messages) | Single `🔄 Restarted (downtime: Ns)` (classifier cancels the pending stop) |
| Package upgrade (deb/rpm) | Stop + Start (2 messages, no version) | Stop + Start (2 messages, no version) | Single `📦 Upgraded vX → vY` (postinstall marker + classifier) |
| Power-loss recovery | Stop + Start (2 messages, no context) | Stop + Start (2 messages, no context) | Single `📊 Recovered` (folds the prior shutdown headline + downtime + reason) |
| Brief power blip | 2 messages (`Power Lost` + `Power Restored`) | Same | Coalesced into 1 `📊 Brief Power Outage: Ns on battery` summary |
| Backlog growth on runaway events | Unbounded queue (OOM risk) | Unbounded queue (OOM risk) | Bounded by `max_pending` (default 10000); oldest cancelled with `backlog_overflow` |
| Stale pending TTL | N/A (no queue) | N/A (memory-only) | `max_age_days` (default 30) ages out pending rows as `too_old` |

### Shutdown drain: `flush(timeout=5)`

When the daemon is exiting (signal, sequence-complete, or `systemctl stop`), the worker is given up to 5 seconds to drain any pending rows before the process exits. The call returns as soon as `pending` hits 0, so a fast network adds zero latency. Whatever doesn't drain stays in SQLite — the next start replays it, so the only real failure mode is "endpoint still unreachable when the system finally powers off", which Eneru flags in `journalctl` rather than dropping silently.

### 30-second power blip

During a brief power blip, power may return within seconds, well before any shutdown triggers fire. But the public Internet often stays unreachable for several minutes while local network equipment reboots (router, modem, switches, WiFi APs).

Notifications insert as `pending` immediately and the worker retries with exponential backoff. Once the network is back, queued messages drain in age order. With v5.2, an `ON_BATTERY` + `POWER_RESTORED` pair from the same outage that's still pending when delivery resumes is **coalesced into one `📊 Brief Power Outage: Ns on battery` summary** rather than shipping as two separate messages.

```
Timeline (brief power blip, network slow to recover):
─────────────────────────────────────────────────────────────────
0s     │ Power lost, ON_BATTERY row inserted (pending)
5s     │ Worker retry #1 — network down (attempts=1, wait=5s)
10s    │ Worker retry #2 — still failing  (attempts=2, wait=10s)
20s    │ Worker retry #3 — still failing  (attempts=3, wait=20s)
28s    │ Power restored, POWER_RESTORED row inserted (pending)
40s    │ Worker retry #4 — still failing  (attempts=4, wait=40s)
60s    │ Network is back!
60s    │ Coalescer folds ON_BATTERY + POWER_RESTORED into one row
60s    │ ✓ "📊 Brief Power Outage: 28s on battery" delivered
─────────────────────────────────────────────────────────────────
       1 message delivered (instead of 2) despite a 60-second outage
```

---

## Lifecycle notifications

v5.2 replaces the unconditional `🚀 Started` / `🛑 Stopped` pair with a **stateful classifier** that picks one of eight messages based on two on-disk markers and the previous-version metadata in the stats DB. The result is exactly one notification per lifecycle transition rather than one per process start.

| Classification | Trigger | Driven by |
|----------------|---------|-----------|
| `📦 Eneru Upgraded vX → vY` | deb/rpm package upgrade | `.upgrade_marker.json` written by `packaging/scripts/postinstall.sh`; old version captured by `preinstall.sh` (`rpm -q eneru` / `dpkg-query`) |
| `📦 Eneru Upgraded vX → vY` (pip path) | `pip install --upgrade eneru` followed by restart | `meta.last_seen_version` in the stats DB differs from current `__version__` |
| `📊 Eneru Recovered` | Daemon coming back after a power-loss-triggered shutdown | `.shutdown_state.json` with `reason: sequence_complete`; folds the prior shutdown headline + summary into one richer message |
| `🔄 Eneru Restarted (downtime: Ns)` | `systemctl restart eneru` within 30 seconds | `.shutdown_state.json` with `reason: signal` and downtime under 30 s |
| `🚀 Eneru Restarted` (fatal) | Daemon came back after a crash that wrote a `fatal` marker | `.shutdown_state.json` with `reason: fatal` |
| `🚀 Eneru Started (last seen Nh ago)` | Daemon started after a long graceful gap | `.shutdown_state.json` present with downtime ≥ 30 s |
| `🚀 Eneru Started (after crash)` | Daemon came back without ever writing a marker | No shutdown marker but `meta.last_seen_version` is set |
| `🚀 Eneru vX Started` | First-ever start | No markers, no `last_seen_version` |

The mechanism that delivers "exactly one message" combines **cancel-on-startup** with a **systemd-run deferred-delivery timer**:

1. On `SIGTERM`, the old daemon enqueues `🛑 Service Stopped` as a `pending` row, stops its worker (so it can't deliver eagerly), and schedules a transient `systemd-run` timer that re-invokes `eneru _deliver-stop` ~15 s later. The timer lives **outside** `eneru.service`'s cgroup, so it survives our exit and doesn't gate the systemd restart cycle.
2. If the new daemon comes up first (`systemctl restart`, package upgrade, recovery), its classifier cancels every `pending` lifecycle row with `cancel_reason='superseded'` BEFORE the deferred timer fires. Single message: `🔄 Restarted` / `📦 Upgraded` / `📊 Recovered`.
3. If no replacement comes up (true `systemctl stop`), the timer fires, sees the row still `pending`, and ships it via Apprise. Single message: `🛑 Service Stopped`.

Fallback: if `systemd-run` isn't available (non-systemd containers, sandboxed builds), the old daemon ships the stop synchronously via Apprise instead. This loses the restart-coalescing benefit on those hosts but guarantees the user always sees a notification.

---

## Coalescing

Two related events that reach the queue close together get folded into one richer message before delivery, so the user sees the *story* rather than the individual signals.

**Brief power outage.** When a `POWER_RESTORED` notification is being enqueued and an `ON_BATTERY` notification from the same outage is still `pending` (i.e. the network was down during the outage and only came back after recovery), both rows are cancelled with reason `coalesced` and replaced by a single `📊 Brief Power Outage: Ns on battery` summary. If either row already shipped, no fold-in happens — the user still gets two messages, which is the right behaviour because the first one already left the building.

**Power-loss recovery fold-in.** When the daemon classifies a startup as `📊 Recovered` (sequence_complete shutdown marker on disk), it absorbs the prior instance's pending shutdown *headline* (the `🚨 EMERGENCY SHUTDOWN INITIATED!` line) and the pending shutdown *summary* (`✅ Shutdown Sequence Complete`) into one message that carries the trigger reason and the total downtime. The fold is bounded to the current outage (`shutdown_at - 60 s` floor) so an unrelated older pending shutdown row from a previous outage isn't accidentally cancelled.

---

## Notification events

| Event | `event_type` (stats DB) | Category | Notes |
|-------|-------------------------|----------|-------|
| Power lost | `ON_BATTERY` | `power_event` | Coalescible with `POWER_RESTORED` if both pending |
| Power restored | `POWER_RESTORED` | `power_event` | Suppressible via `notifications.suppress` |
| Brief power outage (coalesced) | `BRIEF_POWER_OUTAGE` | `power_event` | v5.2 — replaces an `ON_BATTERY` + `POWER_RESTORED` pair |
| Emergency shutdown initiated | `EMERGENCY_SHUTDOWN_INITIATED` | `shutdown` | Safety-critical (cannot be suppressed) |
| Shutdown sequence complete | `SHUTDOWN_SEQUENCE_COMPLETE` | `shutdown_summary` | One per shutdown |
| Voltage: brownout / over-voltage | `BROWNOUT_DETECTED` / `OVER_VOLTAGE_DETECTED` | `voltage` | Hysteresis-debounced; severe deviations bypass the dwell |
| Voltage normalized | `VOLTAGE_NORMALIZED` | `voltage` | Suppressible |
| AVR boost / trim / inactive | `AVR_BOOST_ACTIVE` / `AVR_TRIM_ACTIVE` / `AVR_INACTIVE` | `voltage` | All three suppressible |
| Bypass active / inactive | `BYPASS_MODE_ACTIVE` / `BYPASS_MODE_INACTIVE` | `voltage` | Active is safety-critical |
| Overload active / resolved | `OVERLOAD_ACTIVE` / `OVERLOAD_RESOLVED` | `voltage` | Active is safety-critical |
| Connection lost / restored | `CONNECTION_LOST` / `CONNECTION_RESTORED` | `general` | Restored is suppressible |
| Battery anomaly | `BATTERY_ANOMALY_DETECTED` | `general` | Charge dropped > 20% while on line power; sustained-reading filter (3 polls) for APC / CyberPower / UniFi firmware jitter |
| Lifecycle: started / restarted / recovered / upgraded | `DAEMON_START` / `DAEMON_RESTARTED` / `DAEMON_RECOVERED` / `DAEMON_UPGRADED` | `lifecycle` | One per lifecycle transition (see "Lifecycle notifications") |
| Lifecycle: restarted after fatal | `DAEMON_RESTARTED_AFTER_FATAL` | `lifecycle` | |
| Lifecycle: started after crash | `DAEMON_AFTER_CRASH` | `lifecycle` | Marker-less restart with prior `last_seen_version` |
| Lifecycle: stopped | `DAEMON_STOP` | `lifecycle` | Pending row gets superseded if a new daemon comes up within the restart window |

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
with a `Persisted Ns.` annotation so you can see it was held.

**Severity bypass (rc9, 5.1.0).** Non-severe voltage warnings (up
to and including ±15% from nominal) go through the dwell as
described above. **Severe deviations** (greater than ±15% from
nominal) bypass the dwell entirely and notify immediately, with a
`(severe, X.X% below/above nominal)` tag and an
`Approaching UPS battery-switch threshold` callout when NUT
exposes the UPS's transfer points. The reasoning:
- Non-severe events are usually flap from neighbour appliances
  cycling — the 30s filter is the right call. Note that with
  narrow UPS transfer points the warning thresholds may be tighter
  than ±10%, so the "non-severe" band can fire at less than 10%
  deviation; the dwell still applies.
- Severe deviations indicate real grid trouble (utility fault,
  generator instability, site wiring issue) — the operator wants
  to know immediately, not 30s later.

The bypass threshold (15%) is hard-coded — there's deliberately no
config knob for it, same reasoning as the warning thresholds: a
misconfiguration there would mask real over-voltage events.

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
[Triggers → Voltage thresholds](triggers.md#voltage-thresholds-preset-driven-raw-thresholds-not-user-configurable)),
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
