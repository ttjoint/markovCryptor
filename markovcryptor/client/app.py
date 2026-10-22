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
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.errors import StegoError
from ..observability import event
from . import config
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
        # ``seq`` is the arrival counter the browser uses for incremental
        # rendering: it appends entries with a larger seq instead of rebuilding
        # the whole thread every poll.
        "seq": entry.seq,
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
            "queue": {**account.queue.stats(), "items": _queue_items(account)},
            "models": (account.models.status() if account.models is not None
                       else {"total": 0, "loaded": 0, "folder": "",
                             "names": [], "loading": False}),
            "text_channel": account.text_channel_usable,
            # 队列装得下握手吗？界面靠它在未握手时决定能否点"发起握手"。
            "handshake_capacity": account.covert_capacity(need_handshake=True),
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

        # 明文可以带图：QQ 允许只发图、也允许图文同一条。
        images = payload.get("images") or []
        raw_images: tuple[bytes, ...] = ()
        if images:
            if not isinstance(images, list):
                raise HTTPException(status_code=422, detail="images 必须是数组")
            raw_images = tuple(_decode_data_url(str(item)) for item in images)
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
        """Queue stats plus a thumbnail of every pending picture.

        The user needs to see *which* pictures are queued, not just how many:
        "8/8" tells them nothing about whether the right photo is next.
        """
        return JSONResponse({"ok": True, **account.queue.stats(),
                             "items": _queue_items(account)})

    @app.post("/client/queue/remove")
    async def queue_remove(payload: dict) -> JSONResponse:
        """Drop one picture from the queue without sending it."""
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="index 必须是整数") from None
        if not account.queue.remove(index):
            raise HTTPException(status_code=404, detail=f"队列里没有第 {index} 张")
        return JSONResponse({"ok": True, **account.queue.stats(),
                             "items": _queue_items(account)})

    @app.post("/client/queue/refill")
    def queue_refill() -> JSONResponse:
        """Top the picture queue up and retry any handshake left waiting."""
        added = account.queue.replenish()
        answered = account.retry_handshakes()
        return JSONResponse({"ok": True, "added": added, "answered": answered,
                             **account.queue.stats(),
                             "items": _queue_items(account)})

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

    # ------------------------------------------------------------------ 设置

    @app.get("/client/settings")
    def settings_get() -> JSONResponse:
        """Current configuration, plus where it is stored.

        The path is returned so the UI can show it: users reasonably want to know
        what a program wrote where, and "it is somewhere in AppData" is not an
        answer.
        """
        cfg = config.ClientConfig.load()
        return JSONResponse({
            "ok": True,
            "path": str(config.default_config_path()),
            "cover_folders": [str(p) for p in cfg.cover_folders],
            "usable_folders": [str(p) for p in cfg.usable_cover_folders()],
            "max_queue": cfg.max_queue,
            "pick": cfg.pick,
            "model_folder": str(cfg.model_folder) if cfg.model_folder else "",
            "port": cfg.port,
            "open_browser": cfg.open_browser,
        })

    @app.post("/client/settings")
    async def settings_set(payload: dict) -> JSONResponse:
        """Persist settings and apply the ones that can change at runtime.

        The picture queue is rebuilt in place rather than restarting: pointing at
        a different folder and being told "restart to apply" is a bad experience
        for the single most likely thing a new user does.
        """
        cfg = config.ClientConfig.load()
        if "cover_folders" in payload:
            raw = payload.get("cover_folders") or []
            if not isinstance(raw, list):
                raise HTTPException(status_code=422, detail="cover_folders 必须是列表")
            cfg.cover_folders = [Path(str(p)).expanduser()
                                 for p in raw if str(p).strip()]
        if "max_queue" in payload:
            try:
                cfg.max_queue = max(1, int(payload["max_queue"]))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422,
                                    detail="max_queue 必须是整数") from None
        if "pick" in payload:
            pick = str(payload["pick"]).lower()
            if pick not in ("random", "sequential"):
                raise HTTPException(status_code=422,
                                    detail="pick 只能是 random 或 sequential")
            cfg.pick = pick
        if "model_folder" in payload:
            folder = str(payload["model_folder"] or "").strip()
            cfg.model_folder = Path(folder).expanduser() if folder else None
        if "open_browser" in payload:
            cfg.open_browser = bool(payload["open_browser"])

        if not cfg.save():
            raise HTTPException(
                status_code=500,
                detail=f"配置文件写不进去：{config.default_config_path()}")

        # 立刻生效：换图片文件夹后队列要重新取样。
        usable = cfg.usable_cover_folders()
        account.queue.folders = usable
        account.queue.max_items = cfg.max_queue
        account.queue.pick = cfg.pick
        account.queue.clear()
        account.queue.replenish()

        # 换模型目录后重新扫描。
        if "model_folder" in payload:
            from .models import ModelLibrary
            if cfg.model_folder is not None and cfg.model_folder.is_dir():
                account.models = ModelLibrary(cfg.model_folder)
                account.models.scan()
                account.models.load_in_background()
            else:
                account.models = None

        return JSONResponse({"ok": True,
                             "usable_folders": [str(p) for p in usable],
                             "queue": account.queue.stats()})

    @app.post("/client/settings/pick-folder")
    async def pick_folder(payload: dict) -> JSONResponse:
        """Open a native folder chooser on the machine running the server.

        Typing an absolute path into a text box is the most error-prone step for
        a normal user, and the server runs on the same machine as the browser, so
        a real dialog is both possible and much better.
        """
        title = str(payload.get("title") or "选择文件夹")
        chosen = _pick_folder(title)
        if chosen is None:
            return JSONResponse({"ok": False, "cancelled": True, "path": ""})
        return JSONResponse({"ok": True, "path": chosen})

    @app.post("/client/settings/pick-file")
    async def pick_file(payload: dict) -> JSONResponse:
        """Native file chooser -- used for corpus and model files.

        Typing a full path by hand was the only way before, which is exactly the
        step users get wrong.
        """
        title = str(payload.get("title") or "选择文件")
        spec = str(payload.get("filter") or "")
        chosen = _pick_file(title, spec)
        if chosen is None:
            return JSONResponse({"ok": False, "cancelled": True, "path": ""})
        return JSONResponse({"ok": True, "path": chosen})

    # -------------------------------------------------------------- 模型管理

    @app.post("/client/models/train")
    async def models_train(payload: dict) -> JSONResponse:
        """Train a model from a corpus file and save it into the model folder.

        Runs synchronously: a few MB of Chinese text trains in seconds, and
        returning early would let the UI show a model that is not ready yet.
        """
        source = str(payload.get("path") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not source:
            raise HTTPException(status_code=422, detail="缺少语料文件路径")
        corpus_path = Path(source).expanduser()
        if not corpus_path.is_file():
            raise HTTPException(status_code=404,
                                detail=f"语料文件不存在：{corpus_path}")

        folder = _model_folder(account)
        if folder is None:
            raise HTTPException(status_code=409,
                                detail="还没有配置模型目录，请先在设置里指定")

        try:
            size = int(payload.get("vocabulary_size") or 3000)
        except (TypeError, ValueError):
            size = 3000

        try:
            from ..model import MarkovModel

            text = corpus_path.read_text(encoding="utf-8", errors="replace")
            model = MarkovModel.train(text, vocabulary_size=max(64, size),
                                      name=name or corpus_path.stem)
            target = folder / f"{name or corpus_path.stem}.json"
            model.save(target)
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail=f"训练失败：{exc}") from None
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"写入模型失败：{exc}") from None
        except Exception as exc:                    # noqa: BLE001
            event("models", "train_failed", level=logging.ERROR, error=exc)
            raise HTTPException(status_code=500,
                                detail=f"训练失败：{exc}") from None

        account.models.scan()
        account.models.load_in_background()
        return JSONResponse({"ok": True, "path": str(target)})

    @app.post("/client/models/upload")
    async def models_upload(payload: dict) -> JSONResponse:
        """Accept a model file dropped onto the window.

        Two things happen here, in this order:

        1. **Validate it really is a model** -- by loading it. A user dragging a
           random ``.json`` in should get a clear "this is not a model", not a
           file silently copied into the model folder that then shows up as
           "加载失败" with no explanation.
        2. **Save it into the default model folder** so it survives a restart.
           Loading without saving would work until the user closes the window,
           which is worse than useless: they would think it worked.

        The name comes from the payload (the browser has the real filename); we
        never trust it as a path -- only the final component is used.
        """
        data = str(payload.get("data") or "")
        name = str(payload.get("name") or "model.json").strip()
        if not data:
            raise HTTPException(status_code=422, detail="没有收到文件内容")

        raw = _decode_data_url(data)
        if len(raw) > 64 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="模型文件不能超过 64MB")

        # 只取文件名，防止 ../ 之类的路径穿越。
        safe = Path(name).name or "model.json"
        if not safe.lower().endswith(".json"):
            safe += ".json"

        # 先验证：能在临时文件里成功 load 才算模型。
        import tempfile

        from ..model import MarkovModel

        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                model = MarkovModel.load(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:                    # noqa: BLE001 - user file
            raise HTTPException(
                status_code=422,
                detail=f"这不是可用的文本模型：{exc}") from None

        # 保存到默认模型目录（没配过就自动建）。
        from .models import ModelLibrary, default_model_folder

        if account.models is None:
            account.models = ModelLibrary(default_model_folder())
        folder = account.models.ensure_folder()
        target = folder / safe
        try:
            target.write_bytes(raw)
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"保存模型失败：{exc}") from None

        account.models.scan()
        account.models.load_in_background()
        event("models", "uploaded", path=str(target),
              model_name=getattr(model, "name", "") or safe)
        return JSONResponse({"ok": True, "path": str(target),
                             "name": getattr(model, "name", "") or Path(safe).stem})

    @app.post("/client/models/import")
    async def models_import(payload: dict) -> JSONResponse:
        """Copy an existing model file into the model folder."""
        source = str(payload.get("path") or "").strip()
        if not source:
            raise HTTPException(status_code=422, detail="缺少模型文件路径")
        src = Path(source).expanduser()
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在：{src}")

        folder = _model_folder(account)
        if folder is None:
            raise HTTPException(status_code=409,
                                detail="还没有配置模型目录，请先在设置里指定")
        try:
            target = folder / src.name
            if src.resolve() != target.resolve():
                shutil.copy2(src, target)
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"导入失败：{exc}") from None

        account.models.scan()
        account.models.load_in_background()
        return JSONResponse({"ok": True, "path": str(target)})

    @app.post("/client/models/export")
    async def models_export(payload: dict) -> JSONResponse:
        """Copy a model out to a directory the user chooses."""
        name = str(payload.get("name") or "").strip()
        target_dir = str(payload.get("path") or "").strip()
        if not name or not target_dir:
            raise HTTPException(status_code=422, detail="需要模型名和目标目录")
        entries = account.models.entries() if account.models else []
        entry = next((e for e in entries
                      if e.name == name or e.path.stem == name), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"没有模型叫 {name!r}")
        try:
            dest = Path(target_dir).expanduser() / entry.path.name
            shutil.copy2(entry.path, dest)
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"导出失败：{exc}") from None
        return JSONResponse({"ok": True, "path": str(dest)})

    @app.post("/client/models/delete")
    async def models_delete(payload: dict) -> JSONResponse:
        """Delete a model file from the model folder.

        Only files *inside* the configured model folder are eligible: a local API
        should not be able to delete an arbitrary path just because the UI asked.
        """
        name = str(payload.get("name") or "").strip()
        folder = _model_folder(account)
        if folder is None:
            raise HTTPException(status_code=409, detail="还没有配置模型目录")
        entries = account.models.entries()
        entry = next((e for e in entries
                      if e.name == name or e.path.stem == name), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"没有模型叫 {name!r}")
        try:
            entry.path.resolve().relative_to(folder.resolve())
        except ValueError:
            raise HTTPException(status_code=409,
                                detail="只能删除模型目录里的文件") from None
        # 后台加载线程可能正握着这个文件的句柄；Windows 上会 WinError 32。
        # 先等它放手，再删 —— 否则用户刚训练完就删会失败，且提示莫名其妙。
        account.models.wait_idle(timeout=30.0)
        try:
            entry.path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"删除失败（文件可能正被占用）：{exc}") from None
        account.models.scan()
        return JSONResponse({"ok": True})

    if webui_dir is not None:
        # Mounted last so it does not shadow the API routes above.
        app.mount("/", StaticFiles(directory=str(webui_dir), html=True),
                  name="webui")

    return app


