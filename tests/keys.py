"""Throwaway RSA keys for tests, generated once per run."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _pair() -> tuple[rsa.RSAPrivateKey, str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    # The existing integration stores the bare base64 body of the private key.
    return key, base64.b64encode(private_der).decode(), public_pem.decode()


MERCHANT_KEY, MERCHANT_PRIVATE_B64, MERCHANT_PUBLIC_PEM = _pair()
FASPAY_KEY, _FASPAY_PRIVATE_B64, FASPAY_PUBLIC_PEM = _pair()
