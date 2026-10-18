"""Per-channel option schemas.

Each schema does three jobs at once, which is why they earn their keep:

1. **Validation** at the API boundary (``422`` with a specific message).
2. **UI generation** -- the web front-end renders a form straight from
   ``model_json_schema()``, so a new channel needs no front-end work.
3. **Documentation** -- defaults and bounds live next to the algorithm.

Pydantic is a soft dependency: the channels work without it (options are then
passed as plain dicts), it is only required to *generate* forms and to validate
untrusted API input. ``core`` stays dependency-free for the same reason.
"""

from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - pydantic is in the [web] extra
    BaseModel = object  # type: ignore[assignment,misc]

    def Field(default=None, **kwargs):        # type: ignore[no-redef]
        return default

    _HAS_PYDANTIC = False
else:
    _HAS_PYDANTIC = True


def available() -> bool:
    """Whether option schemas can validate and describe themselves."""
    return _HAS_PYDANTIC


def coerce(schema: type | None, options: Any) -> Any:
    """Turn a plain dict into ``schema``, or pass it through.

    Called by the registry so an HTTP request body and a Python keyword argument
    take exactly the same path.
    """
    if schema is None or not _HAS_PYDANTIC:
        return options
    if options is None:
        return schema()
    if isinstance(options, schema):
        return options
    if isinstance(options, dict):
        return schema(**options)
    raise TypeError(f"选项必须是 {schema.__name__} 或 dict，收到 {type(options).__name__}")


class ImageOptions(BaseModel):
    """Options for the still-image channel."""

    step: float = Field(
        default=10.0, ge=2.0, le=64.0,
        description="QIM 量化步长。越大越耐压缩/缩放，但改动越明显；默认 10 在 PNG 无损通道下不可见。",
    )
    upscale: bool = Field(
        default=True,
        description="小图自动等比放大到刚好装下载荷。关闭后小图会直接报容量不足。",
    )
    include_jpeg: bool = Field(
        default=False,
        description="额外产出一份 JPEG 版本。注意 JPEG 会破坏 QIM 格点，通常无法再提取。",
    )
    jpeg_quality: int = Field(
        default=95, ge=60, le=100,
        description="额外 JPEG 版本的质量。",
    )


class GifOptions(BaseModel):
    """Options for the animated-GIF channel."""

    step: float | None = Field(
        default=None, ge=8.0, le=200.0,
        description=(
            "QIM 步长。留空则自动从 20 起逐档尝试到 128，取第一个能稳定写入的值——"
            "GIF 只有 256 色，不同画面的可用步长差别很大。"
        ),
    )
    frame_index: int = Field(
        default=0, ge=0,
        description="把数据藏进第几帧。默认首帧，其余帧原样保留。",
    )
    upscale: bool = Field(
        default=True,
        description="小动图自动放大。放大倍数会作用于每一帧，体积按平方增长。",
    )


class ChineseOptions(BaseModel):
    """Options for the Chinese-text channel."""

    password: str | None = Field(
        default=None,
        description="口令。给了就用 AES-GCM 加密载荷；留空则只做编码不做加密。",
    )
    add_punctuation: bool = Field(
        default=True,
        description="按语料统计补标点，让输出更像自然中文。",
    )


__all__ = [
    "available", "coerce",
    "ImageOptions", "GifOptions", "ChineseOptions",
]
