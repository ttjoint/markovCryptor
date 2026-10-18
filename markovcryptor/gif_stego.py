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


def _to_palette(image: Image.Image, colors: int = MAX_COLORS) -> Image.Image:
    """Quantise to a GIF-ready palette without dithering-noise."""
    paletted = image.convert("RGB").quantize(
        colors=colors, method=Image.MEDIANCUT, dither=Image.NONE
    )
    # PIL trips over a stale tuple-valued transparency entry when saving P
    # images; the alpha channel is already flattened, so drop it.
    paletted.info.pop("transparency", None)
    return paletted


def frames_of(image: Image.Image) -> tuple[list[Image.Image], list[int]]:
    """Decode every frame of a (possibly animated) GIF.

    Returns ``(frames, durations)``; a still image yields a single frame.
    """
    count = getattr(image, "n_frames", 1)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for index in range(count):
        image.seek(index)
        frames.append(_flatten(image))
        durations.append(int(image.info.get("duration", 40) or 40))
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
    rgb_frames, durations = frames_of(source)
    if not rgb_frames:
        raise GifStegoError("GIF 没有可用的帧")
    targets = [i for i in frames_to_use if 0 <= i < len(rgb_frames)]
    if not targets:
        raise GifStegoError(f"指定的帧 {frames_to_use} 超出了范围 0..{len(rgb_frames) - 1}")

    # Start from the quantised pixels so the embedder writes into colours the
    # format can actually reproduce.
    paletted = [_to_palette(f) for f in rgb_frames]
    work = [p.convert("RGB") for p in paletted]

    # Frames that are too small get scaled up proportionally, exactly like still
    # covers, so a 40x40 emoji GIF is still usable.  Every frame must share the
    # new size, otherwise the animation would jitter.
    resized_note: dict[int, tuple[int, int]] = {}
    for index in targets:
        work[index], original_size, did_resize = upscale_cover(
            work[index], len(expected)
        )
        if did_resize:
            resized_note[index] = original_size
    if resized_note:
        target_size = work[targets[0]].size
        for index in range(len(work)):
            if index not in targets and work[index].size != target_size:
                work[index] = work[index].resize(target_size, Image.LANCZOS)
                paletted[index] = _to_palette(work[index])

    tried: list[float] = list(GIF_STEPS) if step is None else [step]
    last_error: Exception | None = None
    for candidate in tried:
        attempt = list(work)
        reports: list[dict] = []
        ok = True
        for index in targets:
            try:
                stego, report = embed(attempt[index], frame, key, step=candidate)
            except StegoError as exc:
                last_error = exc
                ok = False
                break
            attempt[index] = stego.convert("RGB")
            reports.append(report)
        if not ok:
            continue

        output = [_to_palette(f) for f in attempt]
        data = encode_gif(output, durations)

        # Trust only what a reader can get back out of the encoded file.
        reloaded = Image.open(BytesIO(data))
        try:
            for index in targets:
                reloaded.seek(index)
                got, _ = extract(reloaded, key, step=candidate)
                if got != expected:
                    ok = False
                    break
        except StegoError as exc:
            last_error = exc
            ok = False
        if ok:
            return data, {
                "frames": len(output),
                "embedded_frames": targets,
                "step": candidate,
                "bytes": len(data),
                "carriers": [r["carrier"] for r in reports],
                "max_block_change": max(r["max_block_change"] for r in reports),
                "payload_bytes": reports[0]["payload_bytes"],
                "resized": resized_note,
            }
    raise GifStegoError(
        f"该 GIF 无法稳定承载数据（已尝试 step={tried}）：{last_error}"
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
