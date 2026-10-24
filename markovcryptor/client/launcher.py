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


#: SnowLuma 的 WebUI 端口。它写死在 SnowLuma 里，不是我们决定的。
SNOWLUMA_WEBUI_PORT = 5099

#: dev 模式下 SnowLuma 的固定 WebUI 口令（写死在它源码里）。
DEV_PASSWORD = "snowluma-dev"

#: 非 dev 模式时的 bootstrap 口令（见 ``start_snowluma``）。
SNOWLUMA_BOOTSTRAP_PASSWORD = "mkx-webui-1"


def _snowluma_webui_ports(snowluma: Path) -> list[int]:
    """SnowLuma WebUI 可能监听的端口，按可能性排序。

    **它并不固定在 5099。** 日志里实测到过：

        [WebUI] port 5099 is in use, using 5100 instead

    而 ``config/runtime.json`` 里写的仍然是 5099（配置没跟着更新）。
    所以我们既读配置、也扫实际监听，两个来源合起来才不会漏。
    """
    ports: list[int] = []

    # 1) 配置里写的
    try:
        data = json.loads((Path(snowluma) / "config" / "runtime.json")
                          .read_text(encoding="utf-8"))
        port = data.get("webuiPort")
        if isinstance(port, int) and port > 0:
            ports.append(port)
    except Exception:                              # noqa: BLE001 - 没有就是没有
        pass

    # 2) 实际在听的（SnowLuma 可能已经换过端口）
    for candidate in range(SNOWLUMA_WEBUI_PORT, SNOWLUMA_WEBUI_PORT + 12):
        if candidate in ports:
            continue
        try:
            with socket.create_connection(("127.0.0.1", candidate),
                                          timeout=0.15):
                ports.append(candidate)
        except OSError:
            continue

    # 3) 兜底：默认端口
    if SNOWLUMA_WEBUI_PORT not in ports:
        ports.append(SNOWLUMA_WEBUI_PORT)
    return ports


def webui_uins(port: int | None = None, *, snowluma: Path | None = None,
               password: str | None = None,
               timeout: float = 2.0,
               debug=None) -> tuple[str, ...]:
    """已登录的 QQ 号，从 SnowLuma 自己的 WebUI 读。

    **为什么需要这个**：``onebot_<uin>.json`` 的文件名就是账号，可交付包
    不能预置它（那里面带着构建机的真实令牌）。而 SnowLuma 只在 OneBot
    网络已经配好之后才写这个文件 —— 全新安装时它永远不出现，客户端于是
    死等在 ``wait_for_endpoint``，界面停在"正在启动…"。

    用户看到的现象是"识别不到我已登录的 QQ"，实际是缺一个配置文件，
    而这个配置只能由"当前登录的是谁"推出来。

    WebUI 用 Bearer 令牌：先 POST /api/login 换 token，再查 /api/qq-list。
    登录口令就是我们启动它时通过环境变量喂进去的那个。
    返回空元组表示"读不到"（还没登录 / 端口没开 / 口令不对），不抛异常 ——
    调用方会继续正常等待流程。
    """
    if port is None:
        candidates_ports = (_snowluma_webui_ports(snowluma)
                            if snowluma is not None
                            else [SNOWLUMA_WEBUI_PORT])
    else:
        candidates_ports = [int(port)]

    for candidate_port in candidates_ports:
        found = _webui_uins_on(candidate_port, password=password,
                               timeout=timeout, debug=debug)
        if found:
            return found
    return ()


