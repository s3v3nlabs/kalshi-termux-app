"""Kalshi request signing shared by the REST clients and the WebSocket service.

docs.kalshi.com/getting_started/api_keys (checked Nov 9 2026):
  pre-sign text = timestamp_ms + METHOD + path   (query string stripped, no body)
  Ed25519 keys (Kalshi's default now): Ed25519 over the pre-sign text
  RSA keys: RSA-PSS, SHA-256, MGF1-SHA-256, salt length = digest length
  Headers: KALSHI-ACCESS-KEY / -SIGNATURE (base64) / -TIMESTAMP (ms)
The PEM header does not tell the type ("BEGIN PRIVATE KEY" can be RSA or
Ed25519), so the type comes from the parsed key object.
WebSocket handshakes sign GET + /trade-api/ws/v2.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

WS_SIGN_PATH = "/trade-api/ws/v2"


def presign_text(timestamp: str, method: str, path: str) -> str:
    return f"{timestamp}{method.upper()}{path.split('?')[0]}"


def load_private_key(path: Path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def key_type_of(key) -> Optional[str]:
    if isinstance(key, Ed25519PrivateKey):
        return "ed25519"
    if isinstance(key, rsa.RSAPrivateKey):
        return "rsa"
    return None


def sign_text(private_key, text: str) -> str:
    payload = text.encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(payload)
    elif isinstance(private_key, rsa.RSAPrivateKey):
        signature = private_key.sign(
            payload,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    else:
        raise ValueError("Kalshi keys must be RSA or Ed25519 private keys")
    return base64.b64encode(signature).decode("utf-8")


class Signer:
    """Lazy-loading signer for one API key. Never logs or returns the key."""

    def __init__(self, key_id: str, key_path: Optional[Path]):
        self.key_id = (key_id or "").strip()
        self.key_path = Path(key_path) if key_path else None
        self._key = None

    @property
    def available(self) -> bool:
        return bool(self.key_id) and self.key_path is not None and self.key_path.is_file()

    def key(self):
        if self._key is None:
            if not self.key_path or not self.key_path.is_file():
                raise FileNotFoundError(f"API key file not found: {self.key_path}")
            self._key = load_private_key(self.key_path)
        return self._key

    def key_type(self) -> Optional[str]:
        if not self.available:
            return None
        try:
            return key_type_of(self.key())
        except Exception:
            return None

    def headers(self, method: str, path: str) -> dict:
        if not self.key_id:
            raise ValueError("KALSHI_KEY_ID not configured")
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sign_text(self.key(), presign_text(ts, method, path)),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
