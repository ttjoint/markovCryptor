"""文本模型：列表、重载、切换、训练、上传/导入/导出/删除。

模型不随包分发（用户自有语料 + 版权），所以这里的入口
是"用户自己放进来"。上传时**先校验再落盘**。
"""

from __future__ import annotations

import logging
import threading

from pathlib import Path
import shutil

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


@router.get("/client/models")
def models_state(account: Account = Depends(get_account)) -> JSONResponse:
    """What models are available, and whether the text channel can send."""
    if account.models is None:
        return JSONResponse({"ok": True, "total": 0, "loaded": 0,
                             "names": [], "folder": "", "loading": False,
                             "usable": False})
    return JSONResponse({"ok": True, **account.models.status(),
                         "usable": account.models.usable})


@router.post("/client/models/reload")
def models_reload(account: Account = Depends(get_account)) -> JSONResponse:
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


@router.post("/client/models/select")
async def models_select(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
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


@router.post("/client/models/train")
async def models_train(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
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

    folder = media_service.model_folder(account)
    if folder is None:
        raise HTTPException(status_code=409,
                            detail="还没有配置模型目录，请先在设置里指定")

    try:
        size = int(payload.get("vocabulary_size") or 3000)
    except (TypeError, ValueError):
        size = 3000

    try:
        from ...model import MarkovModel

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


@router.post("/client/models/upload")
async def models_upload(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
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

    raw = media_service.decode_data_url(data)
    if len(raw) > 64 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="模型文件不能超过 64MB")

    # 只取文件名，防止 ../ 之类的路径穿越。
    safe = Path(name).name or "model.json"
    if not safe.lower().endswith(".json"):
        safe += ".json"

    # 先验证：能在临时文件里成功 load 才算模型。
    import tempfile

    from ...model import MarkovModel

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
    from ..models import ModelLibrary, default_model_folder

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


@router.post("/client/models/import")
async def models_import(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
    """Copy an existing model file into the model folder."""
    source = str(payload.get("path") or "").strip()
    if not source:
        raise HTTPException(status_code=422, detail="缺少模型文件路径")
    src = Path(source).expanduser()
    if not src.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{src}")

    folder = media_service.model_folder(account)
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


@router.post("/client/models/export")
async def models_export(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
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


@router.post("/client/models/delete")
async def models_delete(payload: dict, account: Account = Depends(get_account)) -> JSONResponse:
    """Delete a model file from the model folder.

    Only files *inside* the configured model folder are eligible: a local API
    should not be able to delete an arbitrary path just because the UI asked.
    """
    name = str(payload.get("name") or "").strip()
    folder = media_service.model_folder(account)
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
