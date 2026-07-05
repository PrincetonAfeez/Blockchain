"""Crypto-related functionality."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .constants import ADDRESS_DOMAIN, ADDRESS_HASH_BYTES
from .errors import CryptoError


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def address_from_public_key(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise CryptoError("Ed25519 public key must be exactly 32 bytes")
    digest = sha256(ADDRESS_DOMAIN + public_key)[:ADDRESS_HASH_BYTES]
    return "tc1" + digest.hex()


def is_valid_address(address: str) -> bool:
    """True if address is canonical lowercase ``tc1`` + 40 hex digits.

    Toychain addresses have no checksum; a syntactically valid string may not
    correspond to any generated wallet.
    """
    if not isinstance(address, str) or not address.startswith("tc1"):
        return False
    body = address[3:]
    if len(body) != ADDRESS_HASH_BYTES * 2:
        return False
    if body != body.lower():
        return False
    try:
        bytes.fromhex(body)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class KeyPair:
    private_key: bytes
    public_key: bytes

    @property
    def address(self) -> str:
        return address_from_public_key(self.public_key)


def generate_keypair() -> KeyPair:
    private = Ed25519PrivateKey.generate()
    return KeyPair(
        private_key=private.private_bytes_raw(),
        public_key=private.public_key().public_bytes_raw(),
    )


def public_key_from_private(private_key: bytes) -> bytes:
    try:
        return Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()
    except (TypeError, ValueError) as exc:
        raise CryptoError("Invalid Ed25519 private key") from exc


def sign(private_key: bytes, payload: bytes) -> bytes:
    try:
        return Ed25519PrivateKey.from_private_bytes(private_key).sign(payload)
    except (TypeError, ValueError) as exc:
        raise CryptoError("Could not sign with the supplied private key") from exc


def verify(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
        return True
    except (InvalidSignature, TypeError, ValueError):
        return False

