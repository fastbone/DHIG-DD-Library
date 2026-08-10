"""Users, sessions and access control.

Server-side sessions (revocable, stored by token digest) in an HttpOnly cookie,
plus a per-session CSRF token that every state-changing request must echo in an
``X-CSRF-Token`` header. Two roles: ``admin`` manages accounts, API keys and
storage; ``analyst`` runs the library.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from fastapi import HTTPException, Request

from . import db, security
from .config import settings
from .events import broker

COOKIE_NAME = "dd_session"
ROLES = ("admin", "analyst")

# username|ip -> (failures, first_failure_ts)
_attempts: dict[str, tuple[int, float]] = {}
_attempts_lock = threading.Lock()


# --- users ---------------------------------------------------------------


def user_count() -> int:
    return db.scalar("SELECT COUNT(*) FROM users")


def public_user(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    return d


def get_user(username: str) -> dict | None:
    return public_user(db.one("SELECT * FROM users WHERE username=?", (username.strip().lower(),)))


def list_users() -> list[dict]:
    return [
        public_user(r)
        for r in db.rows("SELECT * FROM users ORDER BY role, username")
    ]


def create_user(
    username: str,
    password: str,
    role: str = "analyst",
    *,
    created_by: str | None = None,
    must_change_password: bool = False,
) -> dict:
    username = (username or "").strip().lower()
    if not username or not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValueError("username must be alphanumeric (- _ . allowed)")
    if len(username) > 64:
        raise ValueError("username too long")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise ValueError(f"user {username!r} already exists")
    uid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO users(id, username, password_hash, role, disabled, created_at, created_by,"
        " must_change_password) VALUES(?,?,?,?,0,?,?,?)",
        (uid, username, security.hash_password(password), role, time.time(), created_by,
         int(must_change_password)),
    )
    db.audit("user.create", actor=created_by, detail=f"{username} ({role})")
    return get_user(username)


def set_password(username: str, password: str, *, actor: str | None = None) -> None:
    row = db.one("SELECT id FROM users WHERE username=?", (username.strip().lower(),))
    if row is None:
        raise ValueError("no such user")
    db.execute(
        "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
        (security.hash_password(password), row["id"]),
    )
    # Password change invalidates other sessions for that account.
    db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    db.audit("user.password_change", actor=actor, detail=username)


def update_user(username: str, *, role: str | None = None, disabled: bool | None = None,
                actor: str | None = None) -> dict:
    user = get_user(username)
    if user is None:
        raise ValueError("no such user")
    if role is not None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if user["role"] == "admin" and role != "admin" and _admin_count() <= 1:
            raise ValueError("cannot demote the last admin")
        db.execute("UPDATE users SET role=? WHERE id=?", (role, user["id"]))
    if disabled is not None:
        if disabled and user["role"] == "admin" and _admin_count() <= 1:
            raise ValueError("cannot disable the last admin")
        db.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user["id"]))
        if disabled:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    db.audit("user.update", actor=actor, detail=f"{username} role={role} disabled={disabled}")
    return get_user(username)


def delete_user(username: str, *, actor: str | None = None) -> None:
    user = get_user(username)
    if user is None:
        raise ValueError("no such user")
    if user["role"] == "admin" and _admin_count() <= 1:
        raise ValueError("cannot delete the last admin")
    db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    db.execute("DELETE FROM users WHERE id=?", (user["id"],))
    db.audit("user.delete", actor=actor, detail=username)


def _admin_count() -> int:
    return db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0")


def bootstrap() -> None:
    """Create the first admin from the environment, if configured and absent."""
    username = os.environ.get("DD_ADMIN_USER", "").strip().lower()
    password = os.environ.get("DD_ADMIN_PASSWORD", "")
    if not username or not password:
        if user_count() == 0:
            broker.log(
                "No accounts yet — open the app and create the first administrator.",
                level="warn",
            )
        return
    existing = get_user(username)
    if existing is None:
        try:
            create_user(username, password, role="admin", created_by="bootstrap")
            broker.log(f"Created administrator {username!r} from the environment.", level="success")
        except ValueError as exc:
            broker.log(f"Could not bootstrap admin: {exc}", level="error")
    elif os.environ.get("DD_ADMIN_RESET_PASSWORD", "").lower() in {"1", "true", "yes"}:
        set_password(username, password, actor="bootstrap")
        broker.log(f"Reset password for {username!r} from the environment.", level="warn")


# --- login throttle ------------------------------------------------------


def _throttle_key(username: str, ip: str) -> str:
    return f"{username.lower()}|{ip}"


def throttle_state(username: str, ip: str) -> float:
    """Seconds remaining in a lockout, or 0."""
    with _attempts_lock:
        entry = _attempts.get(_throttle_key(username, ip))
    if not entry:
        return 0.0
    failures, first = entry
    if failures < settings.login_max_attempts:
        return 0.0
    remaining = settings.login_lockout_s - (time.time() - first)
    return max(0.0, remaining)


def record_failure(username: str, ip: str) -> None:
    key = _throttle_key(username, ip)
    now = time.time()
    with _attempts_lock:
        failures, first = _attempts.get(key, (0, now))
        if now - first > settings.login_lockout_s:
            failures, first = 0, now
        _attempts[key] = (failures + 1, first)
        if len(_attempts) > 5000:  # crude bound; restarts clear it anyway
            for k, (_, ts) in list(_attempts.items()):
                if now - ts > settings.login_lockout_s:
                    _attempts.pop(k, None)


def clear_failures(username: str, ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(_throttle_key(username, ip), None)


# --- sessions ------------------------------------------------------------


def client_ip(request: Request) -> str:
    """The peer address, as the ASGI server reports it.

    Deliberately does *not* read ``X-Forwarded-For`` here. That header is
    attacker-controlled unless the request came from a proxy we trust, and this
    value gates the login lockout and lands in the audit log — honouring a raw
    header would let anyone rotate spoofed values to sidestep the lockout and
    poison the audit trail.

    Trust lives in exactly one place: uvicorn's proxy-headers middleware, which
    rewrites the peer address from ``X-Forwarded-For`` only when the connection
    itself comes from an address in ``DD_FORWARDED_ALLOW_IPS`` (IPs or CIDR).
    Behind a proxy that is not listed, every request looks like it came from the
    proxy — a shared throttle bucket, which fails closed rather than open.
    """
    return (request.client.host if request.client else "unknown")[:64]


def login(username: str, password: str, request: Request) -> tuple[str, dict]:
    username = (username or "").strip().lower()
    ip = client_ip(request)
    wait = throttle_state(username, ip)
    if wait > 0:
        raise HTTPException(429, f"too many failed attempts — retry in {int(wait)}s")

    row = db.one("SELECT * FROM users WHERE username=?", (username,))
    stored = row["password_hash"] if row else security.hash_password("placeholder-for-timing")
    ok = security.verify_password(password or "", stored)
    if row is None or not ok or row["disabled"]:
        record_failure(username, ip)
        db.audit("login.failure", actor=username or None, ip=ip,
                 detail="disabled account" if row and row["disabled"] else "bad credentials")
        raise HTTPException(401, "invalid username or password")

    clear_failures(username, ip)
    token = security.new_token()
    csrf = security.new_token()
    now = time.time()
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at, last_seen_at,"
        " user_agent, ip) VALUES(?,?,?,?,?,?,?,?)",
        (
            security.token_fingerprint(token), row["id"], csrf, now,
            now + settings.session_ttl_hours * 3600, now,
            request.headers.get("user-agent", "")[:200], ip,
        ),
    )
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, row["id"]))
    db.audit("login.success", actor=username, ip=ip)
    broker.log(f"{username} signed in.", level="info")
    return token, {**public_user(row), "csrf": csrf}


def logout(token: str | None, actor: str | None = None) -> None:
    if not token:
        return
    db.execute("DELETE FROM sessions WHERE token_hash=?", (security.token_fingerprint(token),))
    db.audit("logout", actor=actor)


def session_user(request: Request) -> dict | None:
    """Resolve the cookie to a user, sliding the expiry. None when unauthenticated."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = db.one(
        "SELECT s.token_hash, s.csrf, s.expires_at, s.last_seen_at, u.* FROM sessions s"
        " JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
        (security.token_fingerprint(token),),
    )
    if row is None:
        return None
    now = time.time()
    if row["expires_at"] < now or row["disabled"]:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
        return None
    # Slide the window, but write at most once a minute.
    if now - (row["last_seen_at"] or 0) > 60:
        db.execute(
            "UPDATE sessions SET last_seen_at=?, expires_at=? WHERE token_hash=?",
            (now, now + settings.session_ttl_hours * 3600, row["token_hash"]),
        )
    user = public_user(row)
    user["csrf"] = row["csrf"]
    return user


def sessions_for(user_id: str) -> list[dict]:
    return [
        {k: r[k] for k in ("created_at", "expires_at", "last_seen_at", "user_agent", "ip")}
        for r in db.rows("SELECT * FROM sessions WHERE user_id=? ORDER BY last_seen_at DESC",
                         (user_id,))
    ]


def purge_expired_sessions() -> int:
    cur = db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    return cur.rowcount or 0


def cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.cookie_secure,
        "path": "/",
        "max_age": settings.session_ttl_hours * 3600,
    }


# --- route guards --------------------------------------------------------


def require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "authentication required")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "administrator role required")
    return user
