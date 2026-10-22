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

from ..observability import event
from .account import Account

#: High, unassigned, and distinct from the workbench (8731) so both can run.
DEFAULT_CLIENT_PORT = 8741

DEFAULT_CLIENT_HOST = "127.0.0.1"


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
            "entries": [_entry_payload(e) for e in conversation.entries],
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
        except ValueError as exc:
            # 用户的错（队列空了、会话没建立），不是服务器故障。
            # 用 409 而不是 400：状态不对，重试同样的请求也不会成。
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except HTTPException:
            raise
        except Exception as exc:                    # noqa: BLE001
            event("client", "send_failed", level=logging.ERROR,
                  peer=peer, mode=mode, error=exc)
            raise HTTPException(status_code=502, detail=f"发送失败：{exc}") from None

        conversation = account.conversations.ensure(peer)
        return JSONResponse({
            "ok": True,
            "state_label": conversation.state_label,
            "entries": [_entry_payload(e) for e in conversation.entries[-3:]],
        })

    @app.get("/client/history/{peer}")
    def history(peer: str, count: int = 20) -> JSONResponse:
        """Fetch recent messages from QQ without merging into the timeline."""
        try:
            parsed = account.load_history(peer, count=count)
        except Exception as exc:                    # noqa: BLE001
            event("client", "history_failed", level=logging.WARNING,
                  peer=peer, error=exc)
            raise HTTPException(status_code=502, detail=f"读取历史失败：{exc}") from None
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

    if getattr(conversation, "_history_merged", False):
        # 只在首次打开时合并一次，否则每次刷新都会把同一段历史再灌一遍。
        return

    existing = list(conversation.entries)
    merged: list = []
    for item in parsed:
        if not (item.text.strip() or item.images or item.unsupported):
            continue
        merged.append(Entry(
            mine=False,
            text=item.text,
            unsupported=item.unsupported,
        ))
    conversation.entries[:] = merged + existing
    conversation._history_merged = True
