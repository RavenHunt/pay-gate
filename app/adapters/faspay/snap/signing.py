"""SNAP asymmetric signing (Faspay Signature SNAP).

stringToSign = METHOD ":" EndpointUrl ":" lower(hex(SHA256(minified body))) ":" X-TIMESTAMP
signature    = base64(RSA-SHA256-sign(private key, stringToSign))
"""

from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.utils.hashing import sha256_hex


def string_to_sign(method: str, endpoint_url: str, body: bytes, timestamp: str) -> str:
    return f"{method.upper()}:{endpoint_url}:{sha256_hex(body).lower()}:{timestamp}"


def sign(private_key: rsa.RSAPrivateKey, message: str) -> str:
    raw = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(raw).decode()


def verify(public_key: rsa.RSAPublicKey, signature_b64: str, message: str) -> bool:
    try:
        raw = base64.b64decode(signature_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        public_key.verify(raw, message.encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True
