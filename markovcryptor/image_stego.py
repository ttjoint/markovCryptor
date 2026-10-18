"""Blind frequency-domain image steganography for an X25519 handshake (MKX1).

How it works
------------

* The cover (any RGB image, both sides >= 64 px) is converted to YCbCr and the
  payload is embedded into the **Y (luma) channel** only, so colour never
  shifts visibly.
* The luma plane is cut into 8x8 blocks.  Each block offers four *lanes*: the
  mid-frequency DCT coefficients ``(2,2)``, ``(2,3)``, ``(3,2)``, ``(3,3)``.
* A bit is stored per lane: the lane's value coefficient is quantised
  (QIM, ``bit = round(c/step) & 1``) and the neighbouring lane-sign coefficient
  is pushed to the sign that the key-derived pseudo-random sequence demands.
  The detector re-quantises and reads the sign — **no original image needed**.
* The shared key decides which blocks are used and in which order, plus the
  sign sequence, so an attacker without the key sees nothing structured.
* Reed-Solomon (pure Python ``reedsolo``) protects the whole frame: a damaged
  block costs at most 4 consecutive bits, well inside the correction budget.

Frame layout (whitened with a keystream derived from magic + nonce)::

    magic[4] | version[1] | nonce[8] | payload[32] | crc16[2]   = 55 bytes

All parameters are determined by ``(key, cover width)``, so the receiver
recovers everything from the received image alone.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
import struct

from PIL import Image
import numpy as np

try:
    import reedsolo
except ImportError:  # pragma: no cover - reported when used
    reedsolo = None  # type: ignore[assignment]

BLOCK = 8
LANES = 4
MAGIC = b"MKX1"
VERSION = 1
NONCE_BYTES = 8
PUBLIC_KEY_BYTES = 32
CRC_BYTES = 2
FRAME_BYTES = len(MAGIC) + 1 + NONCE_BYTES + PUBLIC_KEY_BYTES + CRC_BYTES  # 55
FRAME_BITS = FRAME_BYTES * 8

RS_NSIZE = 71  # one RS block: 55 data + 16 parity -> corrects up to 8 symbol errors
RS_NSYM = RS_NSIZE - FRAME_BYTES
FRAME_CODE_BITS = RS_NSIZE * 8

# (value, sign) coefficient pair per lane inside one 8x8 block.
# Value slots sit on a fixed mid-frequency ring; sign slots on a higher ring so
# that no coefficient is ever used as both a value and a sign carrier.
LANE_VALUE: tuple[tuple[int, int], ...] = ((2, 2), (2, 3), (3, 2), (3, 3))
LANE_SIGN: tuple[tuple[int, int], ...] = ((1, 5), (5, 1), (4, 4), (0, 6))

MIN_SIDE = 40
DEFAULT_STEP = 10.0
# Payloads are split over RS(223,207) blocks (the largest standard codeword),
# so a big cover can carry far more than one 32-byte handshake key.
RS_BLOCK_DATA = 207
RS_BLOCK_NSYM = 16      # corrects 8 symbol errors per block
# Smallest short side accepted without upscaling; below this we enlarge.
SMALLEST_SIDE = 96
MAX_UPSCALE = 64
MAX_RS_BLOCKS = 512
MAX_FRAME_BITS = MAX_RS_BLOCKS * (RS_BLOCK_DATA + RS_BLOCK_NSYM) * 8
STEP_CHOICES = (8.0, 9.0, 10.0, 11.0, 12.0, 14.0)


class StegoError(Exception):
    """Raised when an image carries no readable MKX1 frame."""


# --------------------------------------------------------------- key material


def normalise_key(key: str | bytes) -> bytes:
    raw = key.encode("utf-8") if isinstance(key, str) else key
    if not raw:
        raise ValueError("共享密钥不能为空")
    return hashlib.sha256(b"mkx1-key\x00" + raw).digest()


def derive(key: bytes, label: str, length: int = 32) -> bytes:
    return hashlib.shake_256(b"mkx1-dom\x00" + label.encode() + b"\x00" + key).digest(length)


def _rng(*parts: bytes) -> np.random.Generator:
    seed = int.from_bytes(hashlib.sha256(b"".join(parts)).digest(), "big")
    return np.random.Generator(np.random.PCG64(seed))


# ------------------------------------------------------------------- planning


def blocks_per_row(width: int) -> int:
    return width // BLOCK


def blocks_total(width: int, height: int) -> int:
    """Every 8x8 block in the image (all rows, ignoring the right/bottom offcuts)."""
    return (width // BLOCK) * (height // BLOCK)


def rs_plan(payload_bytes: int) -> tuple[int, int]:
    """Split ``frame`` into RS blocks -> ``(total_data_bytes, total_codeword_bytes)``.

    RS works on whole 255-byte codewords, so the frame is padded up to an even
    per-block chunk.  The return value's second element is what the channel
    must physically carry.
    """
    raw = payload_bytes + frame_overhead()
    blocks = max(1, -(-raw // RS_BLOCK_DATA))
    per_block = -(-raw // blocks)            # balanced split, whole bytes
    total_data = blocks * per_block
    total_code = blocks * (per_block + RS_BLOCK_NSYM)
    return total_data, total_code


def bits_per_block() -> int:
    return LANES


def blocks_needed(payload_bytes: int) -> int:
    """8x8 blocks required to carry ``payload_bytes``."""
    _data, total = rs_plan(payload_bytes)
    return -(-(total * 8) // LANES)


def max_payload_bytes(width: int, height: int) -> int:
    """Largest payload this image can carry.

    Every 8x8 block contributes ``LANES`` bits.  A frame of ``n`` bytes costs
    ``ceil((n+overhead)/207)`` RS blocks of ``chunk+16`` bytes each, so we walk
    the block count upward until the bit budget is exhausted.
    """
    budget_bits = blocks_total(width, height) * LANES
    best = 0
    for nblocks in range(1, MAX_RS_BLOCKS + 1):
        chunk = RS_BLOCK_DATA
        cost_bits = nblocks * (chunk + RS_BLOCK_NSYM) * 8
        if cost_bits > budget_bits:
            # Try the largest chunk that still fits this block count.
            room = budget_bits // (nblocks * 8) - RS_BLOCK_NSYM
            if room <= 0:
                break
            chunk = min(chunk, room)
        payload = nblocks * chunk - frame_overhead()
        if payload > best:
            best = payload
        if chunk < RS_BLOCK_DATA:
            break
    return best


def min_height(width: int, payload_bytes: int = PUBLIC_KEY_BYTES) -> int:
    """Smallest usable height for this width and payload."""
    rows = -(-blocks_needed(payload_bytes) // max(1, blocks_per_row(width)))
    return rows * BLOCK


def check_cover(image: Image.Image, payload_bytes: int = PUBLIC_KEY_BYTES) -> tuple[bool, str]:
    """Return ``(usable, reason)`` without touching the pixels."""
    width, height = image.size
    if min(width, height) < MIN_SIDE // 2:
        return False, f"两边都至少需要 {MIN_SIDE // 2} 像素（当前 {width}×{height}）"
    if blocks_per_row(width) == 0:
        return False, f"宽度 {width} 不足一个 {BLOCK} 像素块"
    have = max_payload_bytes(width, height)
    if have < payload_bytes:
        return False, f"容量不足：最多 {have} 字节，需要 {payload_bytes + frame_overhead()} 字节"
    return True, ""


def channel_bits(payload_bytes: int) -> int:
    """Bits the channel must carry for one payload (lead + body codewords)."""
    nblocks = rs_block_count(payload_bytes)
    chunk = -(-payload_bytes // nblocks)
    return (LEAD_CODE_BYTES + nblocks * (chunk + RS_BLOCK_NSYM)) * 8


def suggest_size(image: Image.Image, payload_bytes: int = PUBLIC_KEY_BYTES) -> tuple[int, int]:
    """Smallest proportional enlargement that lets ``image`` carry a payload."""
    width, height = image.size
    need = -(-channel_bits(payload_bytes) // LANES)
    if blocks_total(width, height) >= need:
        return width, height
    scale = 1
    while scale < MAX_UPSCALE:
        size = (width * scale, height * scale)
        if blocks_total(*size) >= need:
            return size
        scale += 1
    return width * scale, height * scale


def upscale_cover(image: Image.Image, payload_bytes: int = PUBLIC_KEY_BYTES
                  ) -> tuple[Image.Image, tuple[int, int], bool]:
    """Proportionally enlarge small covers just enough to be usable.

    Returns ``(image, original_size, resized)``.  LANCZOS keeps the picture
    sharp and, more importantly, preserves its low-frequency DCT content so the
    QIM carriers stay well-behaved.
    """
    original = image.size
    target = suggest_size(image, payload_bytes)
    if target == original:
        return image, original, False
    return image.resize(target, Image.LANCZOS), original, True


def plan(width: int, height: int, payload_bytes: int = PUBLIC_KEY_BYTES,
         step: float = DEFAULT_STEP) -> dict:
    """Capacity report for one cover and one payload size."""
    need = blocks_needed(payload_bytes)
    have = blocks_total(width, height)
    data, total = rs_plan(payload_bytes)
    return {
        "width": width,
        "height": height,
        "step": step,
        "blocks_available": have,
        "blocks_needed": need,
        "usable": have >= need and min(width, height) >= MIN_SIDE // 2,
        "min_height": min_height(width, payload_bytes),
        "rs_blocks": total // (RS_BLOCK_DATA + RS_BLOCK_NSYM),
        "rs_block": f"RS(223,{RS_BLOCK_DATA})×{total // (RS_BLOCK_DATA + RS_BLOCK_NSYM)}",
        "codeword_bytes": total,
        "max_payload_bytes": max_payload_bytes(width, height),
        "payload_bits": payload_bytes * 8,
    }


def rows_used_for(blocks: int, width: int) -> int:
    """Block-rows the image will touch for ``blocks`` payload blocks."""
    per_row = max(1, blocks_per_row(width))
    return min(width // BLOCK, -(-blocks // per_row))


# ------------------------------------------------------------------- DCT math

_k = np.arange(BLOCK)
_C = np.array(
    [
        [
            (1.0 / np.sqrt(BLOCK))
            if f == 0
            else np.sqrt(2.0 / BLOCK) * np.cos(np.pi * (2 * n + 1) * f / (2 * BLOCK))
            for n in _k
        ]
        for f in _k
    ],
    dtype=np.float64,
)


def _fdct(block: np.ndarray) -> np.ndarray:
    return _C.T @ block @ _C


def _idct(coeffs: np.ndarray) -> np.ndarray:
    return _C @ coeffs @ _C.T


def _blocks_of(plane: np.ndarray) -> np.ndarray:
    h, w = plane.shape
    return (
        plane[: (h // BLOCK) * BLOCK, : (w // BLOCK) * BLOCK]
        .reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK)
        .swapaxes(1, 2)
        .reshape(-1, BLOCK, BLOCK)
    )


def _plane_of(blocks: np.ndarray, width: int, rows: int) -> np.ndarray:
    """Rebuild a cropped plane from blocks (``width``/``BLOCK`` columns)."""
    cols = blocks_per_row(width)
    return (
        blocks[: rows * cols]
        .reshape(rows, cols, BLOCK, BLOCK)
        .swapaxes(1, 2)
        .reshape(rows * BLOCK, cols * BLOCK)
    )


def _block_partner(width: int, height: int, key: bytes, step: float) -> np.ndarray:
    """All blocks of the image in key-derived order (used as the carrier pool)."""
    count = blocks_total(width, height)
    rng = _rng(derive(key, "blocks"), struct.pack(">HH", width, height), struct.pack(">d", float(step)))
    order = np.arange(count)
    rng.shuffle(order)
    return order


# ------------------------------------------------------------------- framing


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ 0x1021) & 0xFFFF if value & 0x8000 else (value << 1) & 0xFFFF
    return value


def _whiten(data: bytes, nonce: bytes, skip: int = 0) -> bytes:
    """XOR ``data`` with a SHAKE keystream; the first ``skip`` bytes stay clear.

    The nonce itself sits inside the frame unwhitened so the receiver can find
    the keystream; everything after it is masked.
    """
    stream = hashlib.shake_256(b"mkx1-whiten\x00" + nonce).digest(len(data))
    out = bytearray(data)
    for i in range(skip, len(data)):
        out[i] ^= stream[i]
    return bytes(out)


NBLOCK_BYTES = 2
META_BYTES = len(MAGIC) + 1 + NONCE_BYTES + NBLOCK_BYTES + 2   # magic, version, nonce, nblocks, length
# The lead occupies exactly one RS codeword.  Its size must be a multiple of 8
# bytes, because every 8x8 block stores 4 bits: 35 bytes would leave a half
# block of slack that nothing could align to.  So we round up to 40.
LEAD_BYTES = 20                                    # meta (17) + CRC16 (2) + pad
LEAD_NSYM = 20
LEAD_CODE_BYTES = LEAD_BYTES + LEAD_NSYM           # 40 bytes = 320 bits = 80 blocks


def build_frame(payload: bytes, nonce: bytes | None = None) -> bytes:
    """Wrap ``payload`` into ``(lead, body)`` -- both whitened and CRC-checked.

    The lead is a fixed-size codeword that the reader can always decode on its
    own, so it advertises the payload length and block count without any
    guessing.  Keeping it a constant size is what makes blind extraction cheap.
    """
    if not 0 < len(payload) <= 0xFFFF:
        raise ValueError("载荷长度必须在 1..65535 字节之间")
    nonce = nonce if nonce is not None else secrets.token_bytes(NONCE_BYTES)
    nblocks = rs_block_count(len(payload))
    meta = (MAGIC + bytes([VERSION]) + nonce
            + struct.pack(">H", nblocks) + struct.pack(">H", len(payload)))
    lead = meta + struct.pack(">H", crc16(meta))
    lead += bytes(LEAD_BYTES - len(lead))
    # The nonce stays in the clear so a blind reader can find the keystream.
    lead = _whiten(lead, nonce, skip=META_BYTES)
    body = _whiten(payload, nonce, skip=0)
    return lead + body


def parse_frame(frame: bytes) -> bytes:
    """Recover the payload given ``lead + body`` (as produced by :func:`build_frame`)."""
    if len(frame) < LEAD_BYTES:
        raise StegoError(f"帧长度 {len(frame)} 过短")
    nonce_start = len(MAGIC) + 1
    nonce = bytes(frame[nonce_start : nonce_start + NONCE_BYTES])
    lead = _whiten(frame[:LEAD_BYTES], nonce, skip=META_BYTES)
    meta = lead[:META_BYTES]
    crc = struct.unpack(">H", lead[META_BYTES : META_BYTES + CRC_BYTES])[0]
    if crc16(meta) != crc:
        raise StegoError("帧头 CRC 校验失败")
    if meta[: len(MAGIC)] != MAGIC or meta[len(MAGIC)] != VERSION:
        raise StegoError("帧标记不匹配")
    length_at = nonce_start + NONCE_BYTES + NBLOCK_BYTES
    length = struct.unpack(">H", meta[length_at : length_at + 2])[0]
    _ = length
    body = frame[LEAD_BYTES : LEAD_BYTES + length]
    if len(body) != length:
        raise StegoError(f"帧内长度字段 {length} 与实际载荷 {len(body)} 不符")
    return _whiten(body, nonce, skip=0)


def frame_overhead() -> int:
    """Fixed bytes a frame spends on its lead plus CRC."""
    return LEAD_BYTES + CRC_BYTES


def lead_block_count(frame: bytes) -> int:
    """Block count declared in a frame's lead."""
    return frame[len(MAGIC) + 1 + NONCE_BYTES]


