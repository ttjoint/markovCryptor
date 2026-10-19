"""Cover library: real pictures to hide payloads in.

Every image this terminal sends must be based on a **real picture**.  A cover
made of random pixel noise carries a payload perfectly well, but it looks like
noise -- anyone glancing at it knows something is wrong.  That defeats the point.

So covers come from a directory of ordinary photographs.  Two consequences shape
this module:

* The repository does not ship a photo library (``data/`` is not version
  controlled, and a photo collection is not source code).  When no directory is
  configured or the directory is empty, we fall back to a *synthesised but
  realistic* picture -- smooth gradients, structured shapes, film-like grain --
  rather than to noise.  It is not as good as a photo, and :func:`describe`
  says so, but it is never obviously fake the way static is.
* Cover choice must not repeat conspicuously.  Reusing one picture for every
  handshake response is itself a tell, so selection is random over the library
  and avoids the most recently used cover.

The library is deliberately dumb: it hands out bytes.  Whether those bytes are
big enough for a payload is the channel's business, not ours.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from PIL import Image

from .. import image_stego

#: Extensions a cover library will pick up.  GIF is excluded on purpose: the
#: library feeds the *image* channel, and an animated cover would silently get
#: flattened to its first frame.
COVER_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

#: The built-in fallback cover is 320x320 (712 bytes of capacity), which leaves
#: room for a handshake with a wide margin while staying small enough to embed
#: quickly.
FALLBACK_SIZE = 320


class CoverLibrary:
    """A set of cover pictures, loaded lazily from a directory.

    ``directory`` may be missing entirely -- that is the normal case in a fresh
    checkout, and the library then serves the built-in cover.  This keeps
    ``import`` cheap and keeps the tests runnable without user data.
    """

    def __init__(self, directory: str | Path | None = None, *,
                 seed: int | None = None) -> None:
        self.directory = Path(directory) if directory else None
        self._paths: list[Path] = []
        self._random = random.Random(seed)
        #: Recently handed-out covers, most recent last.  Avoided when choosing
        #: so consecutive messages do not reuse one picture.
        self._recent: list[Path] = []
        self._scanned = False
        self._fallback: bytes | None = None

    # ------------------------------------------------------------------ public

    def covers(self) -> list[Path]:
        """All usable cover files, sorted for determinism. Cached after first call."""
        if not self._scanned:
            self._paths = _scan(self.directory)
            self._scanned = True
        return list(self._paths)

    @property
    def is_fallback(self) -> bool:
        """True when no real pictures are available and the built-in is used."""
        return not self.covers()

    def describe(self) -> str:
        count = len(self.covers())
        if count:
            return f"{count} 张底图（{self.directory}）"
        where = f"{self.directory} 下没有可用图片" if self.directory else "未配置底图目录"
        return f"{where}，使用内建替代图"

    def next_cover(self) -> bytes:
        """One cover as image bytes, avoiding the last few used."""
        paths = self.covers()
        if not paths:
            return self.fallback_bytes()
        candidates = [p for p in paths if p not in self._recent] or paths
        chosen = self._random.choice(candidates)
        self._recent.append(chosen)
        # Remember at most half the library: enough to avoid a visible repeat,
        # small enough that a small library still rotates.
        keep = max(1, len(paths) // 2)
        self._recent = self._recent[-keep:]
        data = chosen.read_bytes()
        return _normalised(data)

    def cover_for(self, minimum_bytes: int) -> bytes:
        """A cover guaranteed to have ``minimum_bytes`` of capacity.

        Raises :class:`~markovcryptor.core.errors.CapacityExceeded` if even the
        largest available cover is too small -- naming the shortfall, because
        "handshake failed" is not something a user can act on.
        """
        from ..core.errors import CapacityExceeded

        paths = self.covers()
        usable: list[Path] = []
        for path in paths:
            try:
                with Image.open(path) as probe:
                    if image_stego.max_payload_bytes(*probe.size) >= minimum_bytes:
                        usable.append(path)
            except Exception:                   # noqa: BLE001 - a bad file is skipped
                continue
        if usable:
            candidates = [p for p in usable if p not in self._recent] or usable
            chosen = self._random.choice(candidates)
            self._recent.append(chosen)
            return _normalised(chosen.read_bytes())

        fallback = self.fallback_bytes()
        with Image.open(io.BytesIO(fallback)) as image:
            have = image_stego.max_payload_bytes(*image.size)
        if have >= minimum_bytes:
            return fallback
        raise CapacityExceeded(
            f"没有足够大的底图：需要 {minimum_bytes} 字节，最大的底图只有 {have} 字节",
            reasons=(f"底图目录 {self.directory} 里没有能容纳 {minimum_bytes} 字节的图片"
                     if self.directory else "未配置底图目录，内建底图容量不足",
                     f"内建底图 {FALLBACK_SIZE}×{FALLBACK_SIZE} 只有 {have} 字节容量"),
            hints=(f"放入至少 {_side_for(minimum_bytes)}×{_side_for(minimum_bytes)} 的图片到 "
                   f"{self.directory or '底图目录'}",
                   "或减小载荷（握手报文固定 38 字节，无法再减）"),
        )

    def fallback_bytes(self) -> bytes:
        """The built-in realistic cover, encoded once and cached."""
        if self._fallback is None:
            self._fallback = _png(_build_fallback(FALLBACK_SIZE))
        return self._fallback


# ------------------------------------------------------------------ fallback


def _build_fallback(size: int) -> Image.Image:
    """A synthesised picture that looks like a photograph, not like noise.

    The construction matters: the image channel needs mid-frequency detail to
    hide bits in, but random noise is instantly recognisable as fake.  So the
    base is a few broad gradients (sky-like), a soft horizon, a handful of
    structured blobs, and finally a light grain.  That gives plenty of
    coefficients to modulate while remaining plausible to the eye.
    """
    import numpy as np

    rng = np.random.default_rng(0xC0FFEE)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    u = xx / size
    v = yy / size

    # Vertical gradient (darker top -> lighter bottom) with a warm tint.
    base = np.empty((size, size, 3), dtype=np.float64)
    base[:, :, 0] = 90 + 110 * v            # R
    base[:, :, 1] = 110 + 95 * v            # G
    base[:, :, 2] = 150 + 60 * v            # B

    # A soft diagonal light falloff, as if lit from one side.
    glow = 55 * np.exp(-((u - 0.28) ** 2 + (v - 0.22) ** 2) / 0.08)
    base += glow[:, :, None] * np.array([0.8, 0.9, 1.0])

    # A few blurred blobs standing in for objects.
    for _ in range(6):
        cx, cy = rng.uniform(0.15, 0.85, 2)
        sigma = rng.uniform(0.06, 0.16)
        strength = rng.uniform(-38, 38)
        blob = strength * np.exp(-(((u - cx) ** 2 + (v - cy) ** 2) / (2 * sigma ** 2)))
        base += blob[:, :, None] * rng.uniform(0.6, 1.0, 3)

    # Gentle horizontal banding: large-scale structure, still smooth.
    band = 9 * np.sin(2 * np.pi * (v * rng.uniform(1.5, 3.0) + rng.uniform(0, 1)))
    base += band[:, :, None]

    # Film-like grain: small, which is where the hidden bits will live.
    base += rng.normal(0, 3.0, base.shape)

    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


# -------------------------------------------------------------------- helpers


def _side_for(payload_bytes: int) -> int:
    """Smallest square side that holds ``payload_bytes``, rounded up to 8 px."""
    side = 96
    while side <= 4096 and image_stego.max_payload_bytes(side, side) < payload_bytes:
        side += 8
    return side


def _scan(directory: Path | None) -> list[Path]:
    if directory is None or not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() in COVER_SUFFIXES and path.is_file():
            found.append(path)
    return found


def _normalised(data: bytes) -> bytes:
    """Re-encode a cover as PNG.

    Covers arrive as JPEG/WEBP/BMP too, but the image channel's output must be
    lossless or the embedded bits die on save.  Converting up front means the
    embedder only ever sees PNG.
    """
    with Image.open(io.BytesIO(data)) as image:
        if image.format == "PNG":
            return data
        rgb = image.convert("RGB")
    return _png(rgb)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


__all__ = ["COVER_SUFFIXES", "FALLBACK_SIZE", "CoverLibrary"]
