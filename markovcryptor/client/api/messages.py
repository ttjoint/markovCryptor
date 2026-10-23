"""消息与会话窗口：状态、会话详情、发送、历史。

最核心的一组。/client/state 是界面轮询的主入口。
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


@router.get("/client/state")
def state(request: Request, account: Account = Depends(get_account)) -> JSONResponse:
    """Everything the shell needs to draw itself."""
    return JSONResponse({
        "ok": True,
        "uin": account.uin,
        "ready": account.ready,
        "unread_total": account.conversations.unread_total(),
        "queue": {**account.queue.stats(), "items": media_service.queue_items(account)},
        "models": (account.models.status() if account.models is not None
                   else {"total": 0, "loaded": 0, "folder": "",
                         "names": [], "loading": False}),
        "text_channel": account.text_channel_usable,
        # 只有接管到原生窗口时最小化才有意义（Firefox 标签页跑不了）。
        "native_window": bool(getattr(request.app.state, "window_handle", None)),
        # 队列装得下握手吗？界面靠它在未握手时决定能否点"发起握手"。
        "handshake_capacity": account.covert_capacity(need_handshake=True),
        # 本机身份：界面顶部要显示真实昵称和头像，而不是"本机"两个字。
        "self": {
            "uin": account.uin,
            "name": account.directory.self_name,
            "nickname": account.directory.self_nickname,
            "avatar": account.directory.self_avatar,
            "online": account.online,
        },
        "friends": [
            {"uin": friend.uin, "name": friend.display_name,
             "nickname": friend.nickname, "avatar": friend.avatar}
            for friend in account.directory.ordered()
        ],
        "conversations": [
            {
                "peer": conversation.peer,
                "name": conversation.name,
                "unread": conversation.unread,
                "established": conversation.established,
                "state_label": conversation.state_label,
                "last_at": conversation.last_at,
                # 最近列表也要头像。好友列表里有，但会话的对端不一定是
                # 好友（陌生人也能发消息），所以这里按号码现拼一个兜底。
                "avatar": media_service.peer_avatar(account, conversation.peer),
            }
            for conversation in account.conversations.ordered()
        ],
        # 两个标签页的总数，界面直接显示"最近 (12) / 好友 (29)"。
        "counts": {
            "conversations": len(account.conversations),
            "friends": len(account.directory),
        },
    })


@router.get("/client/conversation/{peer}")
def conversation_detail(peer: str, history: int = 0, account: Account = Depends(get_account)) -> JSONResponse:
    """Open one conversation, optionally pulling recent QQ history first.

    History is fetched on open, not at startup: prefetching thirty friends
    would be slow and is more data than the user asked for.
    """
    conversation = account.conversations.ensure(peer)
    if history > 0:
        history_service.merge_history(account, conversation, peer, history)
    return JSONResponse({
        "ok": True,
        "peer": conversation.peer,
        "name": account.directory.name_of(peer),
        "established": conversation.established,
        "state_label": conversation.state_label,
        "entries": [entry_payload(e) for e in conversation.snapshot()],
    })


@router.post("/client/conversation/{peer}/open")
def open_conversation(peer: str, account: Account = Depends(get_account)) -> JSONResponse:
    """Mark the conversation as being read; clears its red dot."""
    conversation = account.conversations.ensure(peer)
    conversation.mark_open()
    return JSONResponse({"ok": True, "unread": conversation.unread})


@router.post("/client/conversation/{peer}/close")
def close_conversation(peer: str, account: Account = Depends(get_account)) -> JSONResponse:
    conversation = account.conversations.get(peer)
    if conversation is not None:
        conversation.mark_open(False)
    return JSONResponse({"ok": True})


@router.post("/client/send")
async def send(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
    """Send plain text, pictures, or covert text, depending on the body.

    One endpoint with a ``mode`` rather than three endpoints, because the UI
    has one composer and the difference is a toggle, not a separate screen.
    """
    peer = str(payload.get("peer") or "").strip()
    if not peer:
        raise HTTPException(status_code=422, detail="缺少对端 QQ 号")
    mode = str(payload.get("mode") or "text")
    text = str(payload.get("text") or "")

    # 校验先做，放到 try 外面：HTTPException 是 Exception 的子类，
    # 写在 try 里会被下面的兜底 except 抓走，422 变成 502。
    if mode not in ("text", "covert", "handshake"):
        raise HTTPException(status_code=422, detail=f"未知模式：{mode}")

    # 明文可以带图：QQ 允许只发图、也允许图文同一条。
    images = payload.get("images") or []
    raw_images: tuple[bytes, ...] = ()
    if images:
        if not isinstance(images, list):
            raise HTTPException(status_code=422, detail="images 必须是数组")
        raw_images = tuple(media_service.decode_data_url(str(item)) for item in images)
    channel = str(payload.get("channel") or "image")
    if channel not in ("image", "text"):
        raise HTTPException(status_code=422, detail=f"未知信道：{channel}")

    if mode == "text" and not text.strip() and not raw_images:
        raise HTTPException(status_code=422, detail="消息是空的")

    try:
        if mode == "text":
            if raw_images:
                account.send_pictures(peer, raw_images, text=text or None)
            else:
                account.send_text(peer, text)
        elif mode == "covert":
            # 信道由界面上的开关决定，不在服务端猜。
            if channel == "text":
                account.send_covert_text(peer, text)
            else:
                account.send_covert(peer, text)
        else:
            account.start_handshake(peer)
    except HTTPException:
        raise
    except (StegoError, ValueError) as exc:
        event("client", "send_rejected", level=logging.INFO,
              peer=peer, mode=mode, error=exc)
        raise http_error(exc) from None
    except Exception as exc:                    # noqa: BLE001
        event("client", "send_failed", level=logging.ERROR,
              peer=peer, mode=mode, error=exc)
        raise http_error(exc, fallback=502) from None

    conversation = account.conversations.ensure(peer)
    return JSONResponse({
        "ok": True,
        "state_label": conversation.state_label,
        "entries": [entry_payload(e) for e in conversation.snapshot()[-3:]],
    })


@router.get("/client/history/{peer}")
def history(peer: str, count: int = 20, account: Account = Depends(get_account)) -> JSONResponse:
    """Fetch recent messages from QQ without merging into the timeline."""
    try:
        parsed = account.load_history(peer, count=count)
    except (StegoError, ValueError) as exc:
        # 非法 QQ 号是用户输入问题（409/422），不是服务器故障。
        event("client", "history_rejected", level=logging.INFO,
              peer=peer, error=exc)
        raise http_error(exc) from None
    except Exception as exc:                    # noqa: BLE001
        event("client", "history_failed", level=logging.WARNING,
              peer=peer, error=exc)
        raise http_error(exc, fallback=502) from None
    return JSONResponse({
        "ok": True,
        "messages": [
            {"text": item.text,
             "unsupported": list(item.unsupported),
             "images": [ref.summary or "" for ref in item.images]}
            for item in parsed
        ],
    })
