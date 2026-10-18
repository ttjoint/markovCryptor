"""Reed-Solomon encoding for the MKX1 frame.

Split out of ``image_stego`` because GIF (and later video) reuse it verbatim: the
error-correction layer knows nothing about pixels.

The important structural detail is that the **lead is its own codeword**, always
exactly one block's worth.  A blind reader can therefore decode the lead before
knowing anything about payload size, learn ``nblocks`` and ``length`` from it, and
only then read the body blocks it actually needs.  Filing the lead into the body
codewords would force a full-image scan on every attempt.
"""

from __future__ import annotations

try:
    import reedsolo
except ImportError:  # pragma: no cover - exercised only when the extra is absent
    reedsolo = None  # type: ignore[assignment]

from .errors import CarrierDamaged, ChannelUnavailable
from .framing import RS_BLOCK_DATA, RS_BLOCK_NSYM, LeadProfile


def available() -> bool:
    """Whether the Reed-Solomon dependency is importable."""
    return reedsolo is not None


def _require() -> None:
    """Reed-Solomon is optional at import time so the Chinese channel works
    without it; any image/GIF operation that reaches this point needs it."""
    if reedsolo is None:  # pragma: no cover
        raise ChannelUnavailable("需要 reedsolo：pip install reedsolo")


def encode(frame: bytes, profile: LeadProfile) -> tuple[bytes, int]:
    """RS-encode ``frame`` into ``lead codeword + body codewords``.

    Returns ``(codeword, total_blocks)`` where ``total_blocks`` counts the lead
    as one block.
    """
    _require()
    lead_len = profile.lead_bytes
    lead, body = frame[:lead_len], frame[lead_len:]
    lead_code = reedsolo.RSCodec(profile.nsym, nsize=profile.code_bytes).encode(lead)
    nblocks = max(1, -(-len(body) // RS_BLOCK_DATA))
    per_block = -(-len(body) // nblocks)
    padded = body + bytes(nblocks * per_block - len(body))
    codec = reedsolo.RSCodec(RS_BLOCK_NSYM, nsize=per_block + RS_BLOCK_NSYM)
    out = bytearray(lead_code)
    for i in range(nblocks):
        out += codec.encode(padded[i * per_block : (i + 1) * per_block])
    return bytes(out), nblocks + 1


def decode_lead(codeword: bytes, profile: LeadProfile) -> bytes:
    """Decode the lead codeword that starts every embedded frame.

    Failure here is ambiguous by nature — a wrong key and a damaged carrier look
    identical — so the message names both possibilities instead of guessing.
    """
    _require()
    if len(codeword) < profile.code_bytes:
        raise CarrierDamaged("码字过短，缺少引导头")
    chunk = bytearray(codeword[: profile.code_bytes])
    try:
        return bytes(reedsolo.RSCodec(profile.nsym, nsize=profile.code_bytes).decode(chunk)[0])
    except Exception as exc:
        raise CarrierDamaged(f"引导头解码失败（密钥错误或图像被改动）：{exc}") from exc


def decode_body(codeword: bytes, nblocks: int, size: int) -> bytes:
    """Decode ``nblocks`` equal body codewords of ``size`` bytes each."""
    _require()
    if nblocks * size > len(codeword):
        raise CarrierDamaged("码字长度不足")
    codec = reedsolo.RSCodec(RS_BLOCK_NSYM, nsize=size)
    data = bytearray()
    for i in range(nblocks):
        chunk = bytearray(codeword[i * size : (i + 1) * size])
        try:
            data += bytes(codec.decode(chunk)[0])
        except Exception as exc:
            raise CarrierDamaged(f"Reed-Solomon 解码失败（密钥错误或图像被改动）：{exc}") from exc
    return bytes(data)


__all__ = ["encode", "decode_lead", "decode_body", "available"]
