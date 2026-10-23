"""HTTP 层共用的小工具：异常映射、消息序列化、data URL 编码。

放在 api/ 而不是 services/ 是因为它们要么返回 HTTPException，
要么只服务于 HTTP 响应结构 —— 不是领域逻辑。
"""

from __future__ import annotations

import base64
import logging

from fastapi import HTTPException

from ...core.errors import StegoError
from ...observability import event
from ..services import media as media_service


def http_error(exc: BaseException, *, fallback: int = 502) -> HTTPException:
    """Turn an exception into an HTTP error the user can act on.

    ``StegoError`` already carries ``http_status`` (413 for a picture too small,
    415 for an unsupported format, 422 for bad input) and ``hints`` (what to do
    next). Discarding both was a real bug: a picture that cannot hold the payload
    is the *user's* choice, and reporting it as 502 sends them to check their
    network instead of picking a bigger picture.

    Anything unrecognised stays 502 -- that genuinely is our fault, and pretending
    otherwise would hide bugs.
    """
    hints = tuple(getattr(exc, "hints", ()) or ())
    detail = str(exc) or type(exc).__name__
    if hints:
        # 前端把 detail 原样显示，所以"怎么办"要拼进去，而不是丢掉。
        detail = "\n".join((detail, *hints))

    # ValueError 先判：``InvalidInput`` 既是 StegoError（http_status=422）也是
    # ValueError，而它表达的是"当前状态不允许这个操作"（未握手、队列空），
    # 409 比 422 准确 —— 请求本身没写错，是时机不对。
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, StegoError):
        status = int(getattr(exc, "http_status", 422) or 422)
        # StegoError 默认 500 表示"我们的错"，这里保留 fallback 语义。
        return HTTPException(status_code=status if status != 500 else fallback,
                            detail=detail)
    return HTTPException(status_code=fallback, detail=detail)


def entry_payload(entry) -> dict:
    """One timeline entry, in the shape the browser renders.

    Images go over as data URLs: the client never writes them to disk, so there
    is no URL to hand out. Base64 in JSON is the honest encoding for that.
    """
    return {
        # ``seq`` is the arrival counter the browser uses for incremental
        # rendering: it appends entries with a larger seq instead of rebuilding
        # the whole thread every poll.
        "seq": entry.seq,
        "at": entry.at,
        "mine": entry.mine,
        "text": entry.text,
        "images": [data_url(raw) for raw in entry.images],
        "covert": list(entry.covert),
        "unsupported": list(entry.unsupported),
        "notes": list(entry.notes),
    }


def data_url(raw: bytes) -> str:
    """Guess an image's MIME from its first bytes and inline it."""
    mime = "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    elif raw[:2] == b"BM":
        mime = "image/bmp"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
