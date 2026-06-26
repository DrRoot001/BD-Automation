"""Token encryption/decryption service.

Google refresh tokens are stored encrypted in the database using Fernet
symmetric encryption. The key is loaded from the ENCRYPTION_KEY env var.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Fernet instance — lazily initialised so import doesn't crash if key is absent
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet

    from app.config import get_settings
    key = get_settings().encryption_key
    if not key:
        return None  # Encryption disabled — tokens stored in plain text (dev only)

    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet
    except Exception as exc:
        logger.warning(f"[Crypto] Failed to initialise Fernet: {exc}. Tokens will be stored unencrypted.")
        return None


def encrypt_token(token: str | None) -> str | None:
    """Encrypt a plaintext token for DB storage.

    Falls back to returning the token unchanged if ENCRYPTION_KEY is not set
    (acceptable for local dev, not for production).
    """
    if not token:
        return token
    f = _get_fernet()
    if f is None:
        return token  # No-op in dev when key is absent
    try:
        return f.encrypt(token.encode()).decode()
    except Exception as exc:
        logger.error(f"[Crypto] Encryption failed: {exc}")
        return token  # Best-effort: return plaintext rather than lose the token


def decrypt_token(encrypted: str | None) -> str | None:
    """Decrypt a token retrieved from the DB.

    Falls back gracefully if the value is already plaintext (unencrypted legacy
    records) or if ENCRYPTION_KEY is not set.
    """
    if not encrypted:
        return encrypted
    f = _get_fernet()
    if f is None:
        return encrypted  # No-op in dev
    try:
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        # Value may be unencrypted legacy data — return as-is
        return encrypted