#: 缩略图边长。队列里一次最多十张，原图直接内联会让 /client/state 变成
#: 几 MB 的响应，而 2.5 秒轮询一次。
_THUMB_SIZE = 96


def _decode_data_url(value: str) -> bytes:
    """Turn ``data:image/png;base64,...`` into bytes.

    The browser hands pasted and dropped pictures over as data URLs, so the
    server needs to undo that before it has an image at all.
    """
    if value.startswith("data:"):
        header, _, body = value.partition(",")
        if ";base64" not in header:
            raise HTTPException(status_code=422, detail="只支持 base64 编码的图片")
        try:
            raw = base64.b64decode(body, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail="图片数据不是合法 base64") from None
    else:
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail="图片数据不是合法 base64") from None
    if not raw:
        raise HTTPException(status_code=422, detail="图片是空的")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单张图片不能超过 8MB")
    return raw


def _thumbnail(raw: bytes) -> str | None:
    """A small JPEG data URL for the queue list."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            image.thumbnail((_THUMB_SIZE, _THUMB_SIZE))
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(
            buffer.getvalue()).decode("ascii")
    except Exception:                               # noqa: BLE001 - cosmetic
        return None


def _queue_items(account: Account) -> list[dict]:
    """Pending pictures as ``{name, thumb}``, oldest first."""
    items = []
    for item in account.queue.preview():
        items.append({
            "name": Path(item.path).name if getattr(item, "path", None) else "",
            "thumb": _thumbnail(item.raw),
        })
    return items


def _model_folder(account: Account) -> Path | None:
    """The configured model folder, if it exists."""
    lib = account.models
    if lib is None or lib.folder is None:
        return None
    folder = Path(lib.folder)
    return folder if folder.is_dir() else None


def _pick_folder(title: str) -> str | None:
    """Native folder picker, via the Win32 shell API.

    **Not tkinter.** tkinter must own the thread that created its root window,
    and this runs on a uvicorn worker thread -- so ``filedialog.askdirectory()``
    blocks forever, the HTTP request never returns, and the button looks dead.
    That was the actual bug: the dialog never appeared and nothing errored.

    ``SHBrowseForFolder`` has no such constraint. Returns ``None`` when the user
    cancels or the platform is not Windows (the UI then lets them type a path).
    """
    try:
        return _pick_folder_win32(title)
    except Exception as exc:                        # noqa: BLE001 - optional
        event("client", "folder_picker_unavailable", level=logging.WARNING,
              error=exc)
        return None


def _pick_file_win32(title: str, filter: str = "") -> str | None:
    """``GetOpenFileNameW`` -- same pointer caveat as the folder picker."""
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
        ]

    comdlg32 = ctypes.windll.comdlg32
    ole32 = ctypes.windll.ole32

    comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
    comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    ole32.CoInitialize(None)
    try:
        buffer = ctypes.create_unicode_buffer(4096)
        # 过滤器用  分隔，最后再补一个  结束。
        spec = filter or "所有文件 (*.*)\0*.*\0\0"
        info = OPENFILENAMEW()
        info.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        info.hwndOwner = None
        info.lpstrFilter = spec
        info.nFilterIndex = 1
        info.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        info.nMaxFile = len(buffer)
        info.lpstrTitle = title
        # OFN_FILEMUSTEXIST(0x1000) | OFN_PATHMUSTEXIST(0x800)
        info.Flags = 0x1000 | 0x800
        if not comdlg32.GetOpenFileNameW(ctypes.byref(info)):
            return None
        return buffer.value or None
    finally:
        ole32.CoUninitialize()


def _pick_file(title: str, filter: str = "") -> str | None:
    """Native file picker. Returns None when cancelled or unavailable."""
    try:
        return _pick_file_win32(title, filter)
    except Exception as exc:                        # noqa: BLE001 - optional
        event("client", "file_picker_unavailable", level=logging.WARNING,
              error=exc)
        return None


def _pick_folder_win32(title: str) -> str | None:
    """``SHBrowseForFolderW`` with a caller-supplied title."""
    import ctypes
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    # **必须声明指针返回值。** ctypes 的默认 restype 是 c_long（32 位），
    # 64 位进程里 SHBrowseForFolderW 返回的 ITEMIDLIST* 高 32 位会被截断，
    # 再传给 SHGetPathFromIDListW 必然失败 —— 对话框弹出来了、用户也选了，
    # 但函数返回 None，界面表现为"点了没反应"。这是实际反馈的 bug。
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p,
                                             wintypes.LPWSTR]
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    # 对话框要求当前线程已初始化 COM；uvicorn 的工作线程通常没有。
    ole32.CoInitialize(None)
    try:
        display = ctypes.create_unicode_buffer(260)
        info = BROWSEINFOW()
        info.hwndOwner = None
        info.pidlRoot = None
        info.pszDisplayName = display
        info.lpszTitle = title
        # BIF_RETURNONLYFSDIRS(0x1) | BIF_NEWDIALOGSTYLE(0x40)
        # 新版对话框才有可输入路径的地址栏，老式那个很难用。
        info.ulFlags = 0x1 | 0x40
        info.lpfn = None
        info.lParam = 0
        info.iImage = 0

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return None                         # 用户取消

        try:
            path = ctypes.create_unicode_buffer(1024)
            if not shell32.SHGetPathFromIDListW(pidl, path):
                return None
            return path.value or None
        finally:
            # 不释放这块 pidl 就是内存泄漏 —— 每次点一次按钮漏一点。
            ole32.CoTaskMemFree(pidl)
    finally:
        ole32.CoUninitialize()


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
    #: (entry, urls) —— 插入之后再下载，界面先出内容、图随后补上。
    pending_images: list = []

    for item in parsed:
        placeholders = list(item.unsupported)
        urls = [ref.locate() for ref in item.images]
        urls = [u for u in urls if u]

        # 归属由 QQ 自己的 sender 决定，不能一律当"对方"。
        # 硬编码 mine=False 会让用户自己发过的话也显示在左边、和对方一模一样,
        # 界面上根本分不出谁说的 —— 这正是实际反馈的问题。
        mine = bool(account.uin) and item.sender == account.uin

        # **图片能下载时不要加占位文字。**
        # 之前对纯图片消息无条件加一行"对方发送了 图片"，等图片真的下载
        # 出来之后，就变成一条带着图的消息旁边还写着"对方发送了图片"——
        # 重复且荒谬。只有拿不到 URL（真的显示不了）时才留一行说明。
        if item.images and not urls and not item.text.strip():
            who = "我发送了" if mine else "对方发送了"
            placeholders.append(f"{who} 图片（无法加载）")

        if not (item.text.strip() or placeholders or urls):
            continue

        entry = Entry(
            mine=mine,
            text=item.text,
            unsupported=tuple(placeholders),
            at=item.at or 0.0,
        )
        merged.append(entry)
        if urls:
            pending_images.append((entry, urls))

    # 去重与插入在会话的锁内一次完成：先快照再切片赋值会抹掉期间到达的
    # 新消息，而"检查标志位 + 插入"分开做会让并发打开灌两遍历史。
    if conversation.prepend_history(merged) and pending_images:
        _download_history_images(account, pending_images)


def _download_history_images(account: Account, pending: list) -> None:
    """Fetch history pictures in the background and attach them to entries.

    Mutating ``entry.images`` in place keeps each entry's ``seq``, so the browser
    appends the picture instead of rebuilding the whole thread (and losing the
    user's scroll position).
    """
    cache = getattr(account, "images", None)
    if cache is None:
        return

    def worker():
        for entry, urls in pending:
            raws = tuple(raw for raw in (cache.get(u) for u in urls) if raw)
            if raws:
                entry.images = raws

    import threading
    threading.Thread(target=worker, name="history-images", daemon=True).start()
