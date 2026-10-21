"""SQLite storage: users, sessions and per-user history of solved (generated) tasks."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# На Render задайте DB_PATH на путь persistent disk (например /var/data/app.db),
# иначе файл БД будет стираться при каждом деплое.
DB_PATH = os.environ.get("DB_PATH") or str(BASE_DIR / "data.db")

HISTORY_LIMIT = 300  # сколько последних задач хранить на пользователя

# Логины, которым автоматически выдаётся PRO: PRO_USERS="alice,bob@gmail.com"
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
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   token_hash TEXT PRIMARY KEY,
                   user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )


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
    return {
        "id": row["id"],
        "username": row["username"],
        "is_pro": bool(row["is_pro"]) or row["username"].lower() in PRO_USERS,
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
        c.execute("INSERT INTO sessions (token_hash, user_id) VALUES (?, ?)", (_hash_token(token), user_id))
    return token


def user_by_token(token: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_hash_token(token),),
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
