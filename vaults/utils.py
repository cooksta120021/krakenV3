"""Encryption helpers using Fernet with a derived key.

In production swap to KMS/HSM; this uses API_CRYPTO_KEY env for a symmetric key.
"""
import base64
import hashlib
import hmac
import time
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError as exc:  # pragma: no cover - dependency check
    raise ImproperlyConfigured("cryptography is required for encryption") from exc


def _get_crypto_key() -> bytes:
    key = getattr(settings, "API_CRYPTO_KEY", None)
    if not key:
        raise ImproperlyConfigured("API_CRYPTO_KEY is not configured")
    # Fernet requires 32 url-safe base64-encoded bytes
    if len(key) == 44:  # likely already base64 fernet key length
        return key.encode()
    digest = hashlib.sha256(key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_get_crypto_key())


def encrypt_value(value: str) -> str:
    if value is None:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - invalid token guard
        raise ImproperlyConfigured("Invalid encryption token; check API_CRYPTO_KEY") from exc


def kraken_private_request(key: str, secret: str, path: str = "/0/private/Balance", data: dict | None = None) -> dict:
    """Minimal Kraken private API request for health validation.

    Uses urllib to avoid extra dependencies. Raises on non-200 or invalid signature.
    """

    data = data or {}
    data["nonce"] = str(int(time.time() * 1000))
    postdata = urlencode(data)
    encoded = postdata.encode()

    message = (data["nonce"] + postdata).encode()
    sha = hashlib.sha256(message).digest()
    mac = hmac.new(base64.b64decode(secret), path.encode() + sha, hashlib.sha512)
    sigdigest = base64.b64encode(mac.digest())

    headers = {
        "API-Key": key,
        "API-Sign": sigdigest.decode(),
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }

    req = Request("https://api.kraken.com" + path, data=encoded, headers=headers)
    with urlopen(req, timeout=8) as resp:
        if resp.status != 200:
            raise Exception(f"Kraken status {resp.status}")
        payload = json.loads(resp.read().decode())
        if payload.get("error"):
            raise Exception(";".join(payload.get("error")))
        return payload.get("result", {})
