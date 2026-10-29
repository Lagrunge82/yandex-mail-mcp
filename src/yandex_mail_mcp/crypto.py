"""Encrypted, self-contained tokens: v1.<kid>.<base64url(nonce || AES-256-GCM(payload))>.

The payload is compact JSON with typ, exp and jti. typ and kid are bound as
associated data, so a token of one type cannot be presented as another.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import ConfigurationError

VERSION = "v1"
_KID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_key(kid: str = "k1") -> str:
    """A new key in the MCP_TOKEN_ENCRYPTION_KEYS format."""
    return f"{kid}:{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}"


class TokenCipher:
    """Seal and open tokens; the first key encrypts, every key decrypts."""

    def __init__(
        self, keys: list[tuple[str, bytes]], clock: Callable[[], float] = time.time
    ) -> None:
        if not keys:
            raise ConfigurationError("At least one token encryption key is required")
        self._kid = keys[0][0]
        self._keys = {kid: AESGCM(key) for kid, key in keys}
        self._clock = clock

    @classmethod
    def from_env(cls, value: str) -> TokenCipher:
        """Parse "kid:base64key[,kid:base64key]" (MCP_TOKEN_ENCRYPTION_KEYS)."""
        keys: list[tuple[str, bytes]] = []
        for item in filter(None, (part.strip() for part in value.split(","))):
            kid, _, encoded = item.partition(":")
            try:
                key = _b64decode(encoded.strip())
            except (binascii.Error, ValueError):
                key = b""
            if not _KID.fullmatch(kid) or len(key) != 32:
                raise ConfigurationError(
                    "MCP_TOKEN_ENCRYPTION_KEYS must be kid:base64key pairs with 32-byte keys"
                )
            if any(kid == known for known, _ in keys):
                raise ConfigurationError(f"MCP_TOKEN_ENCRYPTION_KEYS repeats key id {kid!r}")
            keys.append((kid, key))
        return cls(keys)

    @staticmethod
    def _aad(kid: str, typ: str) -> bytes:
        return f"{VERSION}.{kid}.{typ}".encode()

    def seal(self, typ: str, data: dict[str, Any], ttl: float) -> str:
        payload = {**data, "typ": typ, "exp": self._clock() + ttl, "jti": secrets.token_hex(16)}
        nonce = os.urandom(12)
        sealed = self._keys[self._kid].encrypt(
            nonce, json.dumps(payload, separators=(",", ":")).encode(), self._aad(self._kid, typ)
        )
        return f"{VERSION}.{self._kid}.{_b64encode(nonce + sealed)}"

    def open(self, token: str, typ: str) -> dict[str, Any] | None:
        """The payload of a valid, unexpired token of this type, else None."""
        version, _, rest = token.partition(".")
        kid, _, body = rest.partition(".")
        cipher = self._keys.get(kid)
        if version != VERSION or cipher is None:
            return None
        try:
            raw = _b64decode(body)
            plain = cipher.decrypt(raw[:12], raw[12:], self._aad(kid, typ))
            payload = json.loads(plain)
        except (InvalidTag, binascii.Error, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("typ") != typ:
            return None
        expires = payload.get("exp")
        if not isinstance(expires, (int, float)) or expires <= self._clock():
            return None
        return payload
