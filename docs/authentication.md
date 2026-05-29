# Authentication

Eneru's API can require credentials before serving control actions and the
dashboard. Authentication is **opt-in**: until you enable it, the API behaves
exactly as it did in v5.3 — read-only, no credentials — and every write surface
is hard disabled.

This page covers the **auth foundation**: the local user/API-key store and the
CLI that manages it. The request-side enforcement (login, bearer tokens, the
endpoint matrix) is documented in [Observability and API](observability-api.md).

## Model at a glance

Think of it like `/etc/passwd` + `/etc/shadow` for the API:

- **Usernames are public; passwords never are.** Passwords are stored only as a
  one-way salted **bcrypt** hash. Eneru cannot show you a password back — it can
  only verify one.
- **API keys are random tokens** (`eneru_…`) stored as a SHA-256 digest. The
  plaintext is shown once at creation and is unrecoverable afterward.
- **No default user, no default password.** You create the first account
  yourself. (A shipped default credential is the classic appliance CVE.)
- **One global store**, a dedicated SQLite database at
  `/var/lib/eneru/auth.db` by default — separate from the per-UPS statistics
  databases. Override with `api.auth.db_path` or the CLI `--auth-db` flag.
- **Roles exist but only `admin` is enforced in v6.0.** A `role` column is stored
  now so v7.0 can add `operator`/`viewer` RBAC without a migration; until then,
  every authenticated principal is an admin and non-admin roles are rejected.

## Requirements

The store needs the `bcrypt` package. The deb package and the container image
include it. For pip installs, request the extra:

```bash
pip install 'eneru[auth]'
```

On RPM-based distros it is a soft dependency (`dnf install python3-bcrypt`,
typically from EPEL). If it is missing, auth commands fail with an actionable
hint rather than a stack trace. bcrypt hashes at most the first **72 bytes** of a
password; Eneru truncates to that bound deterministically, so longer passphrases
work but only their first 72 bytes are significant.

## Managing users

> The examples below use the pip/developer command `eneru …`. On a **package
> (deb/rpm) install**, invoke the wrapper instead:
> `sudo python3 /opt/ups-monitor/eneru.py user create alice …`.

```bash
# Create (interactive prompt by default — asks twice and confirms a match)
eneru user create alice

# Create with a generated password (printed once)
eneru user create alice --generate

# Create non-interactively (automation): password read from stdin
printf '%s' "$PASSWORD" | eneru user create alice --password-stdin

# List, inspect (never prints the hash)
eneru user list
eneru user show alice

# Reset a password (same input options as create)
eneru user passwd alice --generate

# Delete
eneru user delete alice
```

There is deliberately **no `--password VALUE` flag** — a password on the command
line leaks into shell history and the process list (`ps`). Use the interactive
prompt, `--generate`, or `--password-stdin`.

## Managing API keys

API keys are for programmatic clients (Grafana, scripts, CI) that send
`Authorization: Bearer <key>`.

```bash
# Create — the key is printed once; store it now
eneru apikey create --label "Grafana read-only"

# List (metadata only, never the key or its hash)
eneru apikey list

# Revoke by id (from the list)
eneru apikey revoke 3
```

## Configuration

```yaml
api:
  enabled: true
  auth:
    enabled: true            # opt-in; off => read-only, writes hard-disabled
    require_for_reads: false # reads stay open (Prometheus keeps scraping); writes always need a credential
    session_ttl: 3600        # dashboard session lifetime, seconds
    db_path: "/var/lib/eneru/auth.db"
```

| Key | Default | Description |
|-----|---------|-------------|
| `api.auth.enabled` | `false` | Turn API authentication on. Off keeps v5.3 read-only behavior |
| `api.auth.require_for_reads` | `false` | When off, read endpoints stay open even with auth on; writes always require a credential |
| `api.auth.session_ttl` | `3600` | Dashboard session token lifetime, in seconds |
| `api.auth.db_path` | `/var/lib/eneru/auth.db` | Location of the user/API-key store; CLI `--auth-db` overrides |

### Auto-enable (create a user, then just sign in — no restart)

If the API is on but you **never set `api.auth.enabled`**, Eneru enforces auth
automatically as soon as the auth DB has at least one user. This removes a common
footgun: creating a user with `eneru user create` and then finding the dashboard
rejects the login because auth was silently off.

The decision is **dynamic**, re-evaluated per request (behind a few-second
cache), so it takes effect **with no restart and no config edit**:

```bash
docker exec <container> eneru user create admin --generate
# within a few seconds the dashboard shows Sign-in and login works — no restart
```

The rule is deliberately conservative:

- An explicit `api.auth.enabled` (either `true` or `false`) **always wins** — set
  `enabled: false` to keep the API open even with users present.
- A fresh install with no users (and no explicit `enabled: true`) stays read-only;
  the check never creates the auth DB as a side effect.
- It does **not** satisfy the `nut_control` fail-closed gate: UPS control still
  requires an explicit `api.auth.enabled: true`.

When the API is enabled but auth is off (no users, or `enabled: false`), the
daemon logs a one-line notice at startup and the dashboard hides its **Sign-in**
button — there is nothing to sign into. The dashboard learns the live auth state
from `/api/v1/config`; if you create the first user while the dashboard is open,
reload the page to pick up the Sign-in button.

## Containers

In a container the store lives under the persisted `/var/lib/eneru` volume, so
users and keys survive restarts. Run the management commands inside the
container, for example:

```bash
docker exec -it eneru eneru user create admin --generate
```
