"""Encrypted at-rest storage for broker OAuth tokens.

Tokens are encrypted with a passphrase-derived key (PBKDF2-HMAC-SHA256, 200k
iterations), then sealed with AES-GCM. Per-user, single-machine — appropriate
for self-hosted deployments.

For multi-tenant deployments, replace this with a real KMS-backed store.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


@dataclass(frozen=True)
class StoredToken:
    broker: str
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[datetime]
    user_id: Optional[str]
    raw: dict[str, object]


class TokenStore:
    """SQLite-backed token vault.

    Schema:
        tokens(broker PRIMARY KEY, ciphertext BLOB, nonce BLOB, salt BLOB, updated TEXT)
    """

    def __init__(self, path: Path, passphrase: str) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._passphrase = passphrase.encode("utf-8")
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    broker     TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL,
                    nonce      BLOB NOT NULL,
                    salt       BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def _derive_key(self, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
        return kdf.derive(self._passphrase)

    def save(self, token: StoredToken) -> None:
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = self._derive_key(salt)
        plaintext = json.dumps({
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "user_id": token.user_id,
            "raw": token.raw,
        }).encode("utf-8")
        ct = AESGCM(key).encrypt(nonce, plaintext, associated_data=token.broker.encode("utf-8"))
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tokens(broker, ciphertext, nonce, salt, updated_at) VALUES (?, ?, ?, ?, ?)",
                (token.broker, ct, nonce, salt, datetime.now().isoformat()),
            )

    def load(self, broker: str) -> Optional[StoredToken]:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT ciphertext, nonce, salt FROM tokens WHERE broker = ?", (broker,),
            ).fetchone()
        if row is None:
            return None
        ct, nonce, salt = row
        key = self._derive_key(salt)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ct, associated_data=broker.encode("utf-8"))
        except Exception:
            return None
        data = json.loads(plaintext.decode("utf-8"))
        return StoredToken(
            broker=broker,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            user_id=data.get("user_id"),
            raw=data.get("raw", {}),
        )

    def delete(self, broker: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM tokens WHERE broker = ?", (broker,))
