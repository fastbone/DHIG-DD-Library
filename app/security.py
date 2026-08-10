"""Password hashing, token handling and secret-at-rest encryption.

The instance secret comes from ``DD_SECRET_KEY``. If it is unset we generate one
into ``data/secret.key`` (0600) so a single-machine install works out of the box
— but in Docker, or anywhere the data directory might be rebuilt independently
of the volume, set it explicitly: losing it makes every stored API key
undecryptable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

# scrypt parameters. N=2^14 with r=8 costs 128*N*r = 16 MiB and ~50ms per hash.
#
# `maxmem` must be passed explicitly: OpenSSL applies a 32 MiB default ceiling
# and raises "memory limit exceeded" when the parameters approach it, which
# turns into a 500 on the login route on some hosts. Sizing it from the
# parameters keeps hashing independent of the platform default.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


def _maxmem(n: int, r: int) -> int:
    return int(128 * n * r * 1.5)

_AAD = b"dd-library/secret/v1"
_KEY_SALT = b"dd-library/instance-key/v1"

_cached_key: bytes | None = None


# --- passwords -----------------------------------------------------------


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_DKLEN, maxmem=_maxmem(_SCRYPT_N, _SCRYPT_R),
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(dk).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        # Parameters come from the stored hash, so hashes written with older
        # settings keep verifying after a parameter change.
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(dk_b64)),
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


# --- tokens --------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """Sessions are stored by digest, so a database read cannot impersonate."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


# --- secret at rest ------------------------------------------------------


def instance_key() -> bytes:
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    raw = os.environ.get("DD_SECRET_KEY")
    if not raw:
        path = settings.data_dir / "secret.key"
        if path.exists():
            raw = path.read_text().strip()
        else:
            raw = secrets.token_urlsafe(48)
            path.write_text(raw)
            os.chmod(path, 0o600)
    _cached_key = hashlib.scrypt(
        raw.encode("utf-8"), salt=_KEY_SALT, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_DKLEN, maxmem=_maxmem(_SCRYPT_N, _SCRYPT_R),
    )
    return _cached_key


def secret_key_source() -> str:
    if os.environ.get("DD_SECRET_KEY"):
        return "env"
    return "file" if (settings.data_dir / "secret.key").exists() else "unset"


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Returns (nonce, ciphertext)."""
    nonce = os.urandom(12)
    return nonce, AESGCM(instance_key()).encrypt(nonce, plaintext.encode("utf-8"), _AAD)


def decrypt(nonce: bytes, ciphertext: bytes) -> str:
    return AESGCM(instance_key()).decrypt(bytes(nonce), bytes(ciphertext), _AAD).decode("utf-8")


# --- paths ---------------------------------------------------------------


def is_within(child: Path, parent: Path) -> bool:
    """True when `child` resolves inside `parent`. Rejects .. and symlink escapes."""
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True
