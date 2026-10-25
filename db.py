"""SQLite storage: users, sessions and per-user history of solved (generated) tasks."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

import rating
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# On Render set DB_PATH to a persistent disk path (e.g. /var/data/app.db),
# otherwise the database file is wiped on every deploy.
DB_PATH = os.environ.get("DB_PATH") or str(BASE_DIR / "data.db")

HISTORY_LIMIT = 300  # how many recent tasks to keep per user
ARCHIVE_LIMIT = 300  # solved tasks kept in the archive per user (oldest are dropped)

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
            """CREATE TABLE IF NOT EXISTS archive (
                   id             INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   attempt_id     TEXT NOT NULL,            -- one per task shown in the browser; re-checking updates the row
                   topic          TEXT NOT NULL,            -- balance_sheet | terminology | transactions
                   source         TEXT NOT NULL DEFAULT 'free',   -- free (pool) | pro (AI generated)
                   title          TEXT NOT NULL DEFAULT '',
                   company        TEXT NOT NULL DEFAULT '',
                   condition_text TEXT NOT NULL DEFAULT '', -- the task statement
                   review         TEXT NOT NULL DEFAULT '[]',     -- JSON: per-item answer, expected answer, verdict, note
                   explanation    TEXT NOT NULL DEFAULT '',
                   score          INTEGER NOT NULL DEFAULT 0,
                   total          INTEGER NOT NULL DEFAULT 1,
                   created_at     TEXT NOT NULL,            -- UTC ISO-8601, first check
                   updated_at     TEXT NOT NULL             -- UTC ISO-8601, last check
               )"""
        )
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_archive_attempt ON archive(user_id, attempt_id)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS checked_tasks (
                   jti        TEXT PRIMARY KEY,         -- id of a served task: its first check wins and reveals the answers
                   user_id    INTEGER,                  -- NULL when the first check was anonymous (then the task can never be rated)
                   created_at TEXT NOT NULL
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_archive_user ON archive(user_id, id DESC)")
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
    ("is_verified", "INTEGER NOT NULL DEFAULT 0"),    # e-mail confirmed
    ("verify_token", "TEXT DEFAULT NULL"),            # SHA-256 of the e-mail confirmation token
    ("verify_token_expires", "TEXT DEFAULT NULL"),    # UTC ISO-8601
    ("reset_token", "TEXT DEFAULT NULL"),             # SHA-256 of the password reset token
    ("reset_token_expires", "TEXT DEFAULT NULL"),
    # rating (FACEIT-style levels); see rating.py
    ("level", f"INTEGER NOT NULL DEFAULT {rating.level_for(rating.START_ELO)}"),
    ("elo", f"INTEGER NOT NULL DEFAULT {rating.START_ELO}"),
    ("calibration_left", f"INTEGER NOT NULL DEFAULT {rating.CALIBRATION_TASKS}"),
    ("tasks_total", "INTEGER NOT NULL DEFAULT 0"),      # scored tasks
    ("points_correct", "INTEGER NOT NULL DEFAULT 0"),
    ("points_total", "INTEGER NOT NULL DEFAULT 0"),
    ("elo_day", "TEXT DEFAULT NULL"),                   # UTC day of the daily gain counter
    ("elo_day_gain", "INTEGER NOT NULL DEFAULT 0"),
    # profile
    ("nickname", "TEXT DEFAULT NULL"),
    ("avatar", "TEXT DEFAULT NULL"),                    # preset id, e.g. "g3"
)
VERIFY_TTL_SECONDS = 24 * 3600
RESET_TTL_SECONDS = 3600


def _migrate(c: sqlite3.Connection) -> None:
    existing = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    first_verification_migration = "is_verified" not in existing
    for name, ddl in USER_MIGRATIONS:
        if name not in existing:
            c.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
    if first_verification_migration:
        # accounts that exist before e-mail verification was introduced stay usable
        c.execute("UPDATE users SET is_verified = 1")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_lemon_sub ON users(lemon_subscription_id)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_nickname ON users(nickname COLLATE NOCASE)")


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
        "is_verified": bool(row["is_verified"]),
        "nickname": row["nickname"],
        "avatar": row["avatar"],
        "level": row["level"],
        "elo": row["elo"],
        "calibration_left": row["calibration_left"],
        "tasks_total": row["tasks_total"],
        "points_correct": row["points_correct"],
        "points_total": row["points_total"],
    }


def create_user(username: str, password: str, verified: bool = False) -> dict | None:
    """Returns the new user, or None if the username is taken."""
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO users (username, password_hash, is_verified) VALUES (?, ?, ?)",
                (username, hash_password(password), 1 if verified else 0),
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


# ---------- e-mail verification and password reset ----------
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_user_by_email(email: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (email,)).fetchone()
    return _public(row) if row else None


def _set_token(user_id: int, kind: str, token: str, ttl_seconds: int) -> None:
    from datetime import timedelta
    expires = iso(now_utc() + timedelta(seconds=ttl_seconds))
    column, expires_column = (f"{kind}_token", f"{kind}_token_expires")   # kind is "verify" or "reset" (never user input)
    with connect() as c:
        c.execute(f"UPDATE users SET {column} = ?, {expires_column} = ? WHERE id = ?", (_token_hash(token), expires, user_id))


def set_verify_token(user_id: int, token: str) -> None:
    _set_token(user_id, "verify", token, VERIFY_TTL_SECONDS)


def set_reset_token(user_id: int, token: str) -> None:
    _set_token(user_id, "reset", token, RESET_TTL_SECONDS)


def token_age_seconds(user_id: int, kind: str) -> float | None:
    """How long ago the user's current verify/reset token was issued (None if there is none). Used as a resend cooldown."""
    from datetime import timedelta
    ttl = VERIFY_TTL_SECONDS if kind == "verify" else RESET_TTL_SECONDS
    with connect() as c:
        row = c.execute(f"SELECT {kind}_token_expires AS e FROM users WHERE id = ? AND {kind}_token IS NOT NULL", (user_id,)).fetchone()
    expires = parse_dt(row["e"]) if row else None
    if not expires:
        return None
    return (now_utc() - (expires - timedelta(seconds=ttl))).total_seconds()


