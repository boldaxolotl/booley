"""Authenticated, versioned encryption for the tracked confidential vocabulary."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re

_MAGIC = b"BOOLEY-CONFIDENTIAL-V1"
_AAD_PREFIX = b"booley confidential vocabulary v1:"
_MAX_SEALED_BYTES = 2 * 1024 * 1024
_KEY_BYTES = 32
_NONCE_BYTES = 12


class SealedVocabularyError(RuntimeError):
    """The encrypted vocabulary or decryption key is invalid."""


def key_id(key: bytes) -> str:
    """Return a nonsecret locator for a random 256-bit key."""
    if len(key) != _KEY_BYTES:
        raise SealedVocabularyError("the vocabulary key has an invalid length")
    return hashlib.sha256(key).hexdigest()[:24]


def sealed_key_id(sealed: bytes) -> str:
    """Read and validate the key locator without decrypting the vocabulary."""
    if len(sealed) > _MAX_SEALED_BYTES:
        raise SealedVocabularyError("the encrypted vocabulary exceeds the size limit")
    lines = sealed.splitlines()
    if (
        len(lines) != 3
        or lines[0] != _MAGIC
        or re.fullmatch(rb"[0-9a-f]{24}", lines[1]) is None
        or not lines[2]
    ):
        raise SealedVocabularyError("the encrypted vocabulary has an invalid format")
    return lines[1].decode("ascii")


def _aead(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise SealedVocabularyError("the cryptography dependency is required") from exc
    return AESGCM(key)


def seal_vocabulary(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt with a fresh nonce and bind the version and key locator."""
    identifier = key_id(key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = _aead(key).encrypt(nonce, plaintext, _AAD_PREFIX + identifier.encode("ascii"))
    return b"\n".join(
        (_MAGIC, identifier.encode("ascii"), base64.b64encode(nonce + ciphertext), b"")
    )


def unseal_vocabulary(sealed: bytes, key: bytes) -> bytes:
    """Decrypt only when the key, version, and authentication tag match."""
    identifier = sealed_key_id(sealed)
    if key_id(key) != identifier:
        raise SealedVocabularyError("the vocabulary key does not match the encrypted file")
    try:
        payload = base64.b64decode(sealed.splitlines()[2], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SealedVocabularyError("the encrypted vocabulary has invalid base64") from exc
    if len(payload) < _NONCE_BYTES + 16:
        raise SealedVocabularyError("the encrypted vocabulary is truncated")
    try:
        from cryptography.exceptions import InvalidTag

        return _aead(key).decrypt(
            payload[:_NONCE_BYTES],
            payload[_NONCE_BYTES:],
            _AAD_PREFIX + identifier.encode("ascii"),
        )
    except InvalidTag as exc:
        raise SealedVocabularyError("the encrypted vocabulary failed authentication") from exc
