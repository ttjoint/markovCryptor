"""PBX1 message envelope: what actually rides inside an MKX1 stego payload.

An MKX1 frame (``core/framing.py``) gives us "N bytes got through the carrier
intact". It says nothing about *what* those bytes mean, and its header is frozen.
So the meaning lives here, one level up: a tiny tagged envelope inside the frame
payload.

Three kinds of message exist, and the distinction matters to the receiver:

``HANDSHAKE``
    "Here is my ephemeral public key." Always carried by an image, never by
    text -- text carriers are too small and need a trained model, and pinning
    the handshake to one carrier keeps the state machine simple.

``BODY_IMAGE`` / ``BODY_TEXT``
    An AES-GCM ciphertext of the user's plaintext. The two tags are separate
    even though the cryptography is identical, because they name the carrier
    the bytes arrived on. A receiver that finds ``BODY_TEXT`` inside an image
    knows something is inconsistent and should say so in the log rather than
    guess.

The envelope is deliberately not authenticated on its own: the type tag is
covered by the GCM associated data (see :func:`seal_body`), so an attacker cannot
relabel a body message without invalidating it. Handshakes carry no secret and
are protected by the MKX1 CRC plus the fact that X25519 rejects bad key material.
"""

from __future__ import annotations

import struct
from enum import Enum

from ..core import crypto
from ..core.errors import BadKey, InvalidInput, NoHiddenFrame
from ..core.framing import build_frame, parse_frame

MAGIC = b"PBX1"
#: ``MAGIC | kind(1)`` -- the smallest possible envelope, so tiny covers still fit.
HEADER_BYTES = len(MAGIC) + 1
PUBLIC_KEY_BYTES = 32
NONCE_BYTES = 12


class Kind(str, Enum):
    HANDSHAKE = "H"
    BODY_IMAGE = "B"
    BODY_TEXT = "T"

    @classmethod
    def of(cls, raw: bytes | str) -> "Kind":
        text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else raw
        try:
            return cls(text)
        except ValueError:
            legal = ", ".join(member.value for member in cls)
            raise NoHiddenFrame(
                f"PBX1 信封类型未知（{text!r}），合法值：{legal}"
            ) from None


class Mode(str, Enum):
    """Why a handshake image was sent.

    Without this the two sides deadlock: both send a request, both wait for a
    response, neither ever replies.
    """

    REQUEST = "Q"     # "I want a session; here is my key"
    RESPONSE = "R"    # "I received yours; here is mine"

    @classmethod
    def of(cls, raw: bytes | str) -> "Mode":
        text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else raw
        try:
            return cls(text)
        except ValueError:
            legal = ", ".join(f"{m.name}={m.value}" for m in cls)
            raise NoHiddenFrame(f"PBX1 握手模式未知（{text!r}），合法值：{legal}") from None


# ------------------------------------------------------------------ handshake


def build_handshake(public_key: bytes, mode: Mode) -> bytes:
    """``MAGIC | 'H' | pub | mode`` -- 38 bytes, fits a 120x120 cover."""
    if len(public_key) != PUBLIC_KEY_BYTES:
        raise InvalidInput(
            f"X25519 公钥必须是 {PUBLIC_KEY_BYTES} 字节，收到 {len(public_key)}"
        )
    return MAGIC + Kind.HANDSHAKE.value.encode() + public_key + mode.value.encode()


def parse_handshake(payload: bytes) -> tuple[bytes, Mode]:
    """Return ``(peer_public, mode)``; raises if this is not a handshake."""
    body = _body(payload, Kind.HANDSHAKE)
    if len(body) != PUBLIC_KEY_BYTES + 1:
        raise NoHiddenFrame(
            f"握手报文长度异常：{len(body)} 字节，应为 {PUBLIC_KEY_BYTES + 1}"
        )
    return body[:PUBLIC_KEY_BYTES], Mode.of(body[PUBLIC_KEY_BYTES:])


# ----------------------------------------------------------------------- body


def _aad(kind: Kind) -> bytes:
    # Binding the tag into the AEAD is what stops a body message from being
    # relabelled as text (or vice versa) without breaking authentication.
    return MAGIC + kind.value.encode()


def seal_body(kind: Kind, key: bytes, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` into a body envelope for ``kind``."""
    if kind is Kind.HANDSHAKE:
        raise InvalidInput("握手报文不含密文，请用 build_handshake")
    nonce, ciphertext = crypto.seal(key, plaintext, _aad(kind))
    return MAGIC + kind.value.encode() + nonce + ciphertext


def open_body(payload: bytes, kind: Kind, key: bytes) -> bytes:
    """Decrypt a body envelope.

    Raises :class:`NoHiddenFrame` if the envelope is not the kind we expected
    (that is a structural fact, not a key problem), and :class:`BadKey` if the
    tag fails. The caller *must* treat ``BadKey`` as "no hidden message here"
    rather than surfacing it -- see ``docs/TERMINAL.md`` §5.
    """
    if kind is Kind.HANDSHAKE:
        raise InvalidInput("握手报文不含密文，请用 parse_handshake")
    body = _body(payload, kind)
    if len(body) < NONCE_BYTES:
        raise NoHiddenFrame(f"正文报文过短：{len(body)} 字节，连 nonce 都放不下")
    return crypto.open_(key, body[:NONCE_BYTES], body[NONCE_BYTES:], _aad(kind))


# -------------------------------------------------------------------- helpers


def _body(payload: bytes, kind: Kind) -> bytes:
    """Strip and validate ``MAGIC | kind``; returns the remainder."""
    if len(payload) < HEADER_BYTES:
        raise NoHiddenFrame(
            f"载荷只有 {len(payload)} 字节，装不下 PBX1 信封头"
        )
    if payload[: len(MAGIC)] != MAGIC:
        raise NoHiddenFrame("不是 PBX1 信封")
    found = Kind.of(payload[len(MAGIC): len(MAGIC) + 1])
    if found is not kind:
        raise NoHiddenFrame(
            f"信封类型是 {found.name}（{found.value}），这里需要 {kind.name}（{kind.value}）"
        )
    return payload[HEADER_BYTES:]


def peek_kind(payload: bytes) -> Kind:
    """The envelope's kind, without checking the rest. Raises if not PBX1."""
    if len(payload) < HEADER_BYTES or payload[: len(MAGIC)] != MAGIC:
        raise NoHiddenFrame("不是 PBX1 信封")
    return Kind.of(payload[len(MAGIC): HEADER_BYTES])


# ------------------------------------------------------- frame round-trip glue
#
# The terminal always speaks "envelope in, frame out" and vice versa. Doing the
# wrap/unwrap here (rather than in the transport or the UI) means there is one
# place that knows the MKX1 frame sits *outside* the PBX1 envelope.


def wrap(envelope: bytes) -> bytes:
    """PBX1 envelope -> MKX1 frame payload for a stego carrier."""
    return build_frame(envelope)


def unwrap(frame_payload: bytes) -> bytes:
    """MKX1 frame payload -> PBX1 envelope."""
    return parse_frame(frame_payload)


__all__ = [
    "MAGIC", "HEADER_BYTES", "PUBLIC_KEY_BYTES", "NONCE_BYTES",
    "Kind", "Mode",
    "build_handshake", "parse_handshake",
    "seal_body", "open_body", "peek_kind",
    "wrap", "unwrap",
]
