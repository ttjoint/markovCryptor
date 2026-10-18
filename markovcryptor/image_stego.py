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

The frame format (two lead profiles, RS layout) and the key derivation live in
:mod:`markovcryptor.core` because the GIF channel reuses them unchanged; this
module owns only the pixel-level carrier: block geometry, DCT lanes, channel
scoring and the embed/extract loop.
"""

from __future__ import annotations

import struct

from PIL import Image
import numpy as np

from .core import ecc
from .core.errors import StegoError
from .core.framing import (
    LANES,
    LEAD_COMPACT,
    LEAD_PROFILES,
    LEAD_STANDARD,
    MAGIC,
    MAX_FRAME_BITS,
    MAX_RS_BLOCKS,
    NBLOCK_BYTES,
    RS_BLOCK_DATA,
    RS_BLOCK_NSYM,
    LeadProfile,
    build_frame,
    lead_profile_for,
    parse_frame,
    rs_plan,
)
# ``normalise_key``/``derive`` stay re-exported: callers reach for them via
# ``image_stego.normalise_key`` and the GIF/handshake layers use the same ones.
from .core.keys import derive, normalise_key, rng

BLOCK = 8
#: Size of a handshake's public key, and the default payload size everywhere
#: (``check_cover``, ``plan``, ``upscale_cover``).
PUBLIC_KEY_BYTES = 32

# (value, sign) coefficient pair per lane inside one 8x8 block.
# Value slots sit on a fixed mid-frequency ring; sign slots on a higher ring so
# that no coefficient is ever used as both a value and a sign carrier.
LANE_VALUE: tuple[tuple[int, int], ...] = ((2, 2), (2, 3), (3, 2), (3, 3))
LANE_SIGN: tuple[tuple[int, int], ...] = ((1, 5), (5, 1), (4, 4), (0, 6))

#: Below this, a cover is rejected outright rather than enlarged.
MIN_SIDE = 40
#: QIM step that trades robustness for capacity; tried first on extraction.
DEFAULT_STEP = 10.0
#: Smallest short side accepted without upscaling.  112 px is the first size
#: that can carry a handshake-sized payload under the compact lead, so anything
#: smaller would be enlarged regardless.
SMALLEST_SIDE = 120
MAX_UPSCALE = 64
STEP_CHOICES = (8.0, 9.0, 10.0, 11.0, 12.0, 14.0)


# ------------------------------------------------------------------- planning


def blocks_per_row(width: int) -> int:
    return width // BLOCK


def blocks_total(width: int, height: int) -> int:
    """Every 8x8 block in the image (all rows, ignoring the right/bottom offcuts)."""
    return (width // BLOCK) * (height // BLOCK)


def blocks_needed(payload_bytes: int, profile: LeadProfile | None = None) -> int:
    """8x8 blocks required to carry ``payload_bytes`` under ``profile``."""
    return -(-channel_bits(payload_bytes, profile) // LANES)


def max_payload_bytes(width: int, height: int) -> int:
    """Largest payload this image can carry.

    The mapping payload -> blocks is monotone, so binary search for the largest
    value whose real cost still fits.  Doing it from the actual cost function
    (rather than an algebraic shortcut) keeps this in exact agreement with
    ``blocks_needed`` and hence with ``check_cover`` -- a mismatch here used to
    let one code path claim capacity that another then rejected.
    """
    budget_blocks = blocks_total(width, height)
    if budget_blocks <= 0:
        return 0
    cheapest = min(LEAD_PROFILES.values(), key=lambda p: p.blocks)
    if blocks_needed(1, cheapest) > budget_blocks:
        return 0
    low, high = 1, MAX_RS_BLOCKS * RS_BLOCK_DATA
    while low < high:
        mid = (low + high + 1) // 2
        profile = lead_profile_for(budget_blocks, mid)
        if blocks_needed(mid, profile) <= budget_blocks:
            low = mid
        else:
            high = mid - 1
    return low


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
        return False, f"容量不足：最多 {have} 字节，需要 {payload_bytes} 字节"
    return True, ""


def channel_bits(payload_bytes: int, profile: LeadProfile | None = None) -> int:
    """Bits the channel must carry for one payload (lead codeword + body)."""
    profile = profile or LEAD_STANDARD
    _data, total = rs_plan(payload_bytes)
    return (profile.code_bytes + total) * 8


def suggest_size(image: Image.Image, payload_bytes: int = PUBLIC_KEY_BYTES) -> tuple[int, int]:
    """Smallest proportional enlargement that lets ``image`` carry a payload.

    ``payload_bytes`` is the *payload* length; the frame's own lead and CRC are
    added on top, so the returned size is guaranteed to satisfy
    :func:`check_cover` for the same payload.
    """
    width, height = image.size
    need = blocks_needed(payload_bytes)   # includes lead + CRC + RS overhead
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


def _scatter_blocks(plane3: np.ndarray, channel: int, blocks: np.ndarray,
                    positions: np.ndarray, width: int) -> None:
    """Write the modified ``blocks`` back into ``plane3`` in place.

    Each block id maps back to a square of pixels; only those squares are
    updated, so the untouched 99.99% of a large photo is never copied.
    """
    cols = blocks_per_row(width)
    values = np.clip(np.rint(blocks[positions]), 0, 255).astype(np.uint8)
    top = ((positions // cols) * BLOCK)[:, None, None]
    left = ((positions % cols) * BLOCK)[:, None, None]
    rows = top + np.arange(BLOCK)[None, :, None]
    coln = left + np.arange(BLOCK)[None, None, :]
    plane3[rows, coln, channel] = values


def _block_partner(width: int, height: int, key: bytes, step: float) -> np.ndarray:
    """All blocks of the image in key-derived order (used as the carrier pool)."""
    count = blocks_total(width, height)
    generator = rng(derive(key, "blocks"),
                    struct.pack(">HH", width, height),
                    struct.pack(">d", float(step)))
    order = np.arange(count)
    generator.shuffle(order)
    return order




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
    # The domain separator is salted with the cover width so a channel choice
    # cannot be reused across differently sized covers.
    generator = rng(derive(key, "carriers"), struct.pack(">H", width),
                    struct.pack(">d", float(step)))
    order = tuple(int(v) for v in generator.permutation(len(CARRIER_ORDER)))
    # ``steps`` is indexed by *plane number* (0=Y, 1=Cb, 2=Cr) so that writers
    # and readers agree regardless of the shuffled ``order``.
    drawn = generator.uniform(0.9, 1.1, len(CARRIER_ORDER)) * step
    steps = [0.0] * len(CARRIER_ORDER)
    for slot, plane in enumerate(order):
        steps[plane] = float(drawn[slot])
    return order, tuple(steps)


#: Pixels sampled when scoring channels.  A megapixel cover costs seconds if we
#: score every pixel, yet the statistics we need (clip rate, dynamic range)
#: converge long before that; 64k samples is within ~1% of the full-image value.
SCORE_SAMPLES = 65_536


def _carrier_scores(ycbcr: np.ndarray) -> list[float]:
    """Per-channel viability in ``0..1`` for the Y, Cb, Cr planes.

    A channel is a poor carrier when the colour conversion clips it (saturated
    covers) or when it carries almost no variation to hide data in (flat
    covers).  The score blends "fraction of pixels stable across the RGB round
    trip" with the channel's dynamic range.

    Large covers are scored on a strided subsample: the quantity is a
    proportion, so sampling error falls off as 1/sqrt(n) and 64k samples keeps
    it well under the 0.02 decision margin used by :func:`_pick_carrier`.
    """
    height, width = ycbcr.shape[:2]
    total = height * width
    if total > SCORE_SAMPLES:
        # Take a contiguous patch large enough for the RGB/YCbCr round trip to
        # behave exactly as it does on the full image (Pillow converts whole
        # arrays), picking stride so the sample stays around SCORE_SAMPLES.
        stride = max(1, int((total / SCORE_SAMPLES) ** 0.5))
        ycbcr = ycbcr[::stride, ::stride, :]

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


# ----------------------------------------------------------------- embedding
def _embed_at_step(cover: Image.Image, frame: bytes, payload: bytes, key: bytes,
                   step: float) -> tuple[Image.Image, dict]:
    """One embedding attempt at a fixed QIM step, with channel fallback."""
    width, height = cover.size
    profile = lead_profile_for(blocks_total(width, height), len(payload))
    if profile is not LEAD_COMPACT:
        # Re-wrap so the lead's nonce length matches the profile actually used.
        frame = build_frame(payload, profile=profile)
    codeword, nblocks = ecc.encode(frame, profile)
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
        # Only the blocks we actually touched need writing back.  The order is
        # shuffled over the whole image, so the used blocks are scattered, but
        # there are only ``needed`` of them: copying the full plane to patch a
        # handful of 8x8 squares wastes a second on a 64 Mpx photo.
        _scatter_blocks(ycbcr, plane_index, blocks, order[:needed], width)
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
                "lead_profile": profile.index,
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
    if not ecc.available():
        raise ecc.ChannelUnavailable("需要 reedsolo：pip install reedsolo")
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
                        plane_index: int, plane_qim_step: float,
                        ycbcr: np.ndarray | None = None) -> tuple[bytes, dict]:
    """Two-stage read: decode the lead, learn the size, then read exactly it.

    The lead is one fixed-size RS codeword at the start of the bit stream, so
    the payload length and block count are available after one lead codeword
    worth of blocks.  Reading every block up front would make extraction cost
    grow with the *cover*, not the payload -- a 12 MP photo has ~900k blocks
    while a handshake needs 80.  Stage two therefore stops as soon as it has
    the exact number of blocks the frame declares.
    """
    if ycbcr is None:
        ycbcr = np.asarray(img_rgb.convert("YCbCr"), dtype=np.float64)
    width, height = img_rgb.size
    plane = ycbcr[:, :, plane_index]
    blocks = _blocks_of(plane)
    order = _block_partner(width, height, key, step)
    limit = min(len(order), MAX_FRAME_BITS // LANES)

    # Try each lead profile in turn: the reader cannot know which one the
    # writer chose until a lead decodes, so this is the price of blind
    # extraction.  The first profile that yields a valid lead wins outright.
    last_error: StegoError | None = None
    for profile in (LEAD_STANDARD, LEAD_COMPACT):
        try:
            return _read_with_profile(
                blocks, order, limit, plane_qim_step, profile,
                width, height, step, plane_index)
        except StegoError as exc:
            last_error = exc
    raise last_error or StegoError("没有可识别的隐写帧")


def _read_with_profile(blocks: np.ndarray, order: np.ndarray, limit: int,
                       plane_qim_step: float, profile: LeadProfile,
                       width: int, height: int, step: float,
                       plane_index: int) -> tuple[bytes, dict]:
    """Read a frame assuming ``profile``, decoding just as many blocks as needed."""
    lead_bits = profile.code_bytes * 8
    soft = np.zeros(max(lead_bits, 1), dtype=np.float64)
    confidence = np.zeros_like(soft)
    filled = 0

    def read_upto(positions: int) -> bytes:
        """Read bits for ``positions`` blocks, growing the buffers as needed."""
        nonlocal soft, confidence, filled
        need = positions * LANES
        if need > soft.size:
            grow = max(need - soft.size, soft.size)
            soft = np.concatenate([soft, np.zeros(grow)])
            confidence = np.concatenate([confidence, np.zeros(grow)])
        for position in range(filled, positions):
            coeffs = _fdct(blocks[int(order[position])])
            for lane in range(LANES):
                value, weight = _read_lane(coeffs, lane, plane_qim_step)
                soft[position * LANES + lane] = value
                confidence[position * LANES + lane] = weight
        filled = positions
        return _pack_lsb((soft[:need] < 0).astype(np.uint8))

    # Stage 1: just enough blocks for the lead, then decode it.
    lead_positions = min(limit, -(-lead_bits // LANES))
    all_bytes = read_upto(lead_positions)
    lead_plain = ecc.decode_lead(all_bytes, profile)
    blocks_at = len(MAGIC) + 1 + profile.nonce_bytes
    length_at = blocks_at + NBLOCK_BYTES
    nblocks = struct.unpack(">H", lead_plain[blocks_at:length_at])[0]
    length = struct.unpack(">H", lead_plain[length_at:length_at + 2])[0]
    if not 0 < length <= 0xFFFF or not 0 < nblocks <= MAX_RS_BLOCKS:
        raise StegoError("帧头内容不合理")

    # Stage 2: read only the declared body.
    size = min(RS_BLOCK_DATA + RS_BLOCK_NSYM, -(-length // nblocks) + RS_BLOCK_NSYM)
    total = profile.code_bytes + nblocks * size
    needed_positions = -(-total * 8 // LANES)
    if needed_positions > limit:
        raise StegoError(
            f"帧声明需要 {needed_positions} 块，此图仅有 {limit} 块（图像被裁剪？）")
    all_bytes = read_upto(needed_positions)

    body_code = all_bytes[profile.code_bytes:]
    body = ecc.decode_body(body_code, nblocks, size)[:length]
    payload = parse_frame(lead_plain + body, profile=profile)
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
    if not ecc.available():
        raise ecc.ChannelUnavailable("需要 reedsolo：pip install reedsolo")
    img = image.convert("RGB")
    width, height = img.size
    if min(width, height) < MIN_SIDE // 2 or blocks_per_row(width) == 0:
        raise StegoError(f"图像太小，不可能携带一帧（{width}×{height}）")
    channels, steps = _carriers(width, key, step)
    # Convert once and share: scoring the carriers and reading the payload need
    # the same YCbCr planes, and on a 64 Mpx photo a second conversion costs a
    # full second.
    planes = np.asarray(img.convert("YCbCr"), dtype=np.float64)
    best_plane, _score = _pick_carrier(planes, channels)
    choose = channels.index(best_plane)
    errors: list[str] = []
    for attempt in range(len(channels)):
        plane_index = channels[(choose + attempt) % len(channels)]
        try:
            return _extract_from_plane(img, key, step, plane_index,
                                       steps[plane_index], ycbcr=planes)
        except StegoError as exc:
            errors.append(f"{CARRIER_NAMES[plane_index]}: {exc}")
    raise StegoError(errors[0] if errors else "没有可识别的隐写帧")


def try_extract(image: Image.Image, key: bytes,
                steps: tuple[float, ...] = STEP_CHOICES) -> tuple[bytes, dict]:
    """Try every plausible QIM step; return the first frame that verifies.

    ``DEFAULT_STEP`` is attempted first because covers only escalate on
    extreme inputs; this turns the common case into a single decode instead of
    one per candidate step (which matters: each attempt re-reads the image).
    """
    ordered = sorted(steps, key=lambda value: abs(value - DEFAULT_STEP))
    errors: list[str] = []
    for step in ordered:
        try:
            return extract(image, key, step=step)
        except StegoError as exc:
            errors.append(f"step={step}: {exc}")
    raise StegoError("；".join(errors[:2]) if errors else "没有可识别的隐写帧")
