"""连 OneBot WebSocket 的一点点胶水。

``websockets`` 从 14 起删掉了 ``legacy`` 实现，鉴权头参数也从
``extra_headers`` 改名叫 ``additional_headers``。本库要同时跑在新旧
版本上（用户机器上装的是什么版本我们控制不了），所以统一走这里。
"""

from __future__ import annotations

from typing import Any


def auth_header(token: str) -> dict[str, str]:
    """OneBot 的鉴权头。空 token 就不带 —— 带个空的反而可能被拒。"""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _header_kwarg() -> str:
    """看当前 ``websockets`` 认哪个参数名。"""
    import inspect

    import websockets

    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):        # 有些版本的 connect 是惰性对象
        return "additional_headers"
    return "extra_headers" if "extra_headers" in params else "additional_headers"


def connect(url: str, token: str = "", **kwargs: Any):
    """``websockets.connect`` 的版本无关包装。

    返回 async context manager，用法和原生的一致。
    """
    import websockets

    headers = auth_header(token)
    if not headers:
        return websockets.connect(url, **kwargs)
    return websockets.connect(url, **{_header_kwarg(): headers}, **kwargs)
