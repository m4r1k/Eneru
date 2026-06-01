"""Local user + API-key authentication store (v6.0).

Eneru's auth is deliberately small: a single global SQLite database (default
``/var/lib/eneru/auth.db``) holding local user accounts and API keys, managed by
the ``eneru user`` / ``eneru apikey`` CLI and read by the embedded API server.

Think of it like ``/etc/passwd`` + ``/etc/shadow`` for the API: the username is
public, the password is only ever stored as a one-way salted hash (bcrypt), and
API keys are random tokens stored as a SHA-256 digest so a leaked database never
yields a usable credential.

Design choices:

* **bcrypt is a lazy import.** It is an optional ``[auth]`` extra so the core
  daemon keeps its PyYAML-only footprint. Any path that needs hashing calls
  :func:`require_bcrypt`, which raises an actionable error when the package is
  missing rather than failing with a bare ``ImportError`` deep in a handler.
* **Short-lived connections.** The CLI (one process) and the daemon's API thread
  (another process) both touch this DB. Each operation opens its own connection
  with WAL enabled, so cross-process access is safe and there is no shared
  ``sqlite3.Connection`` to guard with a lock.
* **A ``role`` column exists now** (default ``admin``) purely so v7.0 RBAC is a
  data-fill, not a schema migration. In v6.0 every authenticated principal is an
  admin; non-admin roles are rejected on write so nobody is misled into thinking
  ``viewer`` is enforced yet.
"""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional


SCHEMA_VERSION = 1

# v6.0 stores the column but enforces a single effective role. v7.0 widens this
# set and adds capability checks; until then a stored ``viewer`` would be a lie.
VALID_ROLES = ("admin",)
DEFAULT_ROLE = "admin"

# bcrypt hashes at most the first 72 bytes of a password. We truncate explicitly
# (classic-bcrypt behaviour) so long passphrases hash deterministically instead
# of raising ValueError on bcrypt >= 4. Documented as a caveat to users.
_BCRYPT_MAX_BYTES = 72

API_KEY_PREFIX = "eneru_"


class AuthError(Exception):
    """Base class for auth-store errors surfaced to the CLI/API."""


class UserExistsError(AuthError):
    """Raised when creating a user that already exists."""


class UserNotFoundError(AuthError):
    """Raised when operating on a user that does not exist."""


def require_bcrypt():
    """Import and return the ``bcrypt`` module, or raise an actionable error.

    Kept in one place so every caller emits the same install hint instead of a
    bare ImportError leaking out of a request handler or CLI command.
    """
    try:
        import bcrypt  # noqa: PLC0415 - intentional lazy import
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise AuthError(
            "Authentication needs the 'bcrypt' package, which is not installed.\n"
            "  pip:    pip install 'eneru[auth]'\n"
            "  Debian/Ubuntu: apt install python3-bcrypt\n"
            "  Fedora/RHEL:   dnf install python3-bcrypt"
        ) from exc
    return bcrypt


