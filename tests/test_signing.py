"""Speed pack item 5: Ed25519 + RSA-PSS signing, key type from the parsed PEM."""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from app.kalshi.client import KalshiClient, detect_key_type, key_type_of_file
from app.kalshi.signing import WS_SIGN_PATH, Signer, presign_text


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _verify(key, text: str, sig_b64: str) -> None:
    sig = base64.b64decode(sig_b64)
    pub = key.public_key()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        pub.verify(sig, text.encode())
    else:
        pub.verify(sig, text.encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())


@pytest.fixture(params=["ed25519", "rsa"])
def keyfile(request, tmp_path):
    key = ed25519.Ed25519PrivateKey.generate() if request.param == "ed25519" else \
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "k.pem"
    p.write_text(_pem(key))
    return request.param, key, p


def test_detect_same_pem_header_both_types(keyfile):
    kind, key, path = keyfile
    pem = path.read_text()
    assert pem.startswith("-----BEGIN PRIVATE KEY-----")  # header alone can't tell
    assert detect_key_type(pem) == kind
    assert key_type_of_file(path) == kind
    assert Signer("kid", path).key_type() == kind


def test_rest_and_ws_signatures_verify(keyfile):
    kind, key, path = keyfile
    s = Signer("kid", path)
    h = s.headers("GET", "/trade-api/v2/portfolio/balance?x=1")
    _verify(key, presign_text(h["KALSHI-ACCESS-TIMESTAMP"], "GET", "/trade-api/v2/portfolio/balance"),
            h["KALSHI-ACCESS-SIGNATURE"])
    w = s.headers("GET", WS_SIGN_PATH)
    _verify(key, f"{w['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/ws/v2", w["KALSHI-ACCESS-SIGNATURE"])
    assert w["KALSHI-ACCESS-KEY"] == "kid"


def test_sync_client_signs_with_either_type(keyfile):
    kind, key, path = keyfile
    cl = KalshiClient(key_id="kid", key_path=path)
    sig = cl.generate_signature("POST", "/trade-api/v2/portfolio/events/orders?q=1", "1700000000000")
    _verify(key, "1700000000000POST/trade-api/v2/portfolio/events/orders", sig)


def test_bad_pem_is_none(tmp_path):
    assert detect_key_type("not a key") is None
    assert key_type_of_file(tmp_path / "missing.pem") is None
    assert Signer("kid", tmp_path / "missing.pem").key_type() is None
