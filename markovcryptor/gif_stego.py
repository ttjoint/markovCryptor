"""GIF support for the MKX1 image channel.

GIF is a *palettised* format: every pixel is an index into a table of at most
256 RGB colours.  That breaks the plain DCT-QIM path in two ways:

* saving a full-colour image as GIF re-quantises every pixel, and the resulting
  error (often tens of levels) is far larger than one QIM lattice cell;
* palette colours may be shared by neighbouring pixels, so a small change can
  alter large flat areas at once.

What *does* survive is a GIF that is written back with a palette the image can
already represent exactly.  ``PIL``'s ``quantize(256)`` is lossless when the
frame has at most 256 distinct colours, which is true of every GIF frame.  So
the recipe is:

1. decode the frame to RGB (start from the *quantised* pixels, so we know the
   exact colours the format can keep);
2. embed with a coarser QIM step (``GIF_MIN_STEP``), because the palette
   boundary will absorb part of the lattice headroom;
3. quantise back to 256 colours and save as GIF;
4. verify by extracting from the *reloaded* GIF, widening the step on failure.

Only the frames the caller asks for are touched; by default frame 0.  Leaving
the remaining frames byte-identical is what keeps the file size stable, since
GIF compression works on differences between frames.
"""

from __future__ import annotations

import os
from io import BytesIO

from PIL import Image

from .image_stego import (
    DEFAULT_STEP,
    StegoError,
    embed,
    extract,
    max_payload_bytes,
)

#: Steps tried when embedding into a palettised frame, coarsest last.
GIF_STEPS = (20.0, 28.0, 40.0, 64.0, 96.0, 128.0)

#: GIF palettes hold at most 256 entries.
MAX_COLORS = 256

#: Scaling an animation up multiplies *every* frame, so the file grows with the
#: square of the scale factor.  A 156-frame 100x100 sticker going 2x turned
#: 667 KB into 5.7 MB to carry 32 bytes, which is not a trade anyone wants.
#: We therefore allow a small enlargement but refuse once the predicted size
#: cost is out of proportion to the payload.
MAX_GIF_UPSCALE = 2
#: Refuse an upscale whose projected file growth exceeds this multiple.
#: Projected pixel-area growth we tolerate.  2x per side (4x the pixels) is
#: the ceiling: past that the animation is mostly being re-encoded, not
#: carrying a message.
MAX_GIF_GROWTH = 4.0
#: Absolute ceiling on the enlarged animation thinkable at all.  Doubling a
#: 6 KB 47-frame sticker yields 283 KB; that is 47x the original file to carry
#: 32 bytes, well past any sane trade.
MAX_GIF_UPSCALE_BYTES = 512 * 1024
#: Largest file we are willing to produce by enlarging.  Scaling multiplies the
#: pixel count *and* breaks the inter-frame delta compression that keeps
#: stickers small, so a 47-frame 155x60 original can go 6 KB -> 283 KB -- the
#: area ratio alone never catches that.  We therefore encode one enlarged frame
#: to get a real per-frame cost and extrapolate from it.
MAX_GIF_UPSCALE_RESULT = 160 * 1024


class GifStegoError(StegoError):
    """Raised when a GIF frame cannot carry the requested payload."""


