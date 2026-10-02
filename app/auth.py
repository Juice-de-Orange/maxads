"""Authentication. Every user is a full administrator by design.

There is no public registration route: the first admin is created on startup,
further accounts are created from inside the dashboard.
"""
import hashlib
import hmac
import secrets
import sqlite3
from datetime import date

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError


_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        _hasher.verify(stored, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_user(conn: sqlite3.Connection, username: str, password: str,
                created_by: int | None = None, must_change: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO users(username, password, created_by, must_change_password) "
        "VALUES (?, ?, ?, ?)",
        (username.strip(), hash_password(password), created_by, int(must_change)),
    )
    return int(cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    """Replace a password and lift the change-on-first-login requirement."""
    conn.execute(
        "UPDATE users SET password = ?, must_change_password = 0 WHERE id = ?",
        (hash_password(password), user_id),
    )


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    if row is None:
        # Spend the same time as a real verification so the response time does
        # not reveal whether the username exists.
        _hasher.hash(password)
        return None
    if verify_password(row["password"], password):
        return row
    return None


def ensure_first_admin(conn: sqlite3.Connection,
                       password: str | None = None) -> tuple[str, str, bool] | None:
    """Create the initial admin if no user exists yet.

    With `password` (from MAXADS_ADMIN_PASSWORD) the operator chose it, so it
    stays. Without, a random one is generated and must be changed on the first
    login. Returns (username, password, generated) exactly once -- the caller
    decides where the password may appear, which is never the log file.
    """
    count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if count:
        return None
    generated = not password
    if generated:
        password = secrets.token_urlsafe(18)
    create_user(conn, "admin", password, must_change=generated)
    return ("admin", password, generated)


def visitor_hash(ip: str, user_agent: str, salt: str) -> str:
    """Stable within one day, unlinkable across days.

    The raw IP never reaches the database; the salt plus the date make the
    value useless for tracking a person beyond the current day.
    """
    material = f"{date.today().isoformat()}|{ip}|{user_agent}".encode()
    return hmac.new(salt.encode(), material, hashlib.sha256).hexdigest()[:32]