def _prepare_password(password: str) -> bytes:
    """Encode + truncate a password to bcrypt's 72-byte input limit."""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt hash (self-describing ``$2b$…`` string) for ``password``."""
    bcrypt = require_bcrypt()
    # N3: reject an embedded NUL. Shipped bcrypt 5.x hashes the whole 72-byte
    # input, but some older builds truncated at the first NUL -- which would let
    # "pw\x00anything" verify against a hash of "pw". Reject at creation so the
    # store's behavior can't silently regress on an older distro bcrypt.
    if "\x00" in password:
        raise AuthError("password must not contain NUL bytes")
    return bcrypt.hashpw(_prepare_password(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check of ``password`` against a stored bcrypt ``hashed``."""
    bcrypt = require_bcrypt()
    try:
        return bcrypt.checkpw(_prepare_password(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed/empty stored hash -> deny, never raise into the caller.
        return False


def generate_password(length_bytes: int = 18) -> str:
    """Return a strong random password suitable for ``--generate``."""
    return secrets.token_urlsafe(length_bytes)


def generate_api_key() -> str:
    """Return a fresh plaintext API key (``eneru_<urlsafe>``)."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """Return the SHA-256 hex digest stored for an API key.

    API keys are long random tokens, not human secrets — a fast digest is the
    right tool (bcrypt's 72-byte cap and cost would be wrong here), and the
    stored digest is useless to an attacker who reads the DB.

    CodeQL flags this as weak password hashing; it is a false positive — the
    input is a 256-bit random token, not a user password, so a slow KDF is
    neither needed nor appropriate.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()  # lgtm[py/weak-sensitive-data-hashing]


def _validate_role(role: str) -> str:
    role = (role or DEFAULT_ROLE).strip()
    if role not in VALID_ROLES:
        allowed = ", ".join(VALID_ROLES)
        raise AuthError(
            f"role {role!r} is not supported in v6.0 (allowed: {allowed}). "
            "Operator/viewer roles arrive with RBAC in v7.0."
        )
    return role


class AuthStore:
    """SQLite-backed store for local users and API keys.

    Construction is cheap and does no I/O; the schema is ensured lazily on the
    first operation. All methods open their own short-lived connection.
    """

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        # Ensure the schema at most once per instance. Without this latch every
        # operation — including read-only/failed auth checks on the API hot path
        # — would re-run the schema DDL and write to the DB.
        self._schema_ready = False

    # ----- connection / schema -----

    @contextmanager
    def _session(self):
        """Yield a short-lived connection; commit on success, always close.

        ``with conn:`` only commits/rolls back the transaction — it does NOT
        close the connection (that leaks fds and trips ResourceWarning). So we
        nest it inside a try/finally that closes.
        """
        # Defensive mkdir: pip installs don't run nfpm's directory entry, so the
        # store must be willing to create its parent dir itself (mirrors stats).
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 2000")
        if not self._schema_ready:
            self._ensure_schema(conn)
            self._schema_ready = True
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        # Always re-assert owner-only permissions, even when the schema is
        # already current — a pre-existing world-readable auth.db (e.g. created
        # by an older version, or with a loose umask) must be tightened too, not
        # only on the migration path. Cheap (a couple of chmods, once per
        # instance via the _schema_ready latch in _session).
        self._restrict_permissions()
        # Gate on PRAGMA user_version so an already-initialized DB is a pure read
        # (no write) — important because the API auth path reads this store on
        # every request. Only a brand-new (or older-schema) DB takes the write.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin',
                    created_at INTEGER NOT NULL,
                    password_changed_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin',
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER
                );
                """
            )
            # PRAGMA can't be parameterized; SCHEMA_VERSION is our own int.
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    def _restrict_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    # ----- users -----

    def create_user(self, username: str, password: str,
                    role: str = DEFAULT_ROLE) -> None:
        """Create a user. Raises :class:`UserExistsError` if it already exists."""
        username = _validate_username(username)
        role = _validate_role(role)
        now = int(time.time())
        pw_hash = hash_password(password)
        with self._session() as conn:
            try:
                conn.execute(
                    "INSERT INTO users(username, password_hash, role, "
                    "created_at, password_changed_at) VALUES (?, ?, ?, ?, ?)",
                    (username, pw_hash, role, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise UserExistsError(f"user {username!r} already exists") from exc

    def set_password(self, username: str, password: str) -> None:
        """Reset a user's password. Raises :class:`UserNotFoundError` if absent."""
        pw_hash = hash_password(password)
        with self._session() as conn:
            # Like moving a one-way turnstile: compute the next marker inside
            # the UPDATE so concurrent password resets cannot both reuse the
            # same password_changed_at value and leave an old session valid.
            cur = conn.execute(
                "UPDATE users SET password_hash = ?, "
                "password_changed_at = max(?, password_changed_at + 1) "
                "WHERE username = ?",
                (pw_hash, int(time.time()), username),
            )
            if cur.rowcount == 0:
                raise UserNotFoundError(f"user {username!r} not found")

    def delete_user(self, username: str) -> None:
        """Delete a user. Raises :class:`UserNotFoundError` if absent."""
        with self._session() as conn:
            cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            if cur.rowcount == 0:
                raise UserNotFoundError(f"user {username!r} not found")

    def get_user(self, username: str) -> Optional[Dict]:
        """Return public user metadata (no hash), or None if absent."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT username, role, created_at, password_changed_at "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_users(self) -> List[Dict]:
        """Return public metadata for all users, ordered by username."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT username, role, created_at, password_changed_at "
                "FROM users ORDER BY username"
            ).fetchall()
        return [dict(r) for r in rows]

    def user_count(self) -> int:
        with self._session() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def authenticate(self, username: str, password: str) -> Optional[Dict]:
        """Return user metadata on a correct password, else None.

        Always runs a bcrypt comparison even for an unknown user (against a dummy
        hash) so response time doesn't reveal whether the username exists.
        """
        with self._session() as conn:
            row = conn.execute(
                "SELECT username, password_hash, role, password_changed_at "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            verify_password(password, _DUMMY_BCRYPT_HASH)
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return {
            "username": row["username"],
            "role": row["role"],
            "password_changed_at": row["password_changed_at"],
            "kind": "user",
        }

    # ----- api keys -----

    def create_api_key(self, label: str, role: str = DEFAULT_ROLE):
        """Create an API key. Returns ``(id, plaintext_key)``; key shown once."""
        role = _validate_role(role)
        label = (label or "").strip()
        if not label:
            raise AuthError("API key label must not be empty")
        # Reject control characters: the label is echoed into the audit log
        # (as the principal), so a newline/CR could forge log lines.
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in label):
            raise AuthError("API key label must not contain control characters")
        now = int(time.time())
        key = generate_api_key()
        with self._session() as conn:
            cur = conn.execute(
                "INSERT INTO api_keys(key_hash, label, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (hash_api_key(key), label, role, now),
            )
            return int(cur.lastrowid), key

    def list_api_keys(self) -> List[Dict]:
        """Return metadata for all API keys (never the key or its hash)."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT id, label, role, created_at, last_used_at "
                "FROM api_keys ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def revoke_api_key(self, key_id: int) -> None:
        """Delete an API key by id. Raises :class:`AuthError` if absent."""
        with self._session() as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            if cur.rowcount == 0:
                raise AuthError(f"API key id {key_id} not found")

    def authenticate_api_key(self, key: str) -> Optional[Dict]:
        """Return key metadata for a valid key (and stamp last_used), else None."""
        if not key:
            return None
        digest = hash_api_key(key)
        now = int(time.time())
        with self._session() as conn:
            row = conn.execute(
                "SELECT id, label, role FROM api_keys WHERE key_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        return {"id": row["id"], "label": row["label"], "role": row["role"],
                "kind": "api_key"}


def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not username:
        raise AuthError("username must not be empty")
    if len(username) > 64:
        raise AuthError("username must be 64 characters or fewer")
    # ASCII letters/digits only (plus . _ - @): keeps names filesystem/URL/log
    # friendly and avoids visually-confusable Unicode look-alike accounts.
    if not all(c.isascii() and (c.isalnum() or c in "._-@") for c in username):
        raise AuthError(
            "username may contain only ASCII letters, digits, and . _ - @"
        )
    return username


# A fixed VALID bcrypt hash of a random string, used only to spend ~equal time
# on the unknown-user path (a malformed hash would short-circuit and defeat the
# timing defense). Never matches any real password.
_DUMMY_BCRYPT_HASH = (
    "$2b$12$RB/LTj0og5aQT9KR1uZrQeLZzG9ATAEr7qSqSp7tAzYzUcKg3k8IW"
)