def rs_block_count(payload_bytes: int) -> int:
    """How many RS codewords the payload will be split over."""
    return max(1, -(-payload_bytes // RS_BLOCK_DATA))


# ----------------------------------------------------------------- embedding


def _bits_lsb(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="little")


def _pack_lsb(bits: np.ndarray) -> bytes:
    return np.packbits(np.asarray(bits, dtype=np.uint8), bitorder="little").tobytes()


def _write_lane(coeffs: np.ndarray, lane: int, bit: int, step: float) -> None:
    """QIM with *centred* lattice points.

    The coefficient is snapped to the lattice ``(k + 0.5) * step`` where ``k``
    has the parity of the bit.  Snapping to the cell *centre* (instead of the
    cell boundary, as plain rounding would) leaves half a step of headroom on
    both sides, so the ±1 luma perturbation introduced by the RGB round trip
    can no longer flip a decision.
    """
    u, v = LANE_VALUE[lane]
    # Target: (k + 0.5) * step with k odd for bit 1, even for bit 0.
    k = int(np.floor(coeffs[u, v] / step - 0.5))
    if (k & 1) != int(bit):
        # Pick the neighbouring k with the wanted parity (nearest centre).
        k += 1 if (coeffs[u, v] / step - 0.5 - k) >= 0 else -1
    coeffs[u, v] = (k + 0.5) * step


def _read_lane(coeffs: np.ndarray, lane: int, step: float) -> tuple[float, float]:
    """Return ``(soft evidence, confidence)``; evidence > 0 means bit 0.

    Evidence = parity of the nearest centred lattice index, weighted by how
    close the coefficient sits to that centre (a coefficient exactly halfway
    between two centres carries no information and gets zero weight).
    """
    u, v = LANE_VALUE[lane]
    shifted = coeffs[u, v] / step - 0.5
    k = np.rint(shifted)
    parity = -1.0 if int(k) & 1 else 1.0   # odd -> bit 1 -> negative evidence
    distance = abs(shifted - k)            # 0 at the centre, 0.5 at the boundary
    weight = max(0.0, 1.0 - 2.0 * distance)
    return parity, weight


# Channel order of the YCbCr planes we may use as carrier.  Y is preferred
# (invisible to the eye), Cb/Cr are the fallbacks for extreme colours such as
# pure saturated red, where Y is clipped against 0/255 and cannot be written.
CARRIER_ORDER = (0, 1, 2)
CARRIER_NAMES = ("Y", "Cb", "Cr")


def _carriers(width: int, key: bytes, step: float) -> tuple[tuple[int, ...], tuple[float, ...]]:
    """Key-derived channel order and per-channel QIM step."""
    rng = _rng(derive(key, "carriers"), struct.pack(">HH", width, RS_NSIZE),
               struct.pack(">d", float(step)))
    order = tuple(int(v) for v in rng.permutation(len(CARRIER_ORDER)))
    # ``steps`` is indexed by *plane number* (0=Y, 1=Cb, 2=Cr) so that writers
    # and readers agree regardless of the shuffled ``order``.
    drawn = rng.uniform(0.9, 1.1, len(CARRIER_ORDER)) * step
    steps = [0.0] * len(CARRIER_ORDER)
    for slot, plane in enumerate(order):
        steps[plane] = float(drawn[slot])
    return order, tuple(steps)


def _carrier_scores(ycbcr: np.ndarray) -> list[float]:
    """Per-channel viability in ``0..1`` for the Y, Cb, Cr planes.

    A channel is a poor carrier when the colour conversion clips it (saturated
    covers) or when it carries almost no variation to hide data in (flat
    covers).  The score blends "fraction of pixels stable across the RGB round
    trip" with the channel's dynamic range.
    """
    quant = np.clip(np.rint(ycbcr), 0, 255)
    img = Image.fromarray(quant.astype(np.uint8), "YCbCr")
    back = np.asarray(img.convert("RGB").convert("YCbCr"), dtype=np.float64)
    scores: list[float] = []
    for channel in range(3):
        plane = quant[:, :, channel]
        stable = float(np.mean(np.abs(back[:, :, channel] - plane) < 2.0))
        spread = min(1.0, float(np.std(plane)) / 48.0)
        # Stability dominates: a channel that clips is useless no matter how
        # much contrast it appears to have.
        scores.append(stable * (0.35 + 0.65 * spread))
    return scores


def _pick_carrier(ycbcr: np.ndarray, channels: tuple[int, ...]) -> tuple[int, float]:
    """Prefer Y, but fall back to Cb/Cr when Y clips (e.g. pure red).

    ``channels`` is the key-derived tie-break order; a channel is only chosen
    over Y when it is clearly more stable.
    """
    scores = _carrier_scores(ycbcr)
    best = channels[0]
    for channel in channels:
        threshold = 0.02 if best == 0 else 0.0
        if scores[channel] > scores[best] + threshold:
            best = channel
    return best, scores[best]


def _encode_rs(frame: bytes) -> tuple[bytes, int]:
    """RS-encode a frame into ``lead codeword + body codewords``.

    The lead is always exactly one ``LEAD_BYTES`` codeword, so the reader can
    decode it without knowing anything about the payload.
    """
    lead, body = frame[:LEAD_BYTES], frame[LEAD_BYTES:]
    lead_code = reedsolo.RSCodec(LEAD_NSYM, nsize=LEAD_BYTES + LEAD_NSYM).encode(lead)
    nblocks = max(1, -(-len(body) // RS_BLOCK_DATA))
    per_block = -(-len(body) // nblocks)
    padded = body + bytes(nblocks * per_block - len(body))
    codec = reedsolo.RSCodec(RS_BLOCK_NSYM, nsize=per_block + RS_BLOCK_NSYM)
    out = bytearray(lead_code)
    for i in range(nblocks):
        out += codec.encode(padded[i * per_block : (i + 1) * per_block])
    return bytes(out), nblocks + 1


def _decode_lead(codeword: bytes) -> bytes:
    """Decode the fixed-size lead codeword that starts every embedded frame."""
    if len(codeword) < LEAD_CODE_BYTES:
        raise StegoError("码字过短，缺少引导头")
    chunk = bytearray(codeword[:LEAD_CODE_BYTES])
    try:
        return bytes(reedsolo.RSCodec(LEAD_NSYM, nsize=LEAD_CODE_BYTES).decode(chunk)[0])
    except Exception as exc:
        raise StegoError(f"引导头解码失败（密钥错误或图像被改动）：{exc}") from exc


def _decode_body(codeword: bytes, nblocks: int, size: int) -> bytes:
    """Decode ``nblocks`` equal body codewords of ``size`` bytes each."""
    if nblocks * size > len(codeword):
        raise StegoError("码字长度不足")
    codec = reedsolo.RSCodec(RS_BLOCK_NSYM, nsize=size)
    data = bytearray()
    for i in range(nblocks):
        chunk = bytearray(codeword[i * size : (i + 1) * size])
        try:
            data += bytes(codec.decode(chunk)[0])
        except Exception as exc:
            raise StegoError(f"Reed-Solomon 解码失败（密钥错误或图像被改动）：{exc}") from exc
    return bytes(data)


def _embed_at_step(cover: Image.Image, frame: bytes, payload: bytes, key: bytes,
                   step: float) -> tuple[Image.Image, dict]:
    """One embedding attempt at a fixed QIM step, with channel fallback."""
    width, height = cover.size
    codeword, nblocks = _encode_rs(frame)
    bits = _bits_lsb(codeword)
    order = _block_partner(width, height, key, step)
    needed = -(-len(bits) // LANES)
    if needed > len(order):
        raise StegoError(f"底图容量不足：需要 {needed} 块，此图仅有 {len(order)} 块")

    ycbcr = np.asarray(cover.convert("YCbCr"), dtype=np.float64)
    channels, steps = _carriers(width, key, step)
    best_plane, score = _pick_carrier(ycbcr, channels)
    choose = channels.index(best_plane)

    worst = 0.0
    plane_index = 0
    for attempt in range(len(channels)):
        plane_index = channels[(choose + attempt) % len(channels)]
        plane = ycbcr[:, :, plane_index]
        blocks = _blocks_of(plane).copy()
        worst = 0.0
        for position in range(needed):
            block_id = int(order[position])
            coeffs = _fdct(blocks[block_id])
            for lane in range(LANES):
                index = position * LANES + lane
                if index < len(bits):
                    _write_lane(coeffs, lane, int(bits[index]), steps[plane_index])
            new_block = _idct(coeffs)
            worst = max(worst, float(np.max(np.abs(new_block - blocks[block_id]))))
            blocks[block_id] = new_block
        # Write back *every* block: the shuffled order scatters the used blocks
        # over the whole image, so cropping to the first few rows would drop
        # most of the payload.  The right/bottom offcuts keep their pixels.
        full_rows = height // BLOCK
        cols_px = blocks_per_row(width) * BLOCK
        plane2 = plane.copy()
        plane2[: full_rows * BLOCK, :cols_px] = _plane_of(blocks, width, full_rows)
        ycbcr[:, :, plane_index] = np.clip(np.rint(plane2), 0, 255)
        candidate = Image.fromarray(ycbcr.astype(np.uint8), "YCbCr").convert("RGB")
        try:
            recovered, _ = extract(candidate, key, step=step)
        except StegoError:
            recovered = None
        if recovered == payload:
            return candidate, {
                "width": width,
                "height": height,
                "step": step,
                "carrier": CARRIER_NAMES[plane_index],
                "blocks_needed": needed,
                "blocks_reserved": len(order),
                "rs_blocks": nblocks + 1,
                "payload_bytes": len(payload),
                "codeword_bytes": len(codeword),
                "ecc_bytes": len(codeword) - len(frame),
                "max_block_change": worst,
                "carrier_score": score,
            }
        ycbcr[:, :, plane_index] = plane
    raise StegoError(f"通道无法承载（step={step}）")


def embed(image: Image.Image, frame: bytes, key: bytes, *, step: float = DEFAULT_STEP) -> tuple[Image.Image, dict]:
    """Embed ``frame`` into ``image``, widening the QIM step if needed.

    Flat, saturated covers clamp the carrier coefficient and can only take a
    coarse lattice, so we retry with a larger step before giving up.  A larger
    step trades invisibility for robustness; callers that care can pass a fixed
    ``step`` through ``steps=(step,)``.
    """
    if reedsolo is None:
        raise RuntimeError("需要 reedsolo：pip install reedsolo")
    cover = image.convert("RGB")
    payload = parse_frame(frame)
    usable, reason = check_cover(cover, len(payload))
    if not usable:
        raise StegoError(f"底图不可用：{reason}")
    errors: list[str] = []
    for candidate_step in (step, *[s for s in STEP_CHOICES if s > step]):
        try:
            return _embed_at_step(cover, frame, payload, key, candidate_step)
        except StegoError as exc:
            errors.append(str(exc))
    raise StegoError("底图颜色极端（如纯饱和色），所有通道都无法稳定承载数据，请换一张图")


# ------------------------------------------------------------------ detection


def _extract_from_plane(img_rgb: Image.Image, key: bytes, step: float,
                        plane_index: int, plane_qim_step: float) -> tuple[bytes, dict]:
    """Read as many bits as the codeword needs, then hand them to RS.

    The number of codeword bytes is itself unknown up front, so we decode
    greedily: read every available block, then try progressively shorter RS
    block layouts until the CRC validates.
    """
    ycbcr = np.asarray(img_rgb.convert("YCbCr"), dtype=np.float64)
    width, height = img_rgb.size
    plane = ycbcr[:, :, plane_index]
    blocks = _blocks_of(plane)
    order = _block_partner(width, height, key, step)
    capacity_bits = min(len(order) * LANES, MAX_FRAME_BITS)

    soft = np.zeros(capacity_bits, dtype=np.float64)
    confidence = np.zeros(capacity_bits, dtype=np.float64)
    count = (capacity_bits + LANES - 1) // LANES
    for position in range(count):
        coeffs = _fdct(blocks[int(order[position])])
        for lane in range(LANES):
            index = position * LANES + lane
            if index >= capacity_bits:
                break
            value, weight = _read_lane(coeffs, lane, plane_qim_step)
            soft[index] = value
            confidence[index] = weight
    all_bits = (soft < 0).astype(np.uint8)
    all_bytes = _pack_lsb(all_bits)

    # The lead is a fixed-size codeword at the very start, so the payload
    # length and block count are known before any guessing happens.
    lead_plain = _decode_lead(all_bytes)
    blocks_at = len(MAGIC) + 1 + NONCE_BYTES
    length_at = blocks_at + NBLOCK_BYTES
    nblocks = struct.unpack(">H", lead_plain[blocks_at:length_at])[0]
    length = struct.unpack(">H", lead_plain[length_at:length_at + 2])[0]
    if not 0 < length <= len(all_bytes) or not 0 < nblocks <= MAX_RS_BLOCKS:
        raise StegoError("帧头内容不合理")
    body_code = all_bytes[LEAD_CODE_BYTES:]
    size = min(RS_BLOCK_DATA + RS_BLOCK_NSYM, -(-length // nblocks) + RS_BLOCK_NSYM)
    body = _decode_body(body_code, nblocks, size)[:length]
    payload = parse_frame(lead_plain + body)
    total = LEAD_CODE_BYTES + nblocks * size
    return payload, {
        "width": width,
        "height": height,
        "step": step,
        "carrier": CARRIER_NAMES[plane_index],
        "rs_blocks": nblocks + 1,
        "codeword_bytes": total,
        "payload_bytes": len(payload),
        "mean_confidence": float(np.mean(confidence[: total * 8])),
        "min_confidence": float(np.min(confidence[: total * 8])),
    }


def extract(image: Image.Image, key: bytes, *, step: float = DEFAULT_STEP) -> tuple[bytes, dict]:
    """Blindly recover the payload, trying each carrier channel in turn."""
    if reedsolo is None:
        raise RuntimeError("需要 reedsolo：pip install reedsolo")
    img = image.convert("RGB")
    width, height = img.size
    if min(width, height) < MIN_SIDE // 2 or blocks_per_row(width) == 0:
        raise StegoError(f"图像太小，不可能携带一帧（{width}×{height}）")
    channels, steps = _carriers(width, key, step)
    ycbcr = np.asarray(img.convert("YCbCr"), dtype=np.float64)
    best_plane, _score = _pick_carrier(ycbcr, channels)
    choose = channels.index(best_plane)
    errors: list[str] = []
    for attempt in range(len(channels)):
        plane_index = channels[(choose + attempt) % len(channels)]
        try:
            return _extract_from_plane(img, key, step, plane_index, steps[plane_index])
        except StegoError as exc:
            errors.append(f"{CARRIER_NAMES[plane_index]}: {exc}")
    raise StegoError(errors[0] if errors else "没有可识别的隐写帧")


def try_extract(image: Image.Image, key: bytes, steps: tuple[float, ...] = STEP_CHOICES) -> tuple[bytes, dict]:
    """Try every plausible QIM step; return the first frame that verifies."""
    errors: list[str] = []
    for step in steps:
        try:
            return extract(image, key, step=step)
        except StegoError as exc:
            errors.append(f"step={step}: {exc}")
    raise StegoError("；".join(errors[-2:]) if errors else "没有可识别的隐写帧")
