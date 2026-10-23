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


@router.post("/client/queue/import")
async def queue_import(payload: dict, account: Account = Depends(get_account)
                       ) -> JSONResponse:
    """把用户拖进来/粘贴的图片插到队首。

    接受 data URL 数组（拖拽和剪贴板都是这个形式）。图片**只留在内存**，
    不写磁盘 —— 项目坚持零持久化。

    插到**最前**而不是队尾：用户刚选的就是这次想用的。放队尾会排到第 20 位
    永远轮不到，那比不允许导入更让人困惑。
    """
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        raise HTTPException(status_code=422, detail="images 必须是非空数组")

    added, rejected = 0, []
    for index, item in enumerate(images):
        try:
            raw = media_service.decode_data_url(str(item))
        except Exception as exc:                    # noqa: BLE001 - 用户给的数据
            rejected.append(f"第 {index + 1} 张：{exc}")
            continue
        entry = account.queue.insert_front(raw, name=str(payload.get("name") or ""))
        if entry is None:
            rejected.append(f"第 {index + 1} 张：不是可用的图片（太小或格式不支持）")
        else:
            added += 1

    event("client", "queue_import", added=added, rejected=len(rejected))
    if added == 0:
        raise HTTPException(
            status_code=422,
            detail="；".join(rejected) or "没有可用的图片")
    return JSONResponse({"ok": True, "added": added, "rejected": rejected,
                         **account.queue.stats(),
                         "items": media_service.queue_items(account)})
