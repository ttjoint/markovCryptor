"""X25519 handshake orchestration on top of the MKX1 image-stego channel.

Two parties, Alice and Bob, exchange X25519 public keys hidden inside ordinary
looking PNG photos (carriers from ``data/``).  Each side:

1. generates an ephemeral X25519 key pair;
2. embeds its own public key into a stego image (blind extraction, QIM-DCT);
3. sends that image over the chat;
4. extracts the peer's public key from the image it received;
5. derives the shared secret, then an HKDF chain for symmetric use.

Because the same shared secret results on both sides, this is a textbook
unauthenticated DH: to also bind identities, both sides may append a MAC of
the transcript over the derived secret using their long-term key (the
pre-shared ``key`` parameter doubles as such a low-entropy secret thanks to
the salted ``derive`` in :mod:`markovcryptor.image_stego`).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from PIL import Image

from . import image_stego


class HandshakeError(Exception):
    """Raised when a handshake step fails."""


@dataclass(frozen=True)
class SessionKeys:
    """Post-handshake symmetric material."""

    shared: bytes        # raw X25519 shared secret (32 B)
    send_key: bytes      # AES-GCM key for the direction we send
    recv_key: bytes      # AES-GCM key for the direction we receive
    transcript: bytes    # transcript hash, binds both public keys


class HandshakeParty:
    """One side of the two-image handshake."""

    def __init__(self, name: str, psk: str):
        self.name = name
        self.psk = image_stego.normalise_key(psk)
        self._eph = X25519PrivateKey.generate()
        self.public_raw = self._eph.public_key().public_bytes_raw()
        self.peer_public: bytes | None = None

    # ------------------------------------------------------------- outbound

    def build_handshake_frame(self) -> bytes:
        """The MKX1 frame carrying our ephemeral public key.

        Exposed separately so alternative transports (GIF, raw bytes, a QR
        code, ...) can reuse exactly the same frame.
        """
        return image_stego.build_frame(self.public_raw)

    def make_stego_image(self, cover: Image.Image, *, step: float = image_stego.DEFAULT_STEP):
        """Embed our ephemeral public key into ``cover`` (returns stego + report)."""
        return image_stego.embed(cover, self.build_handshake_frame(), self.psk, step=step)

    # ------------------------------------------------------------- inbound

    def receive_stego_image(self, image: Image.Image) -> bytes:
        """Blindly extract the peer's public key; returns it (32 bytes)."""
        payload, info = image_stego.try_extract(image, self.psk)
        if len(payload) != image_stego.PUBLIC_KEY_BYTES:
            raise HandshakeError(f"对端公钥长度异常：{len(payload)}")
        self.peer_public = payload
        return payload

    def receive_payload(self, payload: bytes) -> bytes:
        """Accept an already-extracted payload (e.g. read from a GIF frame)."""
        if len(payload) != image_stego.PUBLIC_KEY_BYTES:
            raise HandshakeError(f"对端公钥长度异常：{len(payload)}")
        self.peer_public = payload
        return payload

    # ------------------------------------------------------------ finalize

    def derive_session(self) -> SessionKeys:
        if self.peer_public is None:
            raise HandshakeError("尚未收到对端公钥")
        peer = X25519PublicKey.from_public_bytes(self.peer_public)
        shared = self._eph.exchange(peer)
        first, second = sorted((self.public_raw, self.peer_public))
        transcript = hashlib.sha256(first + second).digest()
        # Direction keys are ordered by role: the party whose public key sorts
        # lower is the "initiator"; both sides compute the same ordering.
        mine_first = self.public_raw < self.peer_public
        okm = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=transcript,
            info=b"mkx1-handshake-v1",
        ).derive(shared)
        k1, k2 = okm[:32], okm[32:]
        send_key, recv_key = (k1, k2) if mine_first else (k2, k1)
        return SessionKeys(shared=shared, send_key=send_key, recv_key=recv_key, transcript=transcript)


# ------------------------------------------------------------------ AES-GCM


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    """AES-256-GCM encrypt; returns ``(nonce, ciphertext)``."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def open_(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key).decrypt(nonce, ciphertext, aad)
