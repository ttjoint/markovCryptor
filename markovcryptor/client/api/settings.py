"""设置读写与原生文件选择器。

配置放 %APPDATA%，不落库；其余一切都在内存里。
"""

from __future__ import annotations

import logging
import threading

from pathlib import Path

import asyncio

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


@router.get("/client/settings")
def settings_get(account: Account = Depends(get_account)) -> JSONResponse:
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
        # 只用布尔值：口令本身永远不回前端。
        #
        # 默认口令意味着「任何拿到源码的人都能解出你的握手帧」，所以要点出来。
        # 但提示只在用户没关过时出现 —— 一条每次启动都弹的警告会被无视，
        # 比不提示更糟。
        # 版本号给「系统」页的运行信息用。
        "version": _version(),
        "psk_is_default": _is_default_psk(cfg.psk),
        "psk_notice_dismissed": cfg.psk_notice_dismissed,
    })


def _version() -> str:
    try:
        from ... import __version__
        return str(__version__)
    except Exception:                              # noqa: BLE001 - optional
        return ""


def _is_default_psk(value: str) -> bool:
    """Is this the shipped default passphrase?"""
    return str(value or "").strip().lower() in ("", "pbx1")


@router.post("/client/settings/psk-notice-dismissed")
def dismiss_psk_notice(account: Account = Depends(get_account)) -> JSONResponse:
    """用户关掉了「你在用默认口令」提示 —— 记住，下次别再弹。"""
    cfg = config.ClientConfig.load()
    cfg.psk_notice_dismissed = True
    cfg.save()
    event("client", "psk_notice_dismissed")
    return JSONResponse({"ok": True})


@router.post("/client/settings")
async def settings_set(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
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
        from ..models import ModelLibrary
        if cfg.model_folder is not None and cfg.model_folder.is_dir():
            account.models = ModelLibrary(cfg.model_folder)
            account.models.scan()
            account.models.load_in_background()
        else:
            account.models = None

    return JSONResponse({"ok": True,
                         "usable_folders": [str(p) for p in usable],
                         "queue": account.queue.stats()})


def _watchdog(request: Request):
    """拿看门狗，没有就返回 None（测试/无窗口环境下可能没有）。"""
    return getattr(request.app.state, "watchdog", None)


async def _run_picker_without_starving_the_loop(watchdog, opener):
    """跑原生对话框，同时**别把事件循环的心跳饿死**。

    **实锤闪退**：点「选择文件夹」或「上传图片」→ 对话框弹出 → 用户在里面
    停留一会儿 → 整个程序直接消失，日志停在最后一行没有任何异常。

    原因是两个问题叠在一起：

    1. ``async def`` 里直接调 ``SHBrowseForFolderW``。那是**阻塞**调用，
       事件循环被它占住，期间所有 HTTP 请求（包括前端心跳）都排不上队。
    2. 前端心跳停了，``LivenessWatchdog`` 到 150 秒就认为"窗口关了"，
       调 ``shutdown()`` 收掉进程 —— 用户看到的就是闪退。

    所以两件事都要做：
    * 对话框放到线程里跑（事件循环继续转，心跳照常回）
    * 期间**解除** watchdog 武装（万一线程池也不够快，不能让它误杀）

    注意 ``watchdog.disarm()`` 之后必须 ``beat()`` 重新武装：``disarm``
    把 armed 置为 False，``beat`` 会重新设 True 并刷新时间戳。
    """
    if watchdog is not None:
        watchdog.disarm()

    try:
        # ``asyncio.to_thread`` 用默认线程池，不阻塞事件循环。
        return await asyncio.to_thread(opener)
    finally:
        if watchdog is not None:
            watchdog.beat()


@router.post("/client/settings/pick-folder")
async def pick_folder(payload: dict,
                      account: Account = Depends(get_account),
                      watchdog: object = Depends(_watchdog)) -> JSONResponse:
    """Open a native folder chooser on the machine running the server.

    Typing an absolute path into a text box is the most error-prone step for
    a normal user, and the server runs on the same machine as the browser, so
    a real dialog is both possible and much better.
    """
    title = str(payload.get("title") or "选择文件夹")
    chosen = await _run_picker_without_starving_the_loop(
        watchdog, lambda: picker_service.pick_folder(title))
    if chosen is None:
        return JSONResponse({"ok": False, "cancelled": True, "path": ""})
    return JSONResponse({"ok": True, "path": chosen})


@router.post("/client/settings/pick-file")
async def pick_file(payload: dict,
                    account: Account = Depends(get_account),
                    watchdog: object = Depends(_watchdog)) -> JSONResponse:
    """Native file chooser -- used for corpus and model files.

    Typing a full path by hand was the only way before, which is exactly the
    step users get wrong.
    """
    title = str(payload.get("title") or "选择文件")
    spec = str(payload.get("filter") or "")
    chosen = await _run_picker_without_starving_the_loop(
        watchdog, lambda: picker_service.pick_file(title, spec))
    if chosen is None:
        return JSONResponse({"ok": False, "cancelled": True, "path": ""})
    return JSONResponse({"ok": True, "path": chosen})

# -------------------------------------------------------------- 模型管理
