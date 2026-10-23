"""Adapter: the DCT-QIM still-image channel.

Thin by design. Everything interesting lives in ``image_stego``; this module
only translates between the uniform channel vocabulary (``Carrier``,
``Feasibility``, ``Envelope``) and the concrete functions that already exist and
are already tested against 15 488 real photos.

Two behaviours worth knowing, both inherited from the kernel rather than added:

* **Small covers are enlarged automatically.** ``suggest_size`` finds the
  smallest proportional scale that fits, so a 64×64 icon still works. The report
  says so, because silently enlarging a user's image is surprising.
* **The QIM step may widen.** Flat or saturated covers cannot take a fine
  lattice, so ``embed`` retries with coarser steps. ``options_used["step"]``
  reflects what actually worked, not what was asked for.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from .. import image_stego
from ..core.errors import CapacityExceeded, NoHiddenFrame, UnsupportedCarrier
from .base import (
    Carrier,
    CarrierKind,
    EmbedReport,
    Envelope,
    Feasibility,
)
from .options import ImageOptions


class ImageChannel:
    id = "image"
    display_name = "图片（DCT-QIM）"
    summary = "把数据藏进照片的中频 DCT 系数，盲提取，不依赖原图"
    mime_in = ("image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff")
    mime_out = "image/png"
    options_schema = ImageOptions
    concurrency_hint = 4
    #: numpy releases the GIL for the DCT work, Pillow does not for decode/encode.

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _open(carrier: Carrier) -> Image.Image:
        """Load a carrier into an RGB image, naming the real failure cause."""
        data = carrier.load()
        if isinstance(data, Image.Image):
            return data.convert("RGB")
        if not isinstance(data, (bytes, bytearray)):
            raise UnsupportedCarrier(
                f"图片信道需要 PNG/JPEG 字节，收到 {type(data).__name__}"
            )
        try:
            with Image.open(BytesIO(data)) as probe:
                # ``copy`` detaches from the file object so the caller may close it,
                # and normalises modes in one step.
                return probe.convert("RGB")
        except UnidentifiedImageError as exc:
            raise UnsupportedCarrier("这不是可识别的图片文件") from exc
        except OSError as exc:
            raise UnsupportedCarrier(f"图片无法解码：{exc}") from exc

    @staticmethod
    def _options(options: ImageOptions | None):
        return options or ImageOptions()

    # -------------------------------------------------------------------- probe

    def probe(self, cover: Carrier, payload_bytes: int) -> Feasibility:
        if payload_bytes < 0:
            raise ValueError("payload_bytes 不能为负")
        if cover.kind is not CarrierKind.IMAGE:
            return Feasibility(
                ok=False, capacity_bytes=0, required_bytes=payload_bytes,
                reasons=(f"图片信道不接受 {cover.kind.value} 载体",),
            )
        try:
            image = self._open(cover)
        except UnsupportedCarrier as exc:
            return Feasibility(ok=False, capacity_bytes=0,
                               required_bytes=payload_bytes, reasons=(str(exc),))

        width, height = image.size
        capacity = image_stego.max_payload_bytes(width, height)
        ok, reason = image_stego.check_cover(image, payload_bytes)

        cost: dict[str, float] = {}
        hints: list[str] = []
        if not ok and "容量不足" in reason:
            # Fit is possible after enlargement -- say how much, and what it costs.
            target = image_stego.suggest_size(image, payload_bytes)
            scale = target[0] / width if width else 1.0
            cost["upscale"] = round(scale, 2)
            hints.append(
                f"放大到 {target[0]}×{target[1]}（{scale:.1f}×）即可承载"
            )
        elif not ok:
            hints.append("换一张更大的图片，或减小载荷")

        return Feasibility(
            ok=ok, capacity_bytes=capacity, required_bytes=payload_bytes,
            reasons=() if ok else (reason,), hints=tuple(hints), cost=cost,
        )

    # -------------------------------------------------------------------- embed

    def embed(self, cover: Carrier, payload: bytes, key: bytes,
              options: ImageOptions | None = None,
              progress=None) -> Envelope:
        opts = self._options(options)
        image = self._open(cover)
        capacity = image_stego.max_payload_bytes(*image.size)

        # 尺寸钳制在这里统一做：先缩到安全上限（缩小总是安全的），
        # 再按需要放大（上限 1.5×，超了就不放 —— 宁报容量不足也不发糊图）。
        # 关掉 upscale 时只做缩小。
        used: Image.Image = image
        upscaled = False
        resize_note = ""
        if opts.upscale:
            used, _orig, _final, resize_note = image_stego.normalize_cover(
                image, len(payload))
            upscaled = used.size != image.size
        else:
            used, _orig, shrunk = image_stego.fit_within(image)
            upscaled = False
            if shrunk:
                resize_note = (f"从 {image.size[0]}×{image.size[1]} "
                               f"缩到 {used.size[0]}×{used.size[1]}")
        if used.size != image.size:
            capacity = image_stego.max_payload_bytes(*used.size)

        frame = image_stego.build_frame(payload)
        try:
            stego, info = image_stego.embed(used, frame, key, step=opts.step)
        except image_stego.StegoError as exc:
            # A capacity problem must stay distinguishable from a robustness
            # problem, so the caller can act differently.
            if "容量不足" in str(exc):
                raise CapacityExceeded(
                    str(exc),
                    hints=(f"最大可承载 {capacity} 字节",),
                ) from exc
            raise

        buffer = BytesIO()
        stego.save(buffer, format="PNG", optimize=True)
        png = buffer.getvalue()

        used_options = {
            "step": info.get("step", opts.step),
            "step_requested": opts.step,
            "upscaled": upscaled,
            "size": f"{stego.size[0]}x{stego.size[1]}",
            # 给用户看的尺寸变化说明（缩小/放大各是什么倍率）。
            "resize_note": resize_note,
        }
        alternatives: dict[str, bytes] = {}
        if opts.include_jpeg:
            alt = BytesIO()
            stego.save(alt, format="JPEG", quality=opts.jpeg_quality)
            alternatives[f"jpeg-q{opts.jpeg_quality}"] = alt.getvalue()

        report = EmbedReport(
            channel=self.id,
            payload_bytes=len(payload),
            carrier_bytes=len(png),
            capacity_bytes=capacity,
            options_used=used_options,
            distortion={k: float(v) for k, v in info.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)},
        )
        return Envelope(primary=png, alternatives=alternatives, report=report)

    # ------------------------------------------------------------------ extract

    def extract(self, carrier: Carrier, key: bytes,
                options: ImageOptions | None = None, progress=None) -> bytes:
        image = self._open(carrier)
        try:
            payload, _info = image_stego.try_extract(image, key)
        except image_stego.StegoError as exc:
            raise NoHiddenFrame(str(exc)) from exc
        return payload


__all__ = ["ImageChannel"]