def _webui_uins_on(port: int, *, password: str | None,
                   timeout: float, debug=None) -> tuple[str, ...]:
    """在**一个**端口上试着读已登录的 QQ。读不到就返回空元组。"""
    import urllib.error
    import urllib.request

    base = f"http://127.0.0.1:{int(port)}"

    def _post(path: str, payload: dict) -> dict | None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            base + path, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _get(path: str, token: str) -> dict | None:
        request = urllib.request.Request(
            base + path, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    # 依次尝试：dev 口令 → bootstrap 口令 → 用户可能设过的口令。
    #
    # 只试一个是不够的：``webui.json`` 一旦存在，SnowLuma 就无视 bootstrap
    # 环境变量，那个实例只认文件里存的密码。
    candidates = [password] if password else [DEV_PASSWORD,
                                             SNOWLUMA_BOOTSTRAP_PASSWORD]
    login = None
    token = None
    for candidate in candidates:
        login = _post("/api/login", {"password": candidate})
        token = (login or {}).get("token")
        # **先赋值再判断。** 这里写成"先看上一轮的 token"会打出
        # "登录失败"但后面明明拿到了 token —— 日志自相矛盾比没有日志更糟。
        if debug is not None and not token:
            debug(f"WebUI 登录失败（口令 {candidate!r}）：{str(login)[:100]}")
        event("launcher", "webui_login_try", level=logging.DEBUG,
              port=int(port), ok=bool(token),
              reply=str(login)[:120] if login else "no response")
        if token:
            break
    if not token:
        # **必须留下原因。** 之前这里静默返回空元组，用户只看到"没生成
        # json"，完全无从判断是 WebUI 没起来、口令不对，还是压根没登录。
        #
        # 真实坑：SnowLuma 只在 ``webui.json`` **不存在**时才采纳
        # ``SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD``。一旦那个文件存在（用户
        # 之前跑过一次、或在 WebUI 里改过密码），我们喂的口令就被完全
        # 忽略，登录 401，然后静默失败。
        if debug is not None:
            debug(f"WebUI 登录全部失败：{str(login)[:160]}")
        event("launcher", "webui_login_failed", level=logging.WARNING,
              port=int(port), reason=str(login)[:200] if login else "no response")
        return ()

    listed = _get("/api/qq-list", str(token))
    if listed is None:
        event("launcher", "webui_qq_list_failed", level=logging.WARNING,
              port=int(port))
        return ()

    uins = []
    for item in listed.get("list") or ():
        uin = str((item or {}).get("uin") or "").strip()
        if uin.isdigit():
            uins.append(uin)
    if not uins:
        # 只在 DEBUG 级别记 —— 启动头几秒 SnowLuma 还没 hook 上 QQ，
        # "没有已登录的 QQ"是常态。打到用户看得见的日志里只会让人以为出错了。
        event("launcher", "webui_no_logged_in_qq", level=logging.DEBUG,
              port=int(port), reply=str(listed)[:160])
    return tuple(uins)


def ensure_onebot_config(snowluma: Path, uin: str, *,
                         port: int = SNOWLUMA_WEBUI_PORT
                         ) -> Path | None:
    """给还没配过 OneBot 的账号补一份配置。

    已存在就**不动**：SnowLuma 可能往里补过字段，重写只会丢用户的改动。
    """
    snowluma = Path(snowluma)
    # SnowLuma 只认 ``onebot_<digits>.json``，别的名字它直接忽略 ——
    # 写一个它不认的文件等于没写。
    if not str(uin).isdigit():
        return None
    target = snowluma / "config" / f"onebot_{uin}.json"
    if target.exists():
        return None
    if not (snowluma / "config").is_dir():
        return None

    from ..onebot_config import OneBotSettings, write_config

    settings = OneBotSettings(uin=uin)
    # 端口递增：同一台机器挂多个号时不能撞（撞了第二个 EADDRINUSE 起不来）。
    used = set()
    for existing in (snowluma / "config").glob("onebot_*.json"):
        try:
            data = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for server in ((data.get("networks") or {}).get("wsServers") or []):
            try:
                used.add(int((server or {}).get("port")))
            except (TypeError, ValueError):
                continue
    offset = 0
    while settings.ws_port + offset * 10 in used:
        offset += 1
    if offset:
        # OneBotSettings 是 frozen 的 —— 只能换一个新实例，不能就地改。
        from dataclasses import replace
        settings = replace(settings,
                           ws_port=settings.ws_port + offset * 10,
                           http_port=settings.http_port + offset * 10)

    path, written = write_config(snowluma, settings)
    if written:
        event("launcher", "onebot_config_created", path=str(path), uin=uin)
        return path
    return None


def wait_for_endpoint(snowluma: Path, *, timeout: float = DEFAULT_LOGIN_WAIT,
                      on_wait=None, on_tick=None) -> Endpoint:
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
        # 每轮给调用方一次机会做别的事（比如替新账号补 OneBot 配置）。
        # 放在 discover 之后：先看配置在不在，再决定要不要补。
        if on_tick is not None:
            try:
                on_tick()
            except Exception as exc:               # noqa: BLE001 - 不能让补配置拖死等待
                event("launcher", "bootstrap_tick_failed", level=logging.DEBUG,
                      error=exc)
            # 补完立刻再看一眼，不必多等一秒。
            endpoint = discover_endpoint(snowluma)
            if endpoint is not None:
                return endpoint
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
    # **WebUI 口令不可靠，所以用 dev 模式。**
    #
    # 踩过：SnowLuma 只在 ``config/webui.json`` **不存在**时才采纳
    # ``SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD``。只要那个文件存在（用户跑过
    # 一次、或在 WebUI 里改过密码），喂进去的口令就被**完全忽略**，
    # 登录 401 —— 于是读不到已登录的 QQ，也就生成不了 onebot 配置，
    # 界面停在"正在启动…"，而且什么日志都没有。
    #
    # ``SNOWLUMA_DEV_MODE=1`` 走固定口令，不看 webui.json，稳定得多。
    # 这是本机回环端口（127.0.0.1），不对外暴露。
    env.setdefault("SNOWLUMA_DEV_MODE", "1")
    env.setdefault("SNOWLUMA_DEV_PASSWORD", DEV_PASSWORD)

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


def restart_snowluma(snowluma: Path, owned) -> None:
    """重启**我们自己起的**那一份 SnowLuma，让它重新读配置。

    为什么必须重启：SnowLuma 只在启动时读一遍 ``config/``。新装用户的
    ``onebot_<uin>.json`` 是我们**在它已经跑起来之后**才写出来的，它会
    把这个文件读一半（日志里有 OneBot 活动）却不起 ws 服务 —— 实测配置
    写着 3001，``netstat`` 里 3001 根本没监听，客户端一直等到超时。

    只动 ``owned`` 里登记过的进程：用户自己开的 SnowLuma 不在里面，
    重启它会打断用户正在用的东西。
    """
    snowluma = Path(snowluma)

    # 先把我们那一条从 owned 里摘掉，避免所有权混乱。
    with owned._lock:                              # noqa: SLF001 - 同包内部协作
        kept = [(label, proc) for label, proc in owned._items
                if label != "SnowLuma"]
        ours = [proc for label, proc in owned._items if label == "SnowLuma"]
        owned._items = kept                        # noqa: SLF001

    for process in ours:
        try:
            process.terminate()
            process.wait(timeout=8.0)
        except Exception:                          # noqa: BLE001 - 尽力而为
            try:
                process.kill()
            except Exception:                      # noqa: BLE001
                pass

    # 等端口真正释放。刚 terminate 完立刻重开常常撞 EADDRINUSE。
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not _port_open("127.0.0.1", SNOWLUMA_WEBUI_PORT):
            break
        time.sleep(0.3)

    # **先起新的再 register。** 顺序反过来的话，start_snowluma 抛异常
    # 就等于"旧的杀了、新的没起、也没人管" —— 那次调试下来后台堆了十几个
    # node，每个都占着数据库，下次启动直接报文件被占用。
    process = start_snowluma(snowluma)
    owned.add("SnowLuma", process)
    event("launcher", "snowluma_restarted")

    # **等它把 WebUI 端口听起来再返回。** 不等的话，调用方紧接着就去
    # discover_endpoint，读到的是还没写完的配置 / 还没起的服务，表现为
    # "OneBot 端口没起来"然后整个启动失败。
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _port_open("127.0.0.1", SNOWLUMA_WEBUI_PORT):
            break
        if process is not None and process.poll() is not None:
            event("launcher", "restart_child_exited",
                  code=process.returncode)
            break
        time.sleep(0.25)
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
