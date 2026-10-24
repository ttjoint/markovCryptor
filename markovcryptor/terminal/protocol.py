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
#: 握手帧里 MAC 的长度。
HANDSHAKE_MAC_BYTES = 16
NONCE_BYTES = 12


class Kind(str, Enum):
    HANDSHAKE = "H"
    BODY_IMAGE = "B"
    BODY_TEXT = "T"
    #: One slice of a ``BODY_IMAGE`` body that was cut across several pictures.
    #: Carries ``index(1) | count(1) | bytes`` so each picture can say where it
    #: sits in the sequence; the receiver joins them by index and only then
    #: authenticates, because the AEAD tag covers the reassembled whole.
    BODY_SLICE = "S"
    #: 带填充的分片：载荷里有"有效长度(2) + 内容 + 随机填充"。
    #: 独立一个 kind 而不是靠猜：正文首字节完全可能像长度，
    #: 那就是一个偶发且极难复现的解码错误。
    BODY_SLICE_PADDED = "P"

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


def build_handshake(public_key: bytes, mode: Mode, *, mac: bytes = b"") -> bytes:
    """``MAGIC | 'H' | pub | mac | mode``.

    带 MAC 是 54 字节，仍能装进最小的可用封面（120×120）。

    MAC 的作用是**身份绑定**：它覆盖公钥和发送方角色，用共享口令派生。
    X25519 本身没有身份认证，任何能改写消息流的人都能分别和两端握手、全程
    解密再转发 —— 这不是理论风险，是这个协议原本就有的洞。
    """
    if len(public_key) != PUBLIC_KEY_BYTES:
        raise InvalidInput(
            f"X25519 公钥必须是 {PUBLIC_KEY_BYTES} 字节，收到 {len(public_key)}"
        )
    if mac and len(mac) != HANDSHAKE_MAC_BYTES:
        raise InvalidInput(
            f"握手 MAC 必须是 {HANDSHAKE_MAC_BYTES} 字节，收到 {len(mac)}")
    return (MAGIC + Kind.HANDSHAKE.value.encode() + public_key + mac
            + mode.value.encode())


def parse_handshake(payload: bytes) -> tuple[bytes, bytes, Mode]:
    """Return ``(peer_public, mac, mode)``; raises if this is not a handshake.

    ``mac`` is empty for old-style 38-byte frames.  Callers that care about
    authentication must treat an empty MAC as untrusted -- see
    ``Terminal._on_handshake``.
    """
    body = _body(payload, Kind.HANDSHAKE)
    if len(body) == PUBLIC_KEY_BYTES + 1:
        return body[:PUBLIC_KEY_BYTES], b"", Mode.of(body[PUBLIC_KEY_BYTES:])
    if len(body) == PUBLIC_KEY_BYTES + HANDSHAKE_MAC_BYTES + 1:
        end = PUBLIC_KEY_BYTES + HANDSHAKE_MAC_BYTES
        return body[:PUBLIC_KEY_BYTES], body[PUBLIC_KEY_BYTES:end], Mode.of(body[end:])
    raise NoHiddenFrame(
        f"握手报文长度异常：{len(body)} 字节，"
        f"应为 {PUBLIC_KEY_BYTES + 1} 或 {PUBLIC_KEY_BYTES + HANDSHAKE_MAC_BYTES + 1}"
    )


# ----------------------------------------------------------------------- body


def _aad(kind: Kind) -> bytes:
    # Binding the tag into the AEAD is what stops a body message from being
    # relabelled as text (or vice versa) without breaking authentication.
    return MAGIC + kind.value.encode()