def _flatten(image: Image.Image) -> Image.Image:
    """Return an RGB frame, compositing any transparency onto black."""
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (0, 0, 0))
        canvas.paste(rgba.convert("RGB"), mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _source_size(image: Image.Image) -> int:
    """Bytes the animation occupies on disk, or 0 when it was not opened from one."""
    stream = getattr(image, "fp", None)
    try:
        return os.fstat(stream.fileno()).st_size
    except (AttributeError, OSError, ValueError):
        return 0


def _gif_frame_bytes(image: Image.Image) -> int:
    """Bytes a single frame occupies once stored as GIF."""
    probe = BytesIO()
    _to_palette(image).save(probe, "GIF")
    return len(probe.getvalue())


def _to_palette(image: Image.Image, colors: int = MAX_COLORS) -> Image.Image:
    """Quantise to a GIF-ready palette without dithering-noise."""
    paletted = image.convert("RGB").quantize(
        colors=colors, method=Image.MEDIANCUT, dither=Image.NONE
    )
    # PIL trips over a stale tuple-valued transparency entry when saving P
    # images; the alpha channel is already flattened, so drop it.
    paletted.info.pop("transparency", None)
    return paletted


def frames_of(image: Image.Image,
              keep: tuple[int, ...] | None = None
              ) -> tuple[list[Image.Image], list[int]]:
    """Decode frames of a (possibly animated) GIF, flattened to RGB.

    ``keep`` limits decoding to the frames that will actually be modified;
    the others are still returned, but as cheap palette copies.  Decoding a
    3700-frame sticker sheet takes seconds, and only frame 0 is ever written.
    """
    count = getattr(image, "n_frames", 1)
    wanted = set(range(count)) if keep is None else set(keep)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for index in range(count):
        image.seek(index)
        durations.append(int(image.info.get("duration", 40) or 40))
        if index in wanted:
            frames.append(_flatten(image))
        else:
            # Untouched frames are handed to the encoder as-is.  Quantising
            # them here would cost ~100ms per frame on a 1080p animation and
            # the encoder quantises again anyway.
            frames.append(image if image.mode == "P" else image.convert("RGB"))
    image.seek(0)
    return frames, durations


def encode_gif(frames: list[Image.Image], durations: list[int],
               loop: int = 0) -> bytes:
    """Serialise palettised frames into a single animated GIF byte string."""
    buffer = BytesIO()
    if len(frames) == 1:
        frames[0].save(buffer, "GIF")
    else:
        frames[0].save(
            buffer,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            optimize=True,
            disposal=1,
        )
    return buffer.getvalue()


def embed_gif(source: Image.Image, frame: bytes, key: bytes,
              *, frames_to_use: tuple[int, ...] = (0,),
              step: float | None = None) -> tuple[bytes, dict]:
    """Embed ``frame`` into the selected frames of a GIF.

    Returns ``(gif_bytes, report)``.  The report records the step that worked
    and how much of the image had to change, mirroring ``image_stego.embed``.
    """
    from .image_stego import parse_frame, upscale_cover

    expected = parse_frame(frame)
    count = getattr(source, "n_frames", 1)
    targets = [i for i in frames_to_use if 0 <= i < count]
    if not targets:
        raise GifStegoError(f"指定的帧 {frames_to_use} 超出了范围 0..{count - 1}")

    # Decode only the frames we are about to modify; the rest are re-encoded
    # from their palette data untouched.
    rgb_frames, durations = frames_of(source, keep=tuple(targets))

    # Start from the quantised pixels so the embedder writes into colours the
    # format can actually reproduce.
    base: list[Image.Image] = []
    for index, frame_image in enumerate(rgb_frames):
        if index in targets:
            base.append(_to_palette(frame_image).convert("RGB"))
        else:
            base.append(frame_image)

    # Size of the animation on disk, used to veto absurd enlargements.  The
    # number of frames alone says nothing: a 47-frame 155x60 sticker is 6 KB
    # until you double it, at which point it becomes 283 KB.
    source_bytes = _source_size(source)

    tried: list[float] = list(GIF_STEPS) if step is None else [step]
    last_error: Exception | None = None

    def attempt_at(frames: list[Image.Image], candidate: float,
                   resized: dict[int, tuple[int, int]]):
        """One full embed -> encode -> read-back cycle at a fixed step."""
        nonlocal last_error
        work = list(frames)
        reports: list[dict] = []
        for index in targets:
            try:
                stego, report = embed(work[index], frame, key, step=candidate)
            except StegoError as exc:
                last_error = exc
                return None, None
            work[index] = stego.convert("RGB")
            reports.append(report)

        # Untouched frames are already palettised; re-quantising them would
        # shuffle their palette and inflate the file for no reason.
        output = [f if f.mode == "P" else _to_palette(f) for f in work]
        data = encode_gif(output, durations)
        # ``embed`` already round-tripped the payload in memory, so a failure
        # here means the *encoder* changed the pixels -- a property of the step,
        # not of this attempt.  Remember it instead of re-encoding (a 100-frame
        # animation costs ~10s per encode, and the step loop would pay it six
        # times over).


        # Trust only what a reader can get back out of the encoded file.
        reloaded = Image.open(BytesIO(data))
        try:
            for index in targets:
                reloaded.seek(index)
                got, _ = extract(reloaded, key, step=candidate)
                if got != expected:
                    last_error = StegoError("重新加载后提取结果不符")
                    return None, None
        except StegoError as exc:
            last_error = exc
            return None, None
        return data, {
            "frames": len(output),
            "embedded_frames": targets,
            "step": candidate,
            "bytes": len(data),
            "carriers": [r["carrier"] for r in reports],
            "lead_profile": reports[0].get("lead_profile", 0),
            "max_block_change": max(r["max_block_change"] for r in reports),
            "payload_bytes": reports[0]["payload_bytes"],
            "resized": resized,
        }

    # Pass 1: keep the original geometry.  A coarser lattice usually succeeds
    # where a finer one fails, and it costs nothing; enlarging a 47-frame
    # animation to squeeze in 32 bytes would multiply the file size instead.
    for candidate in tried:
        data, report = attempt_at(base, candidate, {})
        if data is not None:
            return data, report

    # Pass 2: the frame really is too small.  Scale it up proportionally --
    # every frame together, so the animation does not jitter -- and retry.
    resized: dict[int, tuple[int, int]] = {}
    scaled = list(base)
    for index in targets:
        scaled[index], original_size, did_resize = upscale_cover(
            scaled[index], len(expected)
        )
        if did_resize:
            new_w, new_h = scaled[index].size
            ratio = max(new_w / max(original_size[0], 1),
                        new_h / max(original_size[1], 1))
            # GIF stays compressed, so file size tracks pixel *area*; each
            # untouched frame is enlarged too, so the cost is paid count times.
            growth = (new_w * new_h) / max(original_size[0] * original_size[1], 1)
            # Growth is bounded relative to the original, but a *tiny* file
            # growing 4x is still trivial and a *small* one is not: 6 KB -> 24
            # KB is fine, 6 KB -> 283 KB is not.  Both the ratio and the
            # absolute delta have to pass.
            # Enlarging forces every frame to be stored at the new resolution,
            # so the projected size is (frames x enlarged-frame bytes), not
            # (original size x area ratio): delta compression is what made the
            # original small, and it does not survive.
            per_frame = _gif_frame_bytes(scaled[index])
            projected = per_frame * len(rgb_frames)
            too_expensive = (
                ratio > MAX_GIF_UPSCALE
                or growth > MAX_GIF_GROWTH
                or source_bytes > MAX_GIF_UPSCALE_BYTES
                or projected > MAX_GIF_UPSCALE_RESULT
            )
            if too_expensive:
                capacity = max_payload_bytes(*original_size)
                raise GifStegoError(
                    f"GIF 太小：{original_size[0]}×{original_size[1]} 只能容纳 "
                    f"{capacity} 字节，需要 {len(expected)} 字节。放大 {ratio:.1f}× "
                    f"会让全部 {len(rgb_frames)} 帧的像素量变成 {growth:.1f} 倍"
                    f"（原文件 {source_bytes // 1024} KB），体积代价与载荷不成比例。"
                    f"请换一张更大的动图，或先用其它工具把它调大后重试。")
            resized[index] = original_size
    if resized:
        # Every frame must land on the same grid, but untouched frames stay in
        # palette mode: scaling a P image with NEAREST keeps the palette intact,
        # whereas converting to RGB and re-quantising each frame is what made
        # a 667 KB animation balloon to 5.7 MB.
        target_size = scaled[targets[0]].size
        for index in range(len(scaled)):
            if index in targets or scaled[index].size == target_size:
                continue
            if scaled[index].mode == "P":
                scaled[index] = scaled[index].resize(target_size, Image.NEAREST)
            else:
                scaled[index] = _to_palette(
                    scaled[index].resize(target_size, Image.LANCZOS))
        for candidate in tried:
            data, report = attempt_at(scaled, candidate, resized)
            if data is not None:
                return data, report

    detail = str(last_error) if last_error else "未知原因"
    raise GifStegoError(
        f"该 GIF 无法稳定承载数据（已尝试 step={tried}）：{detail}。"
        f"动图帧多为大面积纯色/渐变色块时 QIM 难以写入，"
        f"可换一张细节更丰富的动图，或改用静态图通道。"
    )


def extract_gif(data: bytes | Image.Image, key: bytes,
                *, frame_index: int = 0,
                steps: tuple[float, ...] = GIF_STEPS) -> tuple[bytes, dict]:
    """Blindly extract the payload from one frame of a GIF."""
    source = Image.open(BytesIO(data)) if isinstance(data, bytes) else data
    count = getattr(source, "n_frames", 1)
    if frame_index >= count:
        raise GifStegoError(f"GIF 只有 {count} 帧，无法读取第 {frame_index} 帧")
    source.seek(frame_index)
    rgb = _flatten(source)

    errors: list[str] = []
    tried = list(steps)
    if DEFAULT_STEP not in tried:
        tried.insert(0, DEFAULT_STEP)
    for step in tried:
        try:
            payload, info = extract(rgb, key, step=step)
        except StegoError as exc:
            errors.append(f"step={step}: {exc}")
            continue
        info = dict(info)
        info["frame_index"] = frame_index
        info["gif_frames"] = count
        return payload, info
    raise GifStegoError("；".join(errors[-2:]) if errors else "GIF 中没有可识别的隐写帧")


def capacity(image: Image.Image, frame_index: int = 0) -> dict:
    """Capacity report for one GIF frame, plus the upscale it would need."""
    image.seek(frame_index)
    width, height = image.size
    payload = max_payload_bytes(width, height)
    return {
        "width": width,
        "height": height,
        "frames": getattr(image, "n_frames", 1),
        "max_payload_bytes": payload,
        "public_keys": payload // 32,
        "note": "容量按首帧像素数计算；逐帧嵌入可得到 帧数× 的总容量",
    }