def _user_id_for_token(c: sqlite3.Connection, kind: str, token: str) -> int | None:
    row = c.execute(
        f"SELECT id, {kind}_token_expires AS e FROM users WHERE {kind}_token = ?", (_token_hash(token),)
    ).fetchone()
    if not row:
        return None
    expires = parse_dt(row["e"])
    return row["id"] if expires and expires > now_utc() else None


def verify_email(token: str) -> int | None:
    """Confirms the e-mail for a valid, unexpired token. Returns the user id or None."""
    with connect() as c:
        uid = _user_id_for_token(c, "verify", token)
        if uid is None:
            return None
        c.execute("UPDATE users SET is_verified = 1, verify_token = NULL, verify_token_expires = NULL WHERE id = ?", (uid,))
        return uid


def reset_password(token: str, new_password: str) -> int | None:
    """Sets a new password for a valid reset token, invalidates the token and all sessions. Returns the user id or None."""
    with connect() as c:
        uid = _user_id_for_token(c, "reset", token)
        if uid is None:
            return None
        username = c.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()["username"]
        c.execute(
            """UPDATE users SET password_hash = ?, is_verified = 1,
                      reset_token = NULL, reset_token_expires = NULL
               WHERE id = ?""",
            (hash_password(new_password), uid),
        )
        c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))                   # log out everywhere
        c.execute("DELETE FROM login_attempts WHERE login_key = ?", (username.lower(),))  # lift any lockout
        return uid


