"""The local HTTP API behind the client UI.

Separate from ``markovcryptor.web`` on purpose: that one is the steganography
workbench and the alice/bob demo. This one serves a chat client with a friend
list, so sharing the module would mean one app carrying two unrelated route
sets.

Loopback only, no auth token, and deliberately **not** on port 8731 -- that is
the workbench's port, and both may be running at once while testing.
"""

from __future__ import annotations

import base64
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.errors import StegoError
from ..observability import event
from .account import Account

#: High, unassigned, and distinct from the workbench (8731) so both can run.
DEFAULT_CLIENT_PORT = 8741

DEFAULT_CLIENT_HOST = "127.0.0.1"


def _http_error(exc: BaseException, *, fallback: int = 502) -> HTTPException:
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


def _entry_payload(entry) -> dict:
    """One timeline entry, in the shape the browser renders.

    Images go over as data URLs: the client never writes them to disk, so there
    is no URL to hand out. Base64 in JSON is the honest encoding for that.
    """
    return {
        "at": entry.at,
        "mine": entry.mine,
        "text": entry.text,
        "images": [_data_url(raw) for raw in entry.images],
        "covert": list(entry.covert),
        "unsupported": list(entry.unsupported),
        "notes": list(entry.notes),
    }


def _data_url(raw: bytes) -> str:
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


def create_client_app(account: Account, *, webui_dir=None) -> FastAPI:
    """Build the ASGI app for one logged-in account."""
    app = FastAPI(title="markovcryptor client",
                  description="单账号 QQ 混淆通信客户端（仅本机回环）")
    app.state.account = account

    @app.get("/client/state")
    def state() -> JSONResponse:
        """Everything the shell needs to draw itself."""
        return JSONResponse({
            "ok": True,
            "uin": account.uin,
            "ready": account.ready,
            "unread_total": account.conversations.unread_total(),
            "queue": account.queue.stats(),
            "models": (account.models.status() if account.models is not None
                       else {"total": 0, "loaded": 0, "folder": "",
                             "names": [], "loading": False}),
            "text_channel": account.text_channel_usable,
            "friends": [
                {"uin": friend.uin, "name": friend.display_name}
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
                }
                for conversation in account.conversations.ordered()
            ],
        })

    @app.get("/client/conversation/{peer}")
    def conversation_detail(peer: str, history: int = 0) -> JSONResponse:
        """Open one conversation, optionally pulling recent QQ history first.

        History is fetched on open, not at startup: prefetching thirty friends
        would be slow and is more data than the user asked for.
        """
        conversation = account.conversations.ensure(peer)
        if history > 0:
            _merge_history(account, conversation, peer, history)
        return JSONResponse({
            "ok": True,
            "peer": conversation.peer,
            "name": account.directory.name_of(peer),
            "established": conversation.established,
            "state_label": conversation.state_label,
            "entries": [_entry_payload(e) for e in conversation.snapshot()],
        })

    @app.post("/client/conversation/{peer}/open")
    def open_conversation(peer: str) -> JSONResponse:
        """Mark the conversation as being read; clears its red dot."""
        conversation = account.conversations.ensure(peer)
        conversation.mark_open()
        return JSONResponse({"ok": True, "unread": conversation.unread})

    @app.post("/client/conversation/{peer}/close")
    def close_conversation(peer: str) -> JSONResponse:
        conversation = account.conversations.get(peer)
        if conversation is not None:
            conversation.mark_open(False)
        return JSONResponse({"ok": True})

    @app.post("/client/send")
    async def send(payload: dict) -> JSONResponse:
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

        try:
            if mode == "text":
                account.send_text(peer, text)
            elif mode == "covert":
                account.send_covert(peer, text)
            else:
                account.start_handshake(peer)
        except HTTPException:
            raise
        except (StegoError, ValueError) as exc:
            event("client", "send_rejected", level=logging.INFO,
                  peer=peer, mode=mode, error=exc)
            raise _http_error(exc) from None
        except Exception as exc:                    # noqa: BLE001
            event("client", "send_failed", level=logging.ERROR,
                  peer=peer, mode=mode, error=exc)
            raise _http_error(exc, fallback=502) from None

        conversation = account.conversations.ensure(peer)
        return JSONResponse({
            "ok": True,
            "state_label": conversation.state_label,
            "entries": [_entry_payload(e) for e in conversation.snapshot()[-3:]],
        })

    @app.get("/client/history/{peer}")
    def history(peer: str, count: int = 20) -> JSONResponse:
        """Fetch recent messages from QQ without merging into the timeline."""
        try:
            parsed = account.load_history(peer, count=count)
        except (StegoError, ValueError) as exc:
            # 非法 QQ 号是用户输入问题（409/422），不是服务器故障。
            event("client", "history_rejected", level=logging.INFO,
                  peer=peer, error=exc)
            raise _http_error(exc) from None
        except Exception as exc:                    # noqa: BLE001
            event("client", "history_failed", level=logging.WARNING,
                  peer=peer, error=exc)
            raise _http_error(exc, fallback=502) from None
        return JSONResponse({
            "ok": True,
            "messages": [
                {"text": item.text,
                 "unsupported": list(item.unsupported),
                 "images": [ref.summary or "" for ref in item.images]}
                for item in parsed
            ],
        })

    @app.get("/client/queue")
    def queue_state() -> JSONResponse:
        return JSONResponse({"ok": True, **account.queue.stats()})

    @app.post("/client/queue/refill")
    def queue_refill() -> JSONResponse:
        """Top the picture queue up and retry any handshake left waiting."""
        added = account.queue.replenish()
        answered = account.retry_handshakes()
        return JSONResponse({"ok": True, "added": added, "answered": answered,
                             **account.queue.stats()})

    @app.get("/client/models")
    def models_state() -> JSONResponse:
        """What models are available, and whether the text channel can send."""
        if account.models is None:
            return JSONResponse({"ok": True, "total": 0, "loaded": 0,
                                 "names": [], "folder": "", "loading": False,
                                 "usable": False})
        return JSONResponse({"ok": True, **account.models.status(),
                             "usable": account.models.usable})

    @app.post("/client/models/reload")
    def models_reload() -> JSONResponse:
        """Re-scan the folder and load anything new.

        Needed after the user drops a ``.json`` model into the folder: without
        this they would have to restart the program, and nothing in the UI would
        explain why the new model "did not work".
        """
        if account.models is None:
            raise HTTPException(status_code=409,
                                detail="没有配置模型目录")
        before = account.models.loaded_count
        account.models.scan()
        account.models.load_in_background()
        return JSONResponse({"ok": True, "loaded_before": before,
                             **account.models.status()})

    @app.post("/client/models/select")
    async def models_select(payload: dict) -> JSONResponse:
        """Choose which model encodes outgoing messages.

        Receiving still tries every loaded model, so this only affects what our
        own messages look like -- not what we can read.
        """
        name = str(payload.get("name") or "").strip()
        if account.models is None:
            raise HTTPException(status_code=409, detail="没有配置模型目录")
        channel = account.conversations.text_channel
        if channel is None or not hasattr(channel, "use"):
            raise HTTPException(status_code=409, detail="文本信道不可用")
        chosen = next((m for m in account.models.loaded_models()
                       if (getattr(m, "name", "") or "") == name), None)
        if chosen is None:
            raise HTTPException(status_code=404,
                                detail=f"没有已加载的模型叫 {name!r}")
        channel.use(chosen)
        return JSONResponse({"ok": True, "selected": name})

    if webui_dir is not None:
        # Mounted last so it does not shadow the API routes above.
        app.mount("/", StaticFiles(directory=str(webui_dir), html=True),
                  name="webui")

    return app


