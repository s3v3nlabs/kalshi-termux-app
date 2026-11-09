"""Local encrypted secret store (Fernet). Never phones home."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.config import settings


# scrypt cost: n=2**15, r=8, p=1 (~32 MiB, well under a second on a phone).
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


def derive_scrypt_key(passphrase: str, salt: bytes, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    """Fernet key from a passphrase via scrypt (current KDF)."""
    kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Legacy PBKDF2 key, only used to open (and then migrate) old vaults."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class Vault:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else settings.vault_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet: Optional[Fernet] = None
        self._unlocked = False

    @property
    def unlocked(self) -> bool:
        return self._unlocked

    def exists(self) -> bool:
        return self.path.is_file()

    def unlock(self, passphrase: str) -> bool:
        if not self.path.is_file():
            # Initialize empty vault
            salt = os.urandom(16)
            key = derive_scrypt_key(passphrase, salt)
            f = Fernet(key)
            blob = {
                "kdf": "scrypt",
                "scrypt": {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
                "salt": base64.b64encode(salt).decode(),
                "payload": f.encrypt(json.dumps({}).encode()).decode(),
            }
            self.path.write_text(json.dumps(blob), encoding="utf-8")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._fernet = f
            self._unlocked = True
            return True

        blob = json.loads(self.path.read_text(encoding="utf-8"))
        salt = base64.b64decode(blob["salt"])
        legacy = blob.get("kdf") != "scrypt"
        if legacy:
            key = _derive_key(passphrase, salt)
        else:
            params = blob.get("scrypt") or {}
            key = derive_scrypt_key(
                passphrase, salt, params.get("n", SCRYPT_N), params.get("r", SCRYPT_R), params.get("p", SCRYPT_P)
            )
        f = Fernet(key)
        try:
            raw = f.decrypt(blob["payload"].encode())
        except InvalidToken:
            self._unlocked = False
            self._fernet = None
            return False
        if legacy:
            # Migrate an old PBKDF2 vault to scrypt with a fresh salt.
            salt = os.urandom(16)
            f = Fernet(derive_scrypt_key(passphrase, salt))
            blob = {
                "kdf": "scrypt",
                "scrypt": {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
                "salt": base64.b64encode(salt).decode(),
                "payload": f.encrypt(raw).decode(),
            }
            self.path.write_text(json.dumps(blob), encoding="utf-8")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._fernet = f
        self._unlocked = True
        return True

    def lock(self) -> None:
        self._fernet = None
        self._unlocked = False

    def _read(self) -> dict:
        if not self._fernet or not self.path.is_file():
            raise RuntimeError("Vault locked")
        blob = json.loads(self.path.read_text(encoding="utf-8"))
        raw = self._fernet.decrypt(blob["payload"].encode())
        return json.loads(raw.decode())

    def _write(self, data: dict) -> None:
        if not self._fernet:
            raise RuntimeError("Vault locked")
        blob = json.loads(self.path.read_text(encoding="utf-8"))
        enc = self._fernet.encrypt(json.dumps(data).encode()).decode()
        blob["payload"] = enc
        self.path.write_text(json.dumps(blob), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> None:
        data = self._read()
        data.pop(key, None)
        self._write(data)

    def store_api_credentials(self, key_id: str, pem_text: str) -> Path:
        """Store key id in vault; write PEM to secrets/ with mode 600."""
        self.set("kalshi_key_id", key_id)
        dest = settings.secrets_dir / "kalshi.key"
        dest.write_text(pem_text.strip() + "\n", encoding="utf-8")
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        # Also mirror into settings for this process
        settings.key_id = key_id
        settings.key_path = dest
        os.environ["KALSHI_KEY_ID"] = key_id
        return dest
