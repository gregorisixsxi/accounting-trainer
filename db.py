"""SQLite storage: users, sessions and per-user history of solved (generated) tasks."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# On Render set DB_PATH to a persistent disk path (e.g. /var/data/app.db),
# otherwise the database file is wiped on every deploy.
DB_PATH = os.environ.get("DB_PATH") or str(BASE_DIR / "data.db")

HISTORY_LIMIT = 300  # how many recent tasks to keep per user

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))          # session token lifetime
LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))     # failed logins allowed per login name ...
LOGIN_WINDOW = int(os.environ.get("LOGIN_WINDOW_SECONDS", "900")) # ... within this window (15 min)
PRO_DAILY_LIMIT = int(os.environ.get("PRO_DAILY_LIMIT", "60"))    # AI generations per PRO user per UTC day

# Usernames that automatically get PRO: PRO_USERS="alice,bob@gmail.com"
PRO_USERS = {u.strip().lower() for u in os.environ.get("PRO_USERS", "").split(",") if u.strip()}


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   id            INTEGER PRIMARY KEY AUTOINCREMENT,
                   username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                   password_hash TEXT NOT NULL,
                   is_pro        INTEGER NOT NULL DEFAULT 0,
                   solved_tasks  TEXT NOT NULL DEFAULT '[]',
                   created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        _migrate(c)
        c.execute(
            """CREATE TABLE IF NOT EXISTS login_attempts (
                   login_key TEXT NOT NULL,
                   ts        INTEGER NOT NULL
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(login_key, ts)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS pro_usage (
                   user_id INTEGER NOT NULL,
                   day     TEXT NOT NULL,
                   count   INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (user_id, day)
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   token_hash TEXT PRIMARY KEY,
                   user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS",
# so the existing columns are checked first and only the missing ones are added.
USER_MIGRATIONS = (
    ("is_pro", "INTEGER NOT NULL DEFAULT 0"),
    ("pro_expires_at", "TEXT DEFAULT NULL"),         # UTC ISO-8601; NULL = no expiry
    ("lemon_customer_id", "TEXT DEFAULT NULL"),
    ("lemon_subscription_id", "TEXT DEFAULT NULL"),
)


def _migrate(c: sqlite3.Connection) -> None:
    existing = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    for name, ddl in USER_MIGRATIONS:
        if name not in existing:
            c.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_lemon_sub ON users(lemon_subscription_id)")


# ---------- time / PRO status ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    """Parses ISO-8601 (also Lemon Squeezy's '...Z' form) into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def is_pro_active(username: str, is_pro, pro_expires_at: str | None) -> bool:
    """PRO if the user is on the admin list, or has a paid flag that has not expired yet (UTC)."""
    if username.lower() in PRO_USERS:
        return True
    if not is_pro:
        return False
    expires = parse_dt(pro_expires_at)
    return expires is None or expires > now_utc()


# ---------- passwords ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


# ---------- users ----------
def _public(row: sqlite3.Row) -> dict:
    active = is_pro_active(row["username"], row["is_pro"], row["pro_expires_at"])
    paid = bool(row["is_pro"]) and active and row["username"].lower() not in PRO_USERS
    return {
        "id": row["id"],
        "username": row["username"],
        "is_pro": active,
        "pro_expires_at": row["pro_expires_at"] if paid else None,
    }


def create_user(username: str, password: str) -> dict | None:
    """Returns the new user, or None if the username is taken."""
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, hash_password(password)),
            )
        except sqlite3.IntegrityError:
            return None
        row = c.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _public(row)


def check_credentials(username: str, password: str) -> tuple[str, dict | None]:
    """('ok', user) | ('no_user', None) | ('bad_password', None)"""
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        return "no_user", None
    if not verify_password(password, row["password_hash"]):
        return "bad_password", None
    return "ok", _public(row)


# ---------- sessions ----------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as c:
        c.execute("DELETE FROM sessions WHERE created_at <= datetime('now', ?)", (f"-{SESSION_DAYS} days",))  # purge expired
        c.execute("INSERT INTO sessions (token_hash, user_id) VALUES (?, ?)", (_hash_token(token), user_id))
    return token


def user_by_token(token: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.created_at > datetime('now', ?)",   # tokens expire after SESSION_DAYS
            (_hash_token(token), f"-{SESSION_DAYS} days"),
        ).fetchone()
    return _public(row) if row else None