def seal_body(kind: Kind, key: bytes, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` into a body envelope for ``kind``."""
    if kind in (Kind.HANDSHAKE, Kind.BODY_SLICE, Kind.BODY_SLICE_PADDED):
        raise InvalidInput(f"{kind.name} 不是密文类型；切片请用 build_slice")
    nonce, ciphertext = crypto.seal(key, plaintext, _aad(kind))
    return MAGIC + kind.value.encode() + nonce + ciphertext


def open_body(payload: bytes, kind: Kind, key: bytes) -> bytes:
    """Decrypt a body envelope.

    Raises :class:`NoHiddenFrame` if the envelope is not the kind we expected
    (that is a structural fact, not a key problem), and :class:`BadKey` if the
    tag fails. The caller *must* treat ``BadKey`` as "no hidden message here"
    rather than surfacing it -- see ``docs/TERMINAL.md`` §5.
    """
    if kind in (Kind.HANDSHAKE, Kind.BODY_SLICE):
        raise InvalidInput(f"{kind.name} 不是密文类型；切片请用 parse_slice")
    body = _body(payload, kind)
    if len(body) < NONCE_BYTES:
        raise NoHiddenFrame(f"正文报文过短：{len(body)} 字节，连 nonce 都放不下")
    return crypto.open_(key, body[:NONCE_BYTES], body[NONCE_BYTES:], _aad(kind))


# ------------------------------------------------------------------- slices
#
# A body too big for one picture is cut into consecutive slices, one per
# picture. Each slice travels in its own PBX1 envelope so every picture on the
# wire contains a *valid* envelope -- otherwise a slice would look like a
# picture with no frame in it, which is the one thing an observer can pick out.

SLICE_HEADER_BYTES = 2      # index(1) | count(1)


def build_slice(index: int, count: int, chunk: bytes,
                real_length: int | None = None) -> bytes:
    """Wrap one piece of a split body.

    ``real_length`` 是 ``chunk`` 里**真正有用的字节数**，允许后面跟填充。

    为什么要填充：不补的话，每张图的载荷长度和它承载的明文份额成正比 ——
    一张只装 12 字节的图，和一张装 400 字节的图，从"能装多少、实际用了
    多少"就看得出来。补满之后每张图都是满载，观察者只能知道"发了几张
    图"（图片数量本来就泄露了信息量），单张长度不再泄露第二份。
    """
    if not 0 <= index < count <= 255:
        raise InvalidInput(f"分片序号越界：{index}/{count}")
    if not chunk:
        raise InvalidInput("分片不能为空：没有载荷的图片会被一眼挑出")
    length = len(chunk) if real_length is None else int(real_length)
    if not 0 < length <= len(chunk):
        raise InvalidInput(f"分片有效长度非法：{length}/{len(chunk)}")
    head = MAGIC + Kind.BODY_SLICE.value.encode() + bytes((index, count))
    # **被填充过就换一个 kind**，不靠猜测区分。
    #
    # 试过"看前 2 字节像不像长度"，但正文首字节完全可能落在合法范围，
    # 那就是一个会偶发、极难复现的解码错误。显式标记没有这个风险。
    if length == len(chunk):
        return head + chunk
    head = MAGIC + Kind.BODY_SLICE_PADDED.value.encode() + bytes((index, count))
    return head + length.to_bytes(2, "big") + chunk


def parse_slice(payload: bytes) -> tuple[int, int, bytes]:
    """Return ``(index, count, chunk)``. Raises if it is not a slice envelope."""
    padded = payload[len(MAGIC):len(MAGIC) + 1] == Kind.BODY_SLICE_PADDED.value.encode()
    body = _body(payload, Kind.BODY_SLICE_PADDED if padded else Kind.BODY_SLICE)
    if len(body) < SLICE_HEADER_BYTES:
        raise NoHiddenFrame(f"分片报文过短：{len(body)} 字节，装不下序号")
    index, count = body[0], body[1]
    if index >= count or count == 0:
        raise NoHiddenFrame(f"分片序号非法：{index}/{count}")
    rest = body[SLICE_HEADER_BYTES:]
    if padded:
        if len(rest) < 2:
            raise NoHiddenFrame("分片有效长度缺失")
        declared = int.from_bytes(rest[:2], "big")
        if not 0 < declared <= len(rest) - 2:
            raise NoHiddenFrame(f"分片有效长度非法：{declared}/{len(rest) - 2}")
        return index, count, rest[2:2 + declared]
    return index, count, rest


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
    "SLICE_HEADER_BYTES",
    "Kind", "Mode",
    "build_handshake", "parse_handshake",
    "seal_body", "open_body", "peek_kind",
    "build_slice", "parse_slice",
    "wrap", "unwrap",
]
