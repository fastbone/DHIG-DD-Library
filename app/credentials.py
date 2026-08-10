"""Anthropic API key storage and client construction.

Keys are encrypted with the instance key and never returned to the browser —
the UI sees a label, the last four characters, and test results. Exactly one key
is active at a time; if none is stored the SDK's own resolution (environment
variable or `ant auth login` profile) is used unchanged.
"""

from __future__ import annotations

import time
import uuid

from anthropic import AsyncAnthropic

from . import db, security
from .config import settings

KEY_MIN_LENGTH = 20


class NoCredentials(RuntimeError):
    pass


def _row_to_public(row) -> dict:
    d = dict(row)
    d.pop("ciphertext", None)
    d.pop("nonce", None)
    d["is_active"] = bool(d["is_active"])
    return d


def list_keys() -> list[dict]:
    return [
        _row_to_public(r)
        for r in db.rows("SELECT * FROM api_keys ORDER BY is_active DESC, created_at DESC")
    ]


def add_key(label: str, key: str, *, actor: str | None = None, activate: bool = True) -> dict:
    key = (key or "").strip()
    label = (label or "").strip()[:80] or "unnamed"
    if len(key) < KEY_MIN_LENGTH or " " in key:
        raise ValueError("that does not look like an API key")
    nonce, ciphertext = security.encrypt(key)
    key_id = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO api_keys(id, label, nonce, ciphertext, last4, is_active, created_at,"
        " created_by) VALUES(?,?,?,?,?,?,?,?)",
        (key_id, label, nonce, ciphertext, key[-4:], 0, time.time(), actor),
    )
    if activate or not db.scalar("SELECT COUNT(*) FROM api_keys WHERE is_active=1"):
        set_active(key_id, actor=actor)
    db.audit("apikey.add", actor=actor, detail=f"{label} (…{key[-4:]})")
    return _row_to_public(db.one("SELECT * FROM api_keys WHERE id=?", (key_id,)))


def set_active(key_id: str, *, actor: str | None = None) -> dict:
    row = db.one("SELECT * FROM api_keys WHERE id=?", (key_id,))
    if row is None:
        raise ValueError("no such key")
    db.execute("UPDATE api_keys SET is_active=0")
    db.execute("UPDATE api_keys SET is_active=1 WHERE id=?", (key_id,))
    db.audit("apikey.activate", actor=actor, detail=f"{row['label']} (…{row['last4']})")
    return _row_to_public(db.one("SELECT * FROM api_keys WHERE id=?", (key_id,)))


def delete_key(key_id: str, *, actor: str | None = None) -> None:
    row = db.one("SELECT * FROM api_keys WHERE id=?", (key_id,))
    if row is None:
        raise ValueError("no such key")
    db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    if row["is_active"]:
        # Promote the newest remaining key so the app keeps working.
        nxt = db.one("SELECT id FROM api_keys ORDER BY created_at DESC LIMIT 1")
        if nxt:
            set_active(nxt["id"], actor=actor)
    db.audit("apikey.delete", actor=actor, detail=f"{row['label']} (…{row['last4']})")


def active_key() -> tuple[str, str] | None:
    """(key_id, plaintext) for the active stored key, if any."""
    row = db.one("SELECT * FROM api_keys WHERE is_active=1 LIMIT 1")
    if row is None:
        return None
    try:
        return row["id"], security.decrypt(row["nonce"], row["ciphertext"])
    except Exception as exc:  # noqa: BLE001 — wrong instance key, or corrupted row
        raise NoCredentials(
            f"stored key {row['label']!r} cannot be decrypted ({type(exc).__name__}). "
            "DD_SECRET_KEY has probably changed — re-add the key."
        ) from exc


def source() -> str:
    """Where credentials will come from: 'stored', 'environment', or 'none'."""
    if db.scalar("SELECT COUNT(*) FROM api_keys WHERE is_active=1"):
        return "stored"
    return "environment" if settings.has_api_key() else "none"


def available() -> bool:
    return source() != "none"


def get_client() -> AsyncAnthropic:
    """An async client using the stored key, else the SDK's own resolution."""
    stored = active_key()
    if stored is not None:
        key_id, plaintext = stored
        db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), key_id))
        return AsyncAnthropic(api_key=plaintext)
    if not settings.has_api_key():
        raise NoCredentials(
            "no API key configured — add one under Admin → API keys, or set ANTHROPIC_API_KEY"
        )
    return AsyncAnthropic()


async def test_key(key_id: str | None = None, *, actor: str | None = None) -> dict:
    """Cheapest possible liveness check: retrieve a model. Costs no tokens."""
    if key_id:
        row = db.one("SELECT * FROM api_keys WHERE id=?", (key_id,))
        if row is None:
            raise ValueError("no such key")
        client = AsyncAnthropic(api_key=security.decrypt(row["nonce"], row["ciphertext"]))
        label = f"{row['label']} (…{row['last4']})"
    else:
        client = get_client()
        label = f"{source()} credentials"

    ok, note = False, ""
    try:
        model = await client.models.retrieve(settings.analyst_model)
        ok = True
        note = f"{model.display_name} reachable"
    except Exception as exc:  # noqa: BLE001 — the note is the product here
        note = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        await client.close()

    if key_id:
        db.execute(
            "UPDATE api_keys SET last_test_at=?, last_test_ok=?, last_test_note=? WHERE id=?",
            (time.time(), int(ok), note, key_id),
        )
    db.audit("apikey.test", actor=actor, detail=f"{label}: {'ok' if ok else note}")
    return {"ok": ok, "note": note, "label": label, "model": settings.analyst_model}
