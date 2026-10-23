"""共享依赖：从 app.state 取当前账号与运行时对象。

端点原本闭包捕获 ``account``，那是"一个函数装下整个应用"的根源。
改成依赖注入后，路由器可以是模块级的。
"""

from __future__ import annotations

from fastapi import Request

from ..account import Account


def get_account(request: Request) -> Account:
    return request.app.state.account


def get_watchdog(request: Request):
    return getattr(request.app.state, "watchdog", None)


def get_window_handle(request: Request):
    return getattr(request.app.state, "window_handle", None)