def _merge_history(account: Account, conversation, peer: str, count: int) -> None:
    """Prepend QQ's own history as visible entries, oldest first.

    Only messages that carry something renderable are added. A history full of
    stickers would otherwise push a wall of "对方发送了 表情包" into the window
    the moment the user opens it.
    """
    from .conversations import Entry

    try:
        parsed = account.load_history(peer, count=count)
    except Exception as exc:                        # noqa: BLE001
        event("client", "history_merge_failed", level=logging.WARNING,
              peer=peer, error=exc)
        return

    merged: list = []
    for item in parsed:
        if not (item.text.strip() or item.images or item.unsupported):
            continue
        # 归属由 QQ 自己的 sender 决定，不能一律当"对方"。
        # 硬编码 mine=False 会让用户自己发过的话也显示在左边、和对方一模一样,
        # 界面上根本分不出谁说的 —— 这正是实际反馈的问题。
        merged.append(Entry(
            mine=bool(account.uin) and item.sender == account.uin,
            text=item.text,
            unsupported=item.unsupported,
            at=item.at or 0.0,
        ))
    # 去重与插入在会话的锁内一次完成：先快照再切片赋值会抹掉期间到达的
    # 新消息，而"检查标志位 + 插入"分开做会让并发打开灌两遍历史。
    conversation.prepend_history(merged)
