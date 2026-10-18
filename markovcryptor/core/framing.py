"""MKX1 frame: the small header every image/GIF carrier starts with.

The frame is the contract between embedding and blind extraction, so it lives in
``core`` rather than in the image channel: the GIF channel reuses it verbatim,
and a future video channel will too.  The byte layout is frozen — carriers
written by an older version must stay readable.

Layout (standard profile, 24 bytes of lead)::

    MAGIC(4) profile(1) nonce(8|4) nblocks(2) length(2) CRC16(2) padding
    └──────────────────── whitened after the nonce ────────────────────┘

Two profiles exist because the lead is a *fixed* cost paid in 8x8 blocks, not a
proportional one: a 44-byte lead costs 88 blocks, which is 45% of a 120x120 cover
but 0.03% of a 4096x4096 one.  Small covers use the compact profile; large ones
spend their spare blocks on a roomier header.  See ``docs/IMAGE_STEGO.md``.
"""

from __future__ import annotations

import hashlib
import secrets
import struct
from typing import NamedTuple

from .errors import CarrierDamaged, NoHiddenFrame

MAGIC = b"MKX1"
CRC_BYTES = 2
#: Frames per profile carry a 2-byte block count, so this is the width, not a limit.
NBLOCK_BYTES = 2
#: 4-bit lanes: every 8x8 block carries one lane group.
LANES = 4
#: RS parity for body codewords (corrects 8 symbol errors per block).
RS_BLOCK_NSYM = 16
#: Largest body data chunk a single RS codeword may hold (255-symbol limit).
RS_BLOCK_DATA = 207

#: Hard cap on how many body codewords we will ever emit.
MAX_RS_BLOCKS = 512

