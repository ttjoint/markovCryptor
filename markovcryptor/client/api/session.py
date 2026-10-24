"""会话与生命周期：心跳、SSE、窗口最小化、退出。

这些端点和"这个进程还在不在"有关，与业务数据无关。
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


@router.post("/client/ping")
def ping(request: Request, account: Account = Depends(get_account)) -> JSONResponse:
    """前端心跳。顺带告诉它服务还活着、以及该多久来一次。"""
    watchdog = get_watchdog(request)
    if watchdog is not None:
        watchdog.beat()
    from ..lifecycle import DEFAULT_BEAT_INTERVAL

    return JSONResponse({"ok": True, "interval": DEFAULT_BEAT_INTERVAL})


@router.get("/client/events")
def events(request: Request, account: Account = Depends(get_account)):
    """Server-Sent Events: 后端主动告诉前端"我还活着 / 我要走了"。

    为什么需要它：只靠前端轮询，后端死掉后最多要等一个轮询周期（2.5 秒）
    才被发现，用户看到的是"窗口卡了一会儿才关"。SSE 连接一断，浏览器
    **立刻**触发 onerror —— 不用等下一轮。

    这里只发心跳注释行，不推业务数据：业务数据仍在 /client/state 里轮询，
    那是"画界面"用的；这条流只负责"还连着吗"。
    """
    import asyncio

    async def stream(account: Account = Depends(get_account)):
        while True:
            # 前端还活着就把看门狗喂一下，顺手证明连接有效。
            watchdog = get_watchdog(request)
            if watchdog is not None:
                watchdog.beat()
            if request.app.state.quit_requested:
                yield "event: quit\ndata: bye\n\n"
                return
            # 注释行（以 : 开头）是合法的 SSE 心跳，浏览器不会派发事件，
            # 但会让连接保持活跃、也能立刻感知断开。
            yield ": ping\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/client/window/minimize")
def window_minimize(request: Request, account: Account = Depends(get_account)) -> JSONResponse:
    """最小化窗口。

    网页没有任何办法最小化自己的 OS 窗口 —— 这是浏览器和原生应用的
    边界。所以由后端找到我们自己起的那个 Chromium 窗口，调 Windows API。
    """
    from . import window as window_mod

    hwnd = getattr(request.app.state, "window_handle", None)
    if not hwnd:
        # 还没接管到窗口（非 Chromium、或窗口刚起来）。不是错误：
        # 用户最多是点了没反应，不该弹报错。
        return JSONResponse({"ok": False, "reason": "no_window"})
    return JSONResponse({"ok": window_mod.minimize(hwnd)})


@router.post("/client/focus")
async def focus_client(request: Request) -> JSONResponse:
    """把已经在跑的那个窗口叫到前面。

    用户第二次双击 start.bat 时不该再起一份 —— SnowLuma 一台机器只有一份，
    两份会互相踩。新进程发现旧实例还活着，就调这个接口把它的窗口提上来，
    然后自己退出。
    """
    hwnd = getattr(request.app.state, "window_handle", None)
    if not hwnd:
        return JSONResponse({"ok": False, "reason": "no-window"})
    try:
        import ctypes
        # SW_RESTORE=9：最小化过也能恢复。SetForegroundWindow 需要
        # 前台权限，失败不算错（用户自己点一下就出来了）。
        ctypes.windll.user32.ShowWindow(int(hwnd), 9)
        ctypes.windll.user32.SetForegroundWindow(int(hwnd))
    except Exception as exc:                       # noqa: BLE001 - 尽力而为
        event("client", "focus_failed", level=logging.DEBUG, error=exc)
        return JSONResponse({"ok": False, "reason": "focus-failed"})
    event("client", "focused")
    return JSONResponse({"ok": True})


@router.post("/client/quit")
async def quit_client(request: Request, account: Account = Depends(get_account)) -> JSONResponse:
    """用户点了退出。

    先回响应再触发退出 —— 否则浏览器拿到的是连接被重置，界面上会显示
    一个莫名其妙的错误，而用户明明只是点了"退出"。
    """
    request.app.state.quit_requested = True
    event("client", "quit_requested")

    on_quit = getattr(request.app.state, "on_quit", None)

    def run_quit():
        # 给响应一点时间真的发出去，再开始拆。
        import time
        time.sleep(0.25)
        if on_quit is not None:
            try:
                on_quit()
            except Exception as exc:            # noqa: BLE001 - shutdown path
                event("client", "quit_handler_failed",
                      level=logging.WARNING, error=exc)

    threading.Thread(target=run_quit, name="quit", daemon=True).start()
    return JSONResponse({"ok": True})
