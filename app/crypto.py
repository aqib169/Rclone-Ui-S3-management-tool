"""Encryption at rest for remote credentials.

A Fernet key is generated once and stored in data/.encryption_key (0600).
If the key file is lost, stored credentials are unrecoverable and every
remote must be re-entered.
"""

import os

from cryptography.fernet import Fernet

from app.config import settings

_key_path = settings.data_dir / ".encryption_key"

_fernet: Fernet | None = None


def _get_or_create_key() -> bytes:
    if _key_path.exists():
        return _key_path.read_bytes()
    key = Fernet.generate_key()
    _key_path.parent.mkdir(parents=True, exist_ok=True)
    _key_path.write_bytes(key)
    os.chmod(_key_path, 0o600)
    return key


def encrypt(plaintext: str) -> str:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_or_create_key())
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not is_encrypted(ciphertext):
        return ciphertext
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_or_create_key())
    return _fernet.decrypt(ciphertext.encode()).decode()


def is_encrypted(value: str) -> bool:
    return value.startswith("gAAAAA")


async def encrypt_plaintext_rows() -> int:
    """One-time migration: encrypt any legacy plaintext remote credentials."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Remote

    changed = 0
    async with SessionLocal() as db:
        rows = (await db.execute(select(Remote))).scalars().all()
        for remote in rows:
            if not is_encrypted(remote.access_key):
                remote.access_key = encrypt(remote.access_key)
                changed += 1
            if not is_encrypted(remote.secret_key):
                remote.secret_key = encrypt(remote.secret_key)
                changed += 1
        if changed:
            await db.commit()
    return changed