def delete_session(token: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


# ---------- solved-task history ----------
def get_history(user_id: int, topic: str | None = None) -> list[dict]:
    with connect() as c:
        row = c.execute("SELECT solved_tasks FROM users WHERE id = ?", (user_id,)).fetchone()
    items = json.loads(row["solved_tasks"]) if row else []
    return [i for i in items if topic is None or i.get("topic") == topic]


def add_to_history(user_id: int, entry: dict) -> None:
    """Atomic read-modify-write so two parallel requests cannot lose an entry."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT solved_tasks FROM users WHERE id = ?", (user_id,)).fetchone()
        items = json.loads(row["solved_tasks"]) if row else []
        items.append(entry)
        c.execute(
            "UPDATE users SET solved_tasks = ? WHERE id = ?",
            (json.dumps(items[-HISTORY_LIMIT:], ensure_ascii=False), user_id),
        )


def clear_history(user_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE users SET solved_tasks = '[]' WHERE id = ?", (user_id,))


# ---------- subscriptions (Lemon Squeezy webhooks) ----------
def find_user_id(user_id: str | int | None = None, subscription_id: str | None = None, email: str | None = None) -> int | None:
    """Locates the paying user: checkout custom data first, then the stored subscription id, then the e-mail."""
    with connect() as c:
        if user_id not in (None, ""):
            try:
                row = c.execute("SELECT id FROM users WHERE id = ?", (int(user_id),)).fetchone()
            except (TypeError, ValueError):
                row = None
            if row:
                return row["id"]
        if subscription_id:
            row = c.execute("SELECT id FROM users WHERE lemon_subscription_id = ?", (str(subscription_id),)).fetchone()
            if row:
                return row["id"]
        if email:
            row = c.execute("SELECT id FROM users WHERE username = ?", (email,)).fetchone()
            if row:
                return row["id"]
    return None


def set_subscription(user_id: int, is_pro: bool, expires_at: datetime | None,
                     customer_id: str | None = None, subscription_id: str | None = None) -> None:
    with connect() as c:
        c.execute(
            """UPDATE users SET is_pro = ?, pro_expires_at = ?,
                      lemon_customer_id = COALESCE(?, lemon_customer_id),
                      lemon_subscription_id = COALESCE(?, lemon_subscription_id)
               WHERE id = ?""",
            (
                1 if is_pro else 0,
                iso(expires_at) if expires_at else None,
                str(customer_id) if customer_id else None,
                str(subscription_id) if subscription_id else None,
                user_id,
            ),
        )


def get_user(user_id: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row) if row else None


# ---------- failed-login throttle (per login name, stored in the DB so it survives restarts) ----------
def login_lock_seconds(login_key: str) -> int:
    """0 if the login may try again, otherwise how many seconds until the oldest counted failure expires."""
    now = int(time.time())
    with connect() as c:
        rows = c.execute(
            "SELECT ts FROM login_attempts WHERE login_key = ? AND ts > ? ORDER BY ts",
            (login_key, now - LOGIN_WINDOW),
        ).fetchall()
    if len(rows) < LOGIN_MAX_FAILS:
        return 0
    return max(1, rows[-LOGIN_MAX_FAILS]["ts"] + LOGIN_WINDOW - now)


def record_login_failure(login_key: str) -> int:
    """Stores a failure and returns how many attempts are left in the current window."""
    now = int(time.time())
    with connect() as c:
        c.execute("DELETE FROM login_attempts WHERE ts <= ?", (now - LOGIN_WINDOW,))
        c.execute("INSERT INTO login_attempts (login_key, ts) VALUES (?, ?)", (login_key, now))
        n = c.execute("SELECT COUNT(*) AS n FROM login_attempts WHERE login_key = ?", (login_key,)).fetchone()["n"]
    return max(0, LOGIN_MAX_FAILS - n)


def clear_login_failures(login_key: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM login_attempts WHERE login_key = ?", (login_key,))


# ---------- daily PRO generation quota (protects the Anthropic budget) ----------
def _today() -> str:
    return now_utc().strftime("%Y-%m-%d")


def consume_pro_quota(user_id: int, limit: int = PRO_DAILY_LIMIT) -> tuple[bool, int]:
    """Atomically takes one generation from today's quota. Returns (allowed, used_after)."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO pro_usage (user_id, day, count) VALUES (?, ?, 0)", (user_id, _today()))
        used = c.execute("SELECT count FROM pro_usage WHERE user_id = ? AND day = ?", (user_id, _today())).fetchone()["count"]
        if used >= limit:
            return False, used
        c.execute("UPDATE pro_usage SET count = count + 1 WHERE user_id = ? AND day = ?", (user_id, _today()))
        return True, used + 1


def refund_pro_quota(user_id: int) -> None:
    """Gives the unit back when a generation failed before producing a task."""
    with connect() as c:
        c.execute("UPDATE pro_usage SET count = MAX(count - 1, 0) WHERE user_id = ? AND day = ?", (user_id, _today()))
        c.execute("DELETE FROM pro_usage WHERE day < date('now', '-7 days')")