# ---------- archive of solved tasks ----------
def save_archive(user_id: int, entry: dict) -> int:
    """Stores a checked task. Checking the same task again (same attempt_id) updates the existing row."""
    now = iso(now_utc())
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """INSERT INTO archive (user_id, attempt_id, topic, source, title, company, condition_text,
                                    review, explanation, score, total, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, attempt_id) DO UPDATE SET
                   review = excluded.review, explanation = excluded.explanation,
                   score = excluded.score, total = excluded.total, updated_at = excluded.updated_at""",
            (
                user_id, entry["attempt_id"], entry["topic"], entry["source"], entry["title"], entry["company"],
                entry["condition"], json.dumps(entry["review"], ensure_ascii=False), entry["explanation"],
                entry["score"], entry["total"], now, now,
            ),
        )
        row_id = c.execute(
            "SELECT id FROM archive WHERE user_id = ? AND attempt_id = ?", (user_id, entry["attempt_id"])
        ).fetchone()["id"]
        c.execute(
            "DELETE FROM archive WHERE user_id = ? AND id NOT IN (SELECT id FROM archive WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, ARCHIVE_LIMIT),
        )
        return row_id


def _archive_light(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "topic": row["topic"], "source": row["source"], "title": row["title"], "company": row["company"],
        "score": row["score"], "total": row["total"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_archive(user_id: int, topic: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    where, params = "user_id = ?", [user_id]
    if topic:
        where += " AND topic = ?"
        params.append(topic)
    with connect() as c:
        total = c.execute(f"SELECT COUNT(*) AS n FROM archive WHERE {where}", params).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM archive WHERE {where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", params + [limit, offset]
        ).fetchall()
    return [_archive_light(r) for r in rows], total


def get_archive_item(user_id: int, item_id: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM archive WHERE id = ? AND user_id = ?", (item_id, user_id)).fetchone()
    if not row:
        return None
    item = _archive_light(row)
    item.update(condition=row["condition_text"], review=json.loads(row["review"]), explanation=row["explanation"])
    return item


def delete_archive_item(user_id: int, item_id: int) -> bool:
    with connect() as c:
        return c.execute("DELETE FROM archive WHERE id = ? AND user_id = ?", (item_id, user_id)).rowcount > 0


# ---------- rating ----------
def claim_task(jti: str, user_id: int | None) -> bool:
    """True for the first check of a served task. Later checks (also by someone else) still grade, but can no longer
    change a rating, because the first check has already revealed the answers."""
    now = iso(now_utc())
    with connect() as c:
        c.execute("DELETE FROM checked_tasks WHERE created_at < ?", (iso(now_utc() - timedelta(days=2)),))
        try:
            c.execute("INSERT INTO checked_tasks (jti, user_id, created_at) VALUES (?, ?, ?)", (jti, user_id, now))
        except sqlite3.IntegrityError:
            return False
    return True


def record_result(user_id: int, score: int, total: int, penalty: float) -> dict:
    """Moves the rating after the first check of a task. Returns the rating change for the browser."""
    today = now_utc().strftime("%Y-%m-%d")
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT elo, calibration_left, elo_day, elo_day_gain FROM users WHERE id = ?", (user_id,)).fetchone()
        new_state, info = rating.apply_result(dict(row), score, total, penalty, today)
        c.execute(
            """UPDATE users SET elo = ?, level = ?, calibration_left = ?, elo_day = ?, elo_day_gain = ?,
                      tasks_total = tasks_total + 1, points_correct = points_correct + ?, points_total = points_total + ?
               WHERE id = ?""",
            (new_state["elo"], new_state["level"], new_state["calibration_left"], new_state["elo_day"], new_state["elo_day_gain"],
             score, total, user_id),
        )
        return info


def set_profile(user_id: int, nickname: str | None = None, avatar: str | None = None) -> str:
    """-> 'ok' | 'taken' (the nickname belongs to someone else)."""
    with connect() as c:
        try:
            if nickname is not None:
                c.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname, user_id))
            if avatar is not None:
                c.execute("UPDATE users SET avatar = ? WHERE id = ?", (avatar, user_id))
        except sqlite3.IntegrityError:
            return "taken"
    return "ok"
