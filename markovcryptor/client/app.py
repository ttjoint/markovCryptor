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
import threading
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..core.errors import StegoError
from ..observability import event
from . import config
from .account import Account
from . import tasks as tasks_mod
from .services import history as history_service
from .services import media as media_service
from .services import picker as picker_service
from .api import media, messages, models, session, settings

#: High, unassigned, and distinct from the workbench (8731) so both can run.
DEFAULT_CLIENT_PORT = 8741

DEFAULT_CLIENT_HOST = "127.0.0.1"


def create_client_app(account: Account, *, webui_dir=None,
                      watchdog=None, on_quit=None) -> FastAPI:
    """Build the ASGI app for one logged-in account.

    ``watchdog`` 让后端知道前端还活着；``on_quit`` 是用户主动点"退出"时
    要跑的回调（通常是把 SnowLuma 一起收掉）。
    """
    app = FastAPI(title="markovcryptor client",
                  description="单账号 QQ 混淆通信客户端（仅本机回环）")
    app.state.account = account
    app.state.watchdog = watchdog
    app.state.on_quit = on_quit
    app.state.quit_requested = False
    # 发送任务的调度器：按会话串行、全局池并发、支持取消。
    # 复用 account 上的那个调度器：接收入闸必须和发送共用一个账本。
    app.state.tasks = getattr(account, "tasks", None) or tasks_mod.TaskManager()

    # 每个资源一个路由器。顺序无关紧要（路径不重叠），
    # 但静态文件必须最后 mount，否则会遮住上面的 API。
    for module in (session, messages, media, models, settings):
        app.include_router(module.router)

    if webui_dir is not None:
        # Mounted last so it does not shadow the API routes above.
        app.mount("/", StaticFiles(directory=str(webui_dir), html=True),
                  name="webui")

    return app


#: 缩略图边长。队列里一次最多十张，原图直接内联会让 /client/state 变成
#: 几 MB 的响应，而 2.5 秒轮询一次。
_THUMB_SIZE = 96

