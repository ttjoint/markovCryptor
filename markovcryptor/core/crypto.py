"""Symmetric and asymmetric crypto used by every channel.

Two independent things live here, both pulled out of their original homes
(``codec`` and ``handshake``) because they are not channel-specific:

* **Password-based symmetric encryption** — PBKDF2-SHA256 into AES-256-GCM.
  Used by the Chinese channel to encrypt a payload with a passphrase.
* **Ephemeral X25519 handshake** — key exchange plus an HKDF chain that yields
  per-direction keys.  Used by the image/GIF channels before any message.

They are kept separate on purpose: they solve different problems (a shared
passphrase vs. a forward-secret session) and share no state.  What they do share
is the rule that a wrong key must fail *loudly* — never silently return garbage.
"""

from __future__ import annotations

import hashlib
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import BadKey, ChannelUnavailable, InvalidInput

# ------------------------------------------------------- password-based AES-GCM

PBKDF2_ITERATIONS = 310_000
SALT_BYTES = 12
TAG_BYTES = 16
#: Fixed nonce: every message derives a *fresh* key from its own random salt, so
#: reusing the nonce across messages is safe here (the key differs each time).
_GCM_NONCE = bytes(12)


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ChannelUnavailable(
            "AES-GCM 需要 cryptography：pip install cryptography"
        ) from exc
    return AESGCM


def derive_password_key(password: str | bytes, salt: bytes) -> bytes:
    """Stretch a passphrase into a 32-byte AES key (PBKDF2-SHA256)."""
    raw = password if isinstance(password, bytes) else password.encode("utf-8")
    if not raw:
        raise InvalidInput("AES 密码不能为空")
    return hashlib.pbkdf2_hmac("sha256", raw, salt, PBKDF2_ITERATIONS, dklen=32)


def encrypt_with_password(data: bytes, password: str | bytes, aad: bytes) -> bytes:
    """Encrypt with a passphrase; returns ``salt || ciphertext``."""
    salt = os.urandom(SALT_BYTES)
    return salt + _aesgcm()(derive_password_key(password, salt)).encrypt(_GCM_NONCE, data, aad)


def decrypt_with_password(body: bytes, password: str | bytes, aad: bytes) -> bytes:
    """Reverse :func:`encrypt_with_password`.

    A truncated body is an input problem; a failed tag is a key problem.  Both
    are reported by name so the caller (and the future HTTP layer) can tell
    "your file is broken" from "your password is wrong".
    """
    if len(body) < SALT_BYTES + TAG_BYTES:
        raise InvalidInput("AES-GCM 密文被截断")
    salt, ciphertext = body[:SALT_BYTES], body[SALT_BYTES:]
    try:
        return _aesgcm()(derive_password_key(password, salt)).decrypt(_GCM_NONCE, ciphertext, aad)
    except Exception as exc:
        raise BadKey("AES-GCM 认证失败（密码错误？）") from exc


# ------------------------------------------------------------ AES-GCM with a key


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    """AES-256-GCM encrypt with an explicit key; returns ``(nonce, ciphertext)``."""
    if len(key) != 32:
        raise InvalidInput(f"AES-256 密钥必须是 32 字节，收到 {len(key)}")
    nonce = secrets.token_bytes(12)
    return nonce, _aesgcm()(key).encrypt(nonce, plaintext, aad)


def open_(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    """Reverse :func:`seal`.  A wrong key raises :class:`BadKey`."""
    if len(key) != 32:
        raise InvalidInput(f"AES-256 密钥必须是 32 字节，收到 {len(key)}")
    try:
        return _aesgcm()(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise BadKey("AES-GCM 认证失败（密钥错误或消息被篡改）") from exc


# ---------------------------------------------------------------- X25519 handshake

HKDF_INFO = b"mkx1-handshake-v1"


def generate_keypair():
    """A fresh ephemeral X25519 private key."""
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise ChannelUnavailable("X25519 需要 cryptography：pip install cryptography") from exc
    return X25519PrivateKey.generate()


def public_bytes(private_key) -> bytes:
    return private_key.public_key().public_bytes_raw()


def exchange(private_key, peer_public: bytes) -> bytes:
    """X25519 shared secret.  A malformed peer key is the caller's input problem."""
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    except ImportError as exc:  # pragma: no cover
        raise ChannelUnavailable("X25519 需要 cryptography：pip install cryptography") from exc
    try:
        return private_key.exchange(X25519PublicKey.from_public_bytes(peer_public))
    except Exception as exc:
        raise InvalidInput(f"对端公钥无效：{exc}") from exc


def session_keys(my_public: bytes, peer_public: bytes, shared: bytes) -> tuple[bytes, bytes, bytes]:
    """Derive ``(send_key, recv_key, transcript)`` from the raw shared secret.

    The transcript (a hash of both public keys in sorted order) serves two
    purposes: it binds the derived keys to this exact pair of keys, and it gives
    both sides a deterministic way to agree on direction without a round trip —
    the party whose key sorts lower sends on ``k1``.
    """
    first, second = sorted((my_public, peer_public))
    transcript = hashlib.sha256(first + second).digest()
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=transcript, info=HKDF_INFO).derive(shared)
    k1, k2 = okm[:32], okm[32:]
    mine_first = my_public < peer_public
    return (k1, k2, transcript) if mine_first else (k2, k1, transcript)


__all__ = [
    "PBKDF2_ITERATIONS", "SALT_BYTES", "TAG_BYTES", "HKDF_INFO",
    "derive_password_key", "encrypt_with_password", "decrypt_with_password",
    "seal", "open_",
    "generate_keypair", "public_bytes", "exchange", "session_keys",
]
