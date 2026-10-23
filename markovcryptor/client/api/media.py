"""待发图片队列。

队列在服务端内存里，界面只是预览。移除只是从队列拿掉，
不删用户的文件。
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...core.errors import StegoError
from ...observability import event
from .. import config
from ..account import Account
from ..services import history as history_service
from ..services import media as media_service
from ..services import picker as picker_service
from .common import data_url, entry_payload, http_error
from .deps import get_account, get_watchdog, get_window_handle

router = APIRouter()


@router.get("/client/queue")
def queue_state(account: Account = Depends(get_account)) -> JSONResponse:
    """Queue stats plus a thumbnail of every pending picture.

    The user needs to see *which* pictures are queued, not just how many:
    "8/8" tells them nothing about whether the right photo is next.
    """
    return JSONResponse({"ok": True, **account.queue.stats(),
                         "items": media_service.queue_items(account)})


@router.post("/client/queue/remove")
async def queue_remove(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
    """Drop one picture from the queue without sending it."""
    try:
        index = int(payload.get("index"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="index 必须是整数") from None
    if not account.queue.remove(index):
        raise HTTPException(status_code=404, detail=f"队列里没有第 {index} 张")
    return JSONResponse({"ok": True, **account.queue.stats(),
                         "items": media_service.queue_items(account)})


@router.post("/client/queue/refill")
def queue_refill(account: Account = Depends(get_account)) -> JSONResponse:
    """Top the picture queue up and retry any handshake left waiting."""
    added = account.queue.replenish()
    answered = account.retry_handshakes()
    return JSONResponse({"ok": True, "added": added, "answered": answered,
                         **account.queue.stats(),
                         "items": media_service.queue_items(account)})