WHITEN_DOMAIN = b"mkx1-whiten\x00"


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE. Detects header corruption before anything is trusted."""
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ 0x1021) & 0xFFFF if value & 0x8000 else (value << 1) & 0xFFFF
    return value


def whiten(data: bytes, nonce: bytes, skip: int = 0) -> bytes:
    """XOR ``data`` with a SHAKE keystream; the first ``skip`` bytes stay clear.

    The nonce sits inside the frame unwhitened (the receiver needs it to find the
    keystream); everything after it is masked.  Whitening keeps a payload of
    zero bytes from producing a visibly uniform run of coefficients.
    """
    stream = hashlib.shake_256(WHITEN_DOMAIN + nonce).digest(len(data))
    out = bytearray(data)
    for i in range(skip, len(data)):
        out[i] ^= stream[i]
    return bytes(out)


def rs_block_count(payload_bytes: int) -> int:
    """How many RS codewords the payload will be split over."""
    return max(1, -(-payload_bytes // RS_BLOCK_DATA))


class LeadProfile(NamedTuple):
    """One lead-header shape: metadata size and RS parity over it."""

    index: int          # stored in the header so readers can tell profiles apart
    nonce_bytes: int
    nsym: int           # RS parity bytes over the lead

    @property
    def meta_bytes(self) -> int:
        """Clear-text metadata length: magic, profile, nonce, block count, length."""
        return len(MAGIC) + 1 + self.nonce_bytes + NBLOCK_BYTES + 2

    @property
    def lead_bytes(self) -> int:
        """Metadata plus CRC, rounded up to a whole 8-byte group.

        The 8-byte multiple is not cosmetic: each block carries 4 bits, so a lead
        that is not a whole number of bytes would misalign every later block and
        become unrecoverable.
        """
        raw = self.meta_bytes + CRC_BYTES
        return -(-raw // 8) * 8

    @property
    def code_bytes(self) -> int:
        return self.lead_bytes + self.nsym

    @property
    def blocks(self) -> int:
        """8x8 blocks this lead costs, at 4 bits per block."""
        return self.code_bytes * 8 // LANES

    def describe(self) -> str:
        return (f"{self.code_bytes}B 码字 / {self.blocks} 块 / "
                f"nonce {self.nonce_bytes}B / RS({self.code_bytes},{self.lead_bytes})")


#: Cheapest header we still trust for handshake-sized payloads: RS corrects
#: ``nsym // 2`` lead-symbol errors, and the lead is the one part whose failure
#: is unrecoverable.
LEAD_COMPACT = LeadProfile(index=2, nonce_bytes=4, nsym=6)
#: Roomier header, used only when its block cost is noise on this cover.
LEAD_STANDARD = LeadProfile(index=1, nonce_bytes=8, nsym=20)

LEAD_PROFILES: dict[int, LeadProfile] = {p.index: p for p in (LEAD_STANDARD, LEAD_COMPACT)}

#: Upper bound on how much of a cover may be read: the largest possible lead
#: plus the largest body we ever emit.  Guards against absurd block scans when a
#: corrupted header claims a huge payload.
MAX_FRAME_BITS = (max(p.code_bytes for p in LEAD_PROFILES.values())
                  + MAX_RS_BLOCKS * (RS_BLOCK_DATA + RS_BLOCK_NSYM)) * 8

#: Below this share of the block budget we spend blocks on a roomier lead;
#: above it every saved block is worth more as payload.
LEAD_ROOMY_SHARE = 0.02


def rs_plan(payload_bytes: int) -> tuple[int, int]:
    """Split the payload into balanced RS blocks -> ``(total_data, total_code)``.

    Only the *body* is planned here; the lead is its own short codeword, added
    by :func:`blocks_needed`.  RS works on whole codewords, so the payload is
    padded up to an even per-block chunk.
    """
    blocks = rs_block_count(payload_bytes)
    per_block = -(-payload_bytes // blocks)      # balanced split, whole bytes
    return blocks * per_block, blocks * (per_block + RS_BLOCK_NSYM)


def lead_profile_for(blocks_available: int, payload_bytes: int) -> LeadProfile:
    """Pick the lead for this cover size and payload."""
    if blocks_available <= 0:
        return LEAD_COMPACT
    if LEAD_STANDARD.blocks / blocks_available > LEAD_ROOMY_SHARE:
        return LEAD_COMPACT
    body_bits = rs_plan(payload_bytes)[1] * 8
    if LEAD_STANDARD.blocks + -(-body_bits // LANES) <= blocks_available:
        return LEAD_STANDARD
    return LEAD_COMPACT


def header_profile(head: bytes) -> LeadProfile | None:
    """Profile named by the first bytes of a frame, or ``None`` if unrecognised."""
    if len(head) <= len(MAGIC) or head[: len(MAGIC)] != MAGIC:
        return None
    return LEAD_PROFILES.get(head[len(MAGIC)])


def build_frame(payload: bytes, nonce: bytes | None = None,
                *, profile: LeadProfile | None = None) -> bytes:
    """Wrap ``payload`` into ``lead + body``, both whitened and CRC-checked."""
    profile = profile or LEAD_COMPACT
    if not 0 < len(payload) <= 0xFFFF:
        raise ValueError("载荷长度必须在 1..65535 字节之间")
    nonce = nonce if nonce is not None else secrets.token_bytes(profile.nonce_bytes)
    if len(nonce) != profile.nonce_bytes:
        raise ValueError(f"nonce 必须是 {profile.nonce_bytes} 字节")
    meta = (MAGIC + bytes([profile.index]) + nonce
            + struct.pack(">H", rs_block_count(len(payload)))
            + struct.pack(">H", len(payload)))
    lead = meta + struct.pack(">H", crc16(meta))
    lead += bytes(profile.lead_bytes - len(lead))
    lead = whiten(lead, nonce, skip=profile.meta_bytes)
    return lead + whiten(payload, nonce, skip=0)


def parse_frame(frame: bytes, *, profile: LeadProfile | None = None) -> bytes:
    """Recover the payload from ``lead + body``.

    ``profile`` is only needed for the header length; pass it when the caller
    already decoded the lead through RS.  Otherwise it is read from the frame.
    Raises :class:`NoHiddenFrame` or :class:`CarrierDamaged` with the real
    cause rather than a generic message.
    """
    profile = profile or header_profile(frame)
    if profile is None:
        raise NoHiddenFrame("帧标记不匹配")
    if len(frame) < profile.lead_bytes:
        raise CarrierDamaged(f"帧长度 {len(frame)} 过短")
    nonce_at = len(MAGIC) + 1
    nonce = bytes(frame[nonce_at : nonce_at + profile.nonce_bytes])
    lead = whiten(frame[: profile.lead_bytes], nonce, skip=profile.meta_bytes)
    meta = lead[: profile.meta_bytes]
    crc = struct.unpack(">H", lead[profile.meta_bytes : profile.meta_bytes + CRC_BYTES])[0]
    if crc16(meta) != crc:
        raise CarrierDamaged("帧头 CRC 校验失败")
    if meta[: len(MAGIC)] != MAGIC or meta[len(MAGIC)] != profile.index:
        raise NoHiddenFrame("帧标记不匹配")
    length = struct.unpack(">H", meta[profile.meta_bytes - 2 : profile.meta_bytes])[0]
    body = frame[profile.lead_bytes : profile.lead_bytes + length]
    if len(body) != length:
        raise CarrierDamaged(f"帧内长度字段 {length} 与实际载荷 {len(body)} 不符")
    return whiten(body, nonce, skip=0)


def frame_overhead(profile: LeadProfile | None = None) -> int:
    """Fixed bytes a frame spends on its lead plus CRC."""
    profile = profile or LEAD_STANDARD
    return profile.lead_bytes + CRC_BYTES


__all__ = [
    "MAGIC", "CRC_BYTES", "NBLOCK_BYTES", "RS_BLOCK_NSYM", "RS_BLOCK_DATA",
    "MAX_RS_BLOCKS", "LANES", "LEAD_COMPACT", "LEAD_STANDARD", "LEAD_PROFILES",
    "LEAD_ROOMY_SHARE", "LeadProfile", "crc16", "whiten", "rs_block_count", "rs_plan",
    "MAX_FRAME_BITS",
    "lead_profile_for", "header_profile", "build_frame", "parse_frame",
    "frame_overhead",
]
