"""Adapter: the animated-GIF channel.

GIF is a *palettised* format -- 256 colours per frame -- so the QIM step has to
be far coarser than for a still photo (20 vs 10) or the palette quantisation
rounds the carrier coefficients back off the lattice. Otherwise the pipeline is
identical to the still-image channel, which is exactly why this adapter is thin:
it adds frame selection and the upscale gate, then defers to ``gif_stego``.

The upscale gate is the one genuinely GIF-specific rule. Enlarging an animation
multiplies *every* frame, so file size grows with the square of the scale factor
while the payload does not. ``gif_stego`` refuses when the cost is out of
proportion (a 156-frame 100×100 sticker going 2× grew 4521% in an early version),
and that refusal is surfaced verbatim here.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from .. import gif_stego
from ..core.errors import CapacityExceeded, NoHiddenFrame, UnsupportedCarrier
from ..core.framing import build_frame
from .base import Carrier, CarrierKind, EmbedReport, Envelope, Feasibility
from .options import GifOptions


class GifChannel:
    id = "gif"
    display_name = "动图（GIF DCT-QIM）"
    summary = "把数据藏进动图的某一帧，盲提取；受 256 色限制，步长需更粗"
    mime_in = ("image/gif",)
    mime_out = "image/gif"
    options_schema = GifOptions
    concurrency_hint = 2
    #: GIF re-encodes every frame per candidate step, so it is heavier per item.

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _open(carrier: Carrier) -> Image.Image:
        data = carrier.load()
        if isinstance(data, Image.Image):
            return data
        if not isinstance(data, (bytes, bytearray)):
            raise UnsupportedCarrier(
                f"GIF 信道需要 GIF 字节，收到 {type(data).__name__}"
            )
        try:
            image = Image.open(BytesIO(data))
        except UnidentifiedImageError as exc:
            raise UnsupportedCarrier("这不是可识别的 GIF 文件") from exc
        except OSError as exc:
            raise UnsupportedCarrier(f"GIF 无法解码：{exc}") from exc
        if getattr(image, "n_frames", 1) < 2:
            raise UnsupportedCarrier(
                "这是单帧图片，请改用图片通道；GIF 通道专为多帧动图设计"
            )
        return image

    @staticmethod
    def _cost_hint(carrier: Carrier) -> dict[str, float]:
        if carrier.size_hint:
            return {"source_kb": round(carrier.size_hint / 1024, 1)}
        return {}

    # -------------------------------------------------------------------- probe

    def probe(self, cover: Carrier, payload_bytes: int) -> Feasibility:
        if payload_bytes < 0:
            raise ValueError("payload_bytes 不能为负")
        if cover.kind is not CarrierKind.ANIMATION:
            return Feasibility(
                ok=False, capacity_bytes=0, required_bytes=payload_bytes,
                reasons=(f"GIF 信道不接受 {cover.kind.value} 载体",),
            )
        try:
            image = self._open(cover)
        except UnsupportedCarrier as exc:
            return Feasibility(ok=False, capacity_bytes=0,
                               required_bytes=payload_bytes, reasons=(str(exc),))

        info = gif_stego.capacity(image, 0)
        capacity = int(info["max_payload_bytes"])
        ok = capacity >= payload_bytes

        reasons: tuple[str, ...] = ()
        hints: list[str] = []
        if not ok:
            reasons = (
                f"首帧 {info['width']}×{info['height']} 只能容纳 {capacity} 字节，"
                f"需要 {payload_bytes} 字节",
            )
            hints.append("换一张更大的动图，或先用其它工具把它调大后重试")

        return Feasibility(
            ok=ok, capacity_bytes=capacity, required_bytes=payload_bytes,
            reasons=reasons, hints=tuple(hints), cost=self._cost_hint(cover),
        )

    # -------------------------------------------------------------------- embed

    def embed(self, cover: Carrier, payload: bytes, key: bytes,
              options: GifOptions | None = None,
              progress=None) -> Envelope:
        opts = options or GifOptions()
        image = self._open(cover)
        frame = build_frame(payload)

        try:
            data, info = gif_stego.embed_gif(
                image, frame, key,
                frames_to_use=(opts.frame_index,),
                step=opts.step,
            )
        except gif_stego.GifStegoError as exc:
            text = str(exc)
            if "只能容纳" in text or "容量" in text:
                raise CapacityExceeded(text) from exc
            raise

        capacity = int(gif_stego.capacity(image, 0)["max_payload_bytes"])
        report = EmbedReport(
            channel=self.id,
            payload_bytes=len(payload),
            carrier_bytes=len(data),
            capacity_bytes=capacity,
            options_used={
                "step": info.get("step", opts.step),
                "step_requested": opts.step if opts.step is not None else "auto",
                "frame_index": opts.frame_index,
            },
            distortion={k: float(v) for k, v in info.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)},
        )
        return Envelope(primary=data, report=report)

    # ------------------------------------------------------------------ extract

    def extract(self, carrier: Carrier, key: bytes,
                options: GifOptions | None = None, progress=None) -> bytes:
        opts = options or GifOptions()
        image = self._open(carrier)
        try:
            payload, _info = gif_stego.extract_gif(
                image, key, frame_index=opts.frame_index
            )
        except gif_stego.GifStegoError as exc:
            raise NoHiddenFrame(str(exc)) from exc
        return payload


__all__ = ["GifChannel"]
