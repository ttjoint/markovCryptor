"""设置读写与原生文件选择器。

配置放 %APPDATA%，不落库；其余一切都在内存里。
"""

from __future__ import annotations

import logging
import threading

from pathlib import Path

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
    })


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


@router.post("/client/settings/pick-folder")
async def pick_folder(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
    """Open a native folder chooser on the machine running the server.

    Typing an absolute path into a text box is the most error-prone step for
    a normal user, and the server runs on the same machine as the browser, so
    a real dialog is both possible and much better.
    """
    title = str(payload.get("title") or "选择文件夹")
    chosen = picker_service.pick_folder(title)
    if chosen is None:
        return JSONResponse({"ok": False, "cancelled": True, "path": ""})
    return JSONResponse({"ok": True, "path": chosen})


@router.post("/client/settings/pick-file")
async def pick_file(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
    """Native file chooser -- used for corpus and model files.

    Typing a full path by hand was the only way before, which is exactly the
    step users get wrong.
    """
    title = str(payload.get("title") or "选择文件")
    spec = str(payload.get("filter") or "")
    chosen = picker_service.pick_file(title, spec)
    if chosen is None:
        return JSONResponse({"ok": False, "cancelled": True, "path": ""})
    return JSONResponse({"ok": True, "path": chosen})

# -------------------------------------------------------------- 模型管理
