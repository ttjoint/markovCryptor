"""The launcher: from a double-click to a chat window, with no console arguments.

Order matters, and every step exists because skipping it produced a confusing
failure:

1. **Find this machine's QQ.** SnowLuma writes ``config/onebot_<QQ号>.json`` once
   it hooks a logged-in QQ, and the *filename is the account*. So we never ask
   the user for their number -- we wait for that file to appear. Asking would be
   redundant and would produce a "config not found" error that looks like a
   broken install.
2. **Start SnowLuma** if its port is not already listening. Reusing a running
   instance matters: the user may already have started it, and a second copy
   fails with ``EADDRINUSE``.
3. **Wait for OneBot to answer** before building the account, otherwise the first
   ``login_info`` call races the socket (``OneBotNetwork.start()`` only spawns a
   thread).
4. **Serve and open the browser.**

What this module deliberately does *not* do: create or edit the OneBot config.
That file is SnowLuma's own, and rewriting it would fight the program that
generates it. If it is missing, the honest message is "log into QQ first".
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..observability import event

#: How long to wait for a QQ to be detected. Long, because it is a human action:
#: the user has to type a password or scan a QR code.
DEFAULT_LOGIN_WAIT = 300.0
#: How long to wait for OneBot's WebSocket to start answering.
DEFAULT_CONNECT_WAIT = 30.0


class LauncherError(RuntimeError):
    """Something the user has to fix. Carries an actionable hint."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class Endpoint:
    """Where this machine's OneBot lives."""

    uin: str
    url: str
    token: str = ""

    @property
    def spec(self) -> str:
        """The ``UIN=WS;TOKEN`` form the CLI takes."""
        return f"{self.uin}={self.url};{self.token}" if self.token else f"{self.uin}={self.url}"


def discover_endpoint(snowluma: Path) -> Endpoint | None:
    """Read the OneBot endpoint out of SnowLuma's own generated config.

    Returns ``None`` while no QQ has been hooked yet -- that is the normal state
    before login, not an error.
    """
    config_dir = Path(snowluma) / "config"
    if not config_dir.is_dir():
        return None
    for path in sorted(config_dir.glob("onebot_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            event("launcher", "config_unreadable", level=logging.WARNING,
                  path=str(path), error=exc)
            continue
        networks = data.get("networks") or {}
        servers = networks.get("wsServers") or []
        if not servers:
            continue
        first = servers[0] or {}
        port = first.get("port")
        if not port:
            continue
        # 文件名就是账号 —— 这是"不用输 QQ 号"的全部依据。
        uin = path.stem.replace("onebot_", "")
        host = first.get("host") or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return Endpoint(uin=uin,
                        url=f"ws://{host}:{int(port)}/",
                        token=str(first.get("accessToken") or ""))
    return None


def wait_for_endpoint(snowluma: Path, *, timeout: float = DEFAULT_LOGIN_WAIT,
                      on_wait=None) -> Endpoint:
    """Poll until SnowLuma has written a config, i.e. until QQ is logged in.

    Raises :class:`LauncherError` with a message that says what to *do*, because
    the only cause is "QQ is not logged in" and that is fixable in ten seconds.
    """
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        endpoint = discover_endpoint(snowluma)
        if endpoint is not None:
            return endpoint
        if not announced and on_wait is not None:
            on_wait()
            announced = True
        time.sleep(1.0)
    raise LauncherError(
        f"等了 {timeout:.0f} 秒，没有检测到已登录的 QQ。",
        hint=("先打开 QQ 并登录，再启动本程序。"
              "SnowLuma 只能挂到已登录的 QQ 进程上，它不能替你输密码。"),
    )


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_of(url: str) -> int:
    """Port out of ``ws://host:port/``."""
    tail = url.split("//", 1)[-1]
    hostport = tail.split("/", 1)[0]
    _, _, port = hostport.rpartition(":")
    return int(port) if port.isdigit() else 0


def start_snowluma(snowluma: Path, *, log_dir: Path | None = None) -> subprocess.Popen | None:
    """Start SnowLuma in the background unless it is already running.

    Returns the process, or ``None`` when an existing instance was reused. The
    caller must not assume it owns the process.
    """
    snowluma = Path(snowluma)
    index = snowluma / "index.mjs"
    if not index.exists():
        raise LauncherError(
            f"包里缺少 SnowLuma：{index}",
            hint="交付包不完整，请重新解压。",
        )

    node = _find_node(snowluma)
    if node is None:
        raise LauncherError(
            "找不到 node.exe。",
            hint="交付包应自带 node，请确认没有被杀毒软件删掉。",
        )

    log_dir = log_dir or (snowluma / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launcher-snowluma.log"

    env = dict(os.environ)
    # These let SnowLuma start without a browser round-trip: the agreements and
    # the WebUI password are supplied up front. Without them it waits for a human
    # to click through a page, which a double-click launch cannot do.
    env.setdefault("SNOWLUMA_ACCEPT_EULA", "1")
    env.setdefault("SNOWLUMA_ACCEPT_PRIVACY", "1")
    env.setdefault("SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD", "mkx-webui-1")

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        with open(log_path, "ab") as log:
            process = subprocess.Popen(
                [node, str(index)],
                cwd=str(snowluma),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
            )
    except OSError as exc:
        raise LauncherError(f"启动 SnowLuma 失败：{exc}",
                            hint=f"看日志 {log_path}") from None

    event("launcher", "snowluma_started", pid=process.pid, log=str(log_path))
    return process


def _find_node(snowluma: Path) -> str | None:
    """Bundled node first, then PATH.

    Bundled first on purpose: a machine may have an ancient node on PATH, and
    picking that over the one shipped with the package would be a mystery
    failure.
    """
    for candidate in (snowluma.parent / "node" / "node.exe",
                      snowluma / "node.exe",
                      snowluma.parent / "node.exe"):
        if candidate.exists():
            return str(candidate)
    return shutil.which("node")


def wait_for_onebot(url: str, *, timeout: float = DEFAULT_CONNECT_WAIT) -> bool:
    """Wait until OneBot's port accepts connections."""
    port = _port_of(url)
    if not port:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open("127.0.0.1", port):
            return True
        time.sleep(0.4)
    return False


def describe(endpoint: Endpoint, browser_label: str, port: int) -> str:
    """The one block of text the launcher prints on success."""
    return (
        f"就绪\n"
        f"  本机 QQ   : {endpoint.uin}\n"
        f"  OneBot WS : {endpoint.url}\n"
        f"  界面      : http://127.0.0.1:{port}/\n"
        f"  浏览器    : {browser_label}\n"
    )
