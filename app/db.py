"""SQLite storage: accounts, API keys, balances and the spend log.

One file, no ORM and no server to run — the gateway is a single container, and
a shop with a few hundred users does not need more than that. Every write goes
through a short transaction; WAL keeps reads fast while a request is logging.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from hashlib import pbkdf2_hmac

DB_PATH = os.environ.get("DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    login         TEXT NOT NULL UNIQUE,
    pw_salt       TEXT NOT NULL,
    pw_hash       TEXT NOT NULL,
    balance       REAL NOT NULL DEFAULT 0,
    blocked       INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS access_codes (
    code       TEXT PRIMARY KEY,
    amount     REAL NOT NULL,
    created_at REAL NOT NULL,
    used_by    INTEGER REFERENCES users(id),
    used_at    REAL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model             TEXT NOT NULL,
    prompt_tokens     REAL NOT NULL DEFAULT 0,
    completion_tokens REAL NOT NULL DEFAULT 0,
    cost              REAL NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS usage_user_time ON usage_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS keys_user ON api_keys(user_id);
"""

SESSION_TTL = 60 * 60 * 24 * 30


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# passwords and sessions
# --------------------------------------------------------------------------


def hash_password(password: str, salt: str = None) -> tuple:
    salt = salt or secrets.token_hex(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return salt, digest.hex()


def create_user(login: str, password: str) -> int:
    salt, digest = hash_password(password)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO users (login, pw_salt, pw_hash, created_at) VALUES (?, ?, ?, ?)",
            (login, salt, digest, time.time()),
        )
        return cur.lastrowid


def find_user(login: str):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()


def check_password(user, password: str) -> bool:
    _, digest = hash_password(password, user["pw_salt"])
    return secrets.compare_digest(digest, user["pw_hash"])


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + SESSION_TTL),
        )
    return token


def user_for_session(token: str):
    if not token:
        return None
    with db() as conn:
        return conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, time.time()),
        ).fetchone()


def drop_session(token: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def _admin_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_admin_session(token: str, expires_at: float) -> None:
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO admin_sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (_admin_token_hash(token), now, expires_at),
        )


def admin_session_valid(token: str) -> bool:
    if not token:
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admin_sessions WHERE token_hash = ? AND expires_at > ?",
            (_admin_token_hash(token), time.time()),
        ).fetchone()
    return row is not None


def drop_admin_session(token: str) -> None:
    if not token:
        return
    with db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (_admin_token_hash(token),))


# --------------------------------------------------------------------------
# api keys
# --------------------------------------------------------------------------


def new_key(user_id: int, label: str = "") -> str:
    key = "sk-" + secrets.token_hex(24)
    with db() as conn:
        conn.execute(
            "INSERT INTO api_keys (user_id, key, label, created_at) VALUES (?, ?, ?, ?)",
            (user_id, key, label, time.time()),
        )
    return key


def user_for_key(key: str):
    """The account behind an sk- key, or None if unknown or revoked."""
    if not key:
        return None
    with db() as conn:
        return conn.execute(
            "SELECT u.* FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE k.key = ? AND k.revoked = 0",
            (key,),
        ).fetchone()


def keys_of(user_id: int) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT id, key, label, created_at, revoked FROM api_keys "
            "WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------


def balance_of(user_id: int) -> float:
    with db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        return float(row["balance"]) if row else 0.0


def top_up(user_id: int, amount: float) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))


def redeem(code: str, user_id: int):
    """Apply an access code. Returns the amount, or None if the code is spent."""
    code = code.strip().upper()
    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT * FROM access_codes WHERE code = ?", (code,)).fetchone()
        if not row or row["used_by"] is not None:
            return None
        conn.execute(
            "UPDATE access_codes SET used_by = ?, used_at = ? WHERE code = ?",
            (user_id, now, code),
        )
        conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (row["amount"], user_id))
        return float(row["amount"])


def make_code(amount: float) -> str:
    code = "WN-" + secrets.token_hex(12).upper()
    with db() as conn:
        conn.execute(
            "INSERT INTO access_codes (code, amount, created_at) VALUES (?, ?, ?)",
            (code, amount, time.time()),
        )
    return code


def recent_codes(limit: int = 50) -> list:
    limit = max(1, min(200, int(limit)))
    with db() as conn:
        rows = conn.execute(
            "SELECT c.code, c.amount, c.created_at, u.login AS used_by "
            "FROM access_codes c LEFT JOIN users u ON u.id = c.used_by "
            "ORDER BY c.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "code": row["code"],
            "amount": float(row["amount"]),
            "created_at": float(row["created_at"]),
            "used_by": row["used_by"],
        }
        for row in rows
    ]


def charge(user_id: int, model: str, prompt_tokens: float, completion_tokens: float, cost: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET balance = MAX(balance - ?, 0) WHERE id = ?",
            (cost, user_id),
        )
        conn.execute(
            "INSERT INTO usage_log (user_id, model, prompt_tokens, completion_tokens, cost, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, model, prompt_tokens, completion_tokens, cost, time.time()),
        )


def stats(user_id: int) -> dict:
    now = time.time()
    with db() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(cost),0) c, COUNT(*) n, COALESCE(MAX(cost),0) m "
            "FROM usage_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        day = conn.execute(
            "SELECT COALESCE(SUM(cost),0) c, COUNT(*) n FROM usage_log "
            "WHERE user_id = ? AND created_at > ?",
            (user_id, now - 86400),
        ).fetchone()
        by_model = conn.execute(
            "SELECT model, SUM(cost) c, COUNT(*) n FROM usage_log WHERE user_id = ? "
            "GROUP BY model ORDER BY c DESC LIMIT 8",
            (user_id,),
        ).fetchall()
        daily = conn.execute(
            "SELECT CAST((? - created_at) / 86400 AS INTEGER) d, SUM(cost) c FROM usage_log "
            "WHERE user_id = ? AND created_at > ? GROUP BY d",
            (now, user_id, now - 14 * 86400),
        ).fetchall()
        recent = conn.execute(
            "SELECT model, prompt_tokens, completion_tokens, cost, created_at FROM usage_log "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
            (user_id,),
        ).fetchall()

    days = {int(row["d"]): float(row["c"]) for row in daily}
    return {
        "total": float(total["c"]),
        "requests": int(total["n"]),
        "max_request": float(total["m"]),
        "day_total": float(day["c"]),
        "day_requests": int(day["n"]),
        "by_model": [{"model": r["model"], "cost": float(r["c"]), "requests": int(r["n"])} for r in by_model],
        # oldest first, so the chart reads left to right
        "daily": [{"days_ago": i, "cost": days.get(i, 0.0)} for i in range(13, -1, -1)],
        "recent": [
            {
                "model": r["model"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "cost": float(r["cost"]),
                "at": r["created_at"],
            }
            for r in recent
        ],
    }


def dump_json(row) -> str:
    return json.dumps({k: row[k] for k in row.keys()}, ensure_ascii=False)
