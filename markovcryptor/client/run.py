"""Double-click entry point: no arguments, no console knowledge required.

Everything the CLI ``client`` subcommand does with explicit flags, this does by
looking around:

1. find SnowLuma next to the executable (or at ``--snowluma``),
2. wait for a logged-in QQ and read its account from the config SnowLuma wrote,
3. start SnowLuma if needed,
4. start the HTTP server and open the browser.

Kept separate from ``cli.py`` so the packaged launcher can call one function
without dragging in argparse, and so the "no flags" path stays readable.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..observability import event
from . import browser as browser_mod
from . import launcher
from . import lifecycle
from .account import Account
from .app import DEFAULT_CLIENT_PORT, create_client_app
from .config import ClientConfig
from . import config as config_mod
from .queue import PictureQueue


#: Where startup messages also go. Under ``pythonw`` there is no console at all,
#: so a message written only to stdout is invisible -- and a launcher that fails
#: silently is the worst possible outcome: the user double-clicks, nothing
#: happens, and they conclude the package is broken.
LOG_NAME = "client.log"


def _log_path() -> Path:
    """Log beside the config, so it is findable without a console."""
    return Path(config_mod.default_config_path()).parent / LOG_NAME


def _log(msg: str) -> None:
    """Write to the console if there is one, and always to a log file.

    The file matters more than the console here: the packaged launcher may run
    with no console at all, and "startup failed" needs to be discoverable
    afterwards. Failure to write the log is ignored -- logging must never be the
    reason the program dies.
    """
    line = msg
    try:
        stream = sys.stdout
        if stream is not None:
            stream.write(line + "\n")
            stream.flush()
    except Exception:                              # noqa: BLE001 - no console
        pass
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:                              # noqa: BLE001 - logging
        pass


def find_snowluma(explicit: Path | None = None) -> Path:
    """Locate SnowLuma relative to this package or the executable.

    Order: explicit, next to the exe, then next to the package. Bundled layouts
    put it beside the launcher; a source checkout has it under ``dist/``.
    """
    if explicit is not None:
        return Path(explicit)
    here = Path(__file__).resolve()
    repo = here.parent.parent.parent          # <root>/markovcryptor/client/..
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        exe_dir / "snowluma",                 # 打包后：launcher 旁边
        exe_dir.parent / "snowluma",
        repo / "snowluma",                    # 源码树：仓库根
        Path.cwd() / "snowluma",
    ]
    # 源码树里 SnowLuma 常放在 dist/<包名>/ 下，取最新的一包。
    dist = repo / "dist"
    if dist.is_dir():
        candidates += sorted(dist.glob("*/snowluma"), reverse=True)
    for candidate in candidates:
        if (candidate / "index.mjs").exists():
            return candidate
    # Return the most likely path so the error message names something real.
    return candidates[0]


def _model_library(cfg: ClientConfig, snowluma_root: Path):
    """Text models to load, or None when there are none configured.

    Two locations, in order: the configured folder, then ``models/`` beside the
    executable (which is where the bundle puts the shipped ones). Returning None
    rather than an empty library keeps "text channel disabled" distinguishable
    from "text channel enabled but nothing loaded".
    """
    from .models import ModelLibrary

    candidates = []
    if cfg.model_folder is not None:
        candidates.append(Path(cfg.model_folder))
    candidates.append(Path(sys.executable).resolve().parent / "models")
    candidates.append(snowluma_root.parent / "models")

    for folder in candidates:
        if folder.is_dir() and any(folder.glob("*.json")):
            _log(f"文本模型目录：{folder}")
            return ModelLibrary(folder)
    _log("没有找到文本模型；只能发送图片密文")
    return None


def _bootstrap_onebot(root: Path, owned=None) -> None:
    """等 QQ 登录的同时，替新账号补一份 OneBot 配置。

    **补完必须重启 SnowLuma。** SnowLuma 在启动时读一遍配置，之后不再
    重读；我们在它已经跑起来之后才写出 ``onebot_<uin>.json``，它会
    把这个文件"读进去一半"（日志里有 OneBot 活动），但**不会真的起
    ws 服务**，于是客户端一直等到超时。实测：配置里写着 3001，netstat
    里 3001 根本没有监听。

    重启一次它就正常了。这只发生在**全新安装的第一次**，之后配置一直在。
    """
    uins = launcher.webui_uins(snowluma=root,
                                debug=lambda msg: _log("  " + msg))
    if not uins:
        # 这一步失败必须让用户看见。原来静默返回空元组，用户只看到
        # "没生成 json"，完全无从判断是 WebUI 没起来、口令不对，
        # 还是那边 QQ 根本没登录。
        # 头几秒读不到是**正常的** —— SnowLuma 还没起完 WebUI。只在
        # 等了一会儿之后还说读不到，才值得告诉用户，否则第一秒就打一行
        # 看着像报错的话，反而让人以为出问题了。
        attempts = getattr(_bootstrap_onebot, "_attempts", 0) + 1
        _bootstrap_onebot._attempts = attempts
        if attempts == 20:                       # 约 20 秒
            _log("  还没等到 QQ 登录；请确认 QQ 已打开并登录了账号")
        return

    created_any = False
    for uin in uins:
        created = launcher.ensure_onebot_config(root, uin)
        if created is not None:
            _log(f"已为 QQ {uin} 创建 OneBot 配置：{created.name}")
            created_any = True

    if not created_any or owned is None:
        return

    # 让 SnowLuma 重新读配置。只重启**我们自己起的**那一份。
    _log("重新启动 SnowLuma 以加载新配置…")
    try:
        process = launcher.restart_snowluma(root, owned)
        if process is not None:
            # 换了新 pid，孤儿记录也要跟着换 —— 否则下次启动会去认一个
            # 早就死掉的 pid。
            _remember_child(process.pid, root=root)
    except Exception as exc:                       # noqa: BLE001
        event("launcher", "restart_after_bootstrap_failed",
              level=logging.WARNING, error=exc)


def run(*, snowluma: Path | None = None, config_path: Path | None = None,
        wait: float = launcher.DEFAULT_LOGIN_WAIT,
        open_browser: bool | None = None,
        owned: "lifecycle.OwnedProcesses | None" = None) -> int:
    """Start everything. Returns a process exit code."""
    from .lifecycle import OwnedProcesses

    owned = owned if owned is not None else OwnedProcesses()

    # 已经有客户端在跑？把它的窗口叫到前面，自己退出。
    #
    # SnowLuma 是"一台机器一份"的资源：它 hook 住 QQ、在固定端口上提供
    # OneBot。两个客户端各起一份的话，第二个会把第一个的杀掉，用户看到
    # 的是"关掉一个窗口，两个都没了"。
    existing = _running_instance()
    if existing is not None:
        pid, port = existing
        _log(f"已经有客户端在运行（pid {pid}），正在打开它的窗口…")
        _focus_existing_window(port)
        return 0

    # 先收掉上次留下的孤儿：它占着 snowluma 的数据库，不收掉这次就
    # "文件被占用"。放在最前面，早于任何读配置的动作。
    _reap_previous_orphan()

    # 崩溃 / 未捕获异常时也要收子进程。atexit 覆盖绝大多数解释器退出；
    # 真正被强杀（任务管理器）时靠下次启动的 _reap_previous_orphan。
    atexit.register(owned.terminate_all)
    cfg = ClientConfig.load(config_path)

    try:
        root = find_snowluma(snowluma)
        _log(f"SnowLuma 目录：{root}")

        # 配置存在 ≠ SnowLuma 在跑：上次退出后文件留着，进程早没了。
        # 所以判断依据是**端口是否在听**，不是配置文件在不在。
        #
        # 而配置文件里的端口也**不可信**：SnowLuma 每次启动会重新分配
        # （3000/3001 之类），上次写的 3001 这次可能是 3000。机器重启后
        # 就撞上过：读到旧配置的 3001，启动后实际在 3000，于是死等 3001
        # 直到超时，报"OneBot 端口没起来"。
        #
        # 因此规则是：旧配置只用来"猜一下是不是已经在跑"，凡是自己启动了
        # 一份，就必须**重新发现**端口，不能用启动前的值。
        previous = launcher.discover_endpoint(root)
        reused = (previous is not None
                  and launcher.wait_for_onebot(previous.url, timeout=1.0))

        if reused:
            endpoint = previous
            _log(f"复用已在运行的 SnowLuma，QQ {endpoint.uin}")
        else:
            if previous is not None:
                _log(f"发现已登录的 QQ {previous.uin}，启动 SnowLuma…")
            else:
                _log("正在等待 QQ 登录…（SnowLuma 只能挂到已登录的 QQ 上）")
            # 起新的之前，先收掉本包遗留的 node。
            #
            # 它们会**死死占着 QQ 的 hook 管道**：新实例的 WebUI 起得来、
            # 端口也在听，但 `[Hook] login detected` 永远不出现，OneBot
            # 端口于是永不打开 —— 表现为 "OneBot 端口没起来"。
            # 实测日志：03:02 那次 hook 成功，之后 03:06、03:08 两次都只
            # 到 "[WebUI] listening" 就再无动静。
            strays = launcher.kill_stray_snowluma(root)
            if strays:
                _log(f"  收掉了 {len(strays)} 个上次遗留的 SnowLuma 进程")

            # 记下来：退出时要收掉。只收**我们自己起的**那一份 ——
            # 用户手动开的 SnowLuma 不能因为关这个客户端就被杀掉。
            process = launcher.start_snowluma(root)
            owned.add("SnowLuma", process)
            if process is not None:
                # 记下来，万一这次没退干净，下次启动能认出来并收掉。
                _remember_child(process.pid, root=root)
            # 启动之后重新读配置：端口几乎肯定变了。
            #
            # **全新安装时那个配置根本不会出现**：`onebot_<uin>.json` 的
            # 文件名就是 QQ 号，交付包不能预置（里面带着构建机的真实令牌），
            # 而 SnowLuma 只在 OneBot 网络配好之后才写它。于是死等，
            # 界面一直停在"正在启动…"，用户以为程序没识别到已登录的 QQ。
            #
            # 所以边等边补：问一下 SnowLuma 现在登录的是谁，替它把配置写出来。
            endpoint = launcher.wait_for_endpoint(
                root, timeout=wait,
                on_tick=lambda: _bootstrap_onebot(root, owned),
                on_wait=lambda: _log("  仍在等待：请打开 QQ 并登录"))

        if not launcher.wait_for_onebot(endpoint.url):
            # 端口对不上是最常见的原因：SnowLuma 起来了，但写在配置里的
            # 端口和它实际监听的不一致（或者它还卡在初始化里）。
            raise launcher.LauncherError(
                f"OneBot 端口没起来：{endpoint.url}",
                hint=("SnowLuma 可能还在初始化，或端口被别的程序占了。"
                      "看 snowluma/logs/launcher-snowluma.log；"
                      "重启本程序通常会重新读到正确端口。"))

    except launcher.LauncherError as exc:
        _log(f"启动失败：{exc}")
        if exc.hint:
            _log(f"  怎么办：{exc.hint}")
        return 2
    except Exception as exc:                       # noqa: BLE001 - startup boundary
        _log(f"启动失败：{exc}")
        return 2

    # 到这里 QQ 一定在线，可以建账号了。
    return _serve(endpoint, cfg, config_path=config_path, root=root,
                  open_browser=open_browser, owned=owned)


# --------------------------------------------------------------------------
# 子进程兜底回收
#
# **实锤**：反复启动调试下来，后台堆了十几个 node.exe，每个都占着
# ``snowluma/data`` 里的数据库 —— 下次启动直接 "文件被占用"。
#
# ``shutdown()`` 只在"正常退出"这条路上跑。异常、Ctrl+C、用户直接关掉
# 控制台窗口、pythonw 崩溃 —— 这些都不会经过它。而 SnowLuma 是独立进程，
# 我们死了它不知道。
#
# 所以再挂两道：
#   1. ``atexit`` —— 覆盖大多数解释器退出（包括未捕获异常）
#   2. 子进程交给 Windows Job 对象 —— 父进程一死，系统连带杀掉
#
# Job 对象在纯 Python 里要 ctypes，容易出错；这里用更朴素但可靠的办法：
# 把 pid 记到磁盘，下次启动先清理上次留下的孤儿。
# --------------------------------------------------------------------------

_ORPHAN_FILE = "snowluma-child.json"


#: 单实例锁文件。记下"当前这个客户端占着哪个端口"。
_INSTANCE_FILE = "client-instance.json"


def _instance_path() -> Path:
    from . import config as config_mod
    return config_mod.default_config_path().parent / _INSTANCE_FILE


def _running_instance() -> tuple[int, int] | None:
    """上一个还活着的客户端实例是谁：(pid, port)。

    **为什么需要它**：SnowLuma 是"一台机器一份"的资源 —— 它 hook 住 QQ，
    在固定端口上提供 OneBot。两个客户端实例各起一份 SnowLuma 的话，
    第二个把第一个的杀掉，用户看到的是"关掉一个窗口，两个都没了"。

    所以新实例启动时先看看有没有活着的旧实例：有就把它的窗口叫到前面，
    自己退出。这比让两份互相踩要可靠得多。
    """
    try:
        raw = _instance_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        pid = int(data.get("pid") or 0)
        port = int(data.get("port") or 0)
    except Exception:                              # noqa: BLE001 - 没有就是没有
        return None
    if pid <= 0 or port <= 0:
        return None

    # pid 还活着、而且**确实是我们这个客户端**（pid 会被复用，必须核对）。
    if not _pid_is_our_client(pid):
        _clear_instance()
        return None

    # 端口也在听，才算是可用的实例。只剩尸体的话清掉记录。
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return pid, port
    except OSError:
        _clear_instance()
        return None


def _pid_is_our_client(pid: int) -> bool:
    """这个 pid 还是不是跑着 markovcryptor.client 的 python 进程。"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
             ".CommandLine"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                              # noqa: BLE001
        return False
    command = (completed.stdout or "").strip()
    if not command:
        return False
    # 只比对 "-m markovcryptor.client.run" 这种形式：Windows 上
    # 命令行里一定有它，路径分隔符是斜杠还是反斜杠都不影响。
    return "markovcryptor.client.run" in command


def _claim_instance(port: int) -> None:
    try:
        path = _instance_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "port": int(port)}),
                        encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001
        event("lifecycle", "instance_claim_failed", level=logging.DEBUG,
              error=exc)


def _clear_instance() -> None:
    try:
        _instance_path().unlink(missing_ok=True)
    except Exception:                              # noqa: BLE001
        pass


def _focus_existing_window(port: int) -> None:
    """把已经在跑的那个窗口叫到前面。"""
    try:
        import urllib.request
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/client/focus", data=b"{}", method="POST")
        urllib.request.urlopen(request, timeout=3)
    except Exception as exc:                       # noqa: BLE001 - 尽力而为
        event("lifecycle", "focus_existing_failed", level=logging.DEBUG,
              error=exc)


def _orphan_path() -> Path:
    from . import config as config_mod
    return config_mod.default_config_path().parent / _ORPHAN_FILE


def _remember_child(pid: int, *, root: Path) -> None:
    """记下我们起的 SnowLuma，供下次启动时清理。"""
    try:
        path = _orphan_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": int(pid), "snowluma": str(root)}),
                        encoding="utf-8")
    except Exception as exc:                        # noqa: BLE001 - 记账失败不致命
        event("lifecycle", "orphan_record_failed", level=logging.DEBUG, error=exc)


def _forget_child() -> None:
    try:
        _orphan_path().unlink(missing_ok=True)
    except Exception:                               # noqa: BLE001
        pass


def _reap_previous_orphan() -> None:
    """上次没退干净的 SnowLuma，这次启动时收掉。

    只在它**确实是我们上次起的那个 pid**、而且命令行里带着同一个
    snowluma 路径时才动手 —— 不能误杀用户自己开的实例。
    """
    try:
        raw = _orphan_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        pid = int(data.get("pid") or 0)
        recorded = str(data.get("snowluma") or "")
    except Exception:                               # noqa: BLE001 - 没有记录是常态
        return

    if pid <= 0:
        _forget_child()
        return

    alive = _pid_is_snowluma(pid, recorded)
    if alive:
        event("lifecycle", "reaping_orphan", pid=pid)
        _log(f"发现上次没退干净的 SnowLuma（pid {pid}），正在收掉")
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:                    # noqa: BLE001
            event("lifecycle", "reap_failed", level=logging.WARNING, error=exc)
    _forget_child()


def _pid_is_snowluma(pid: int, recorded_root: str) -> bool:
    """这个 pid 现在还是不是"我们上次起的那个 SnowLuma"。"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
             ".CommandLine"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        command = (completed.stdout or "").strip()
    except Exception:                               # noqa: BLE001
        return False
    if not command:
        return False                                # 进程已经没了
    if "snowluma" not in command.lower() and "index.mjs" not in command.lower():
        return False                                # pid 被别的程序复用了
    # 路径比较要统一斜杠再比：记录里可能是 ``D:/x/snowluma``，
    # 而命令行里是 ``D:\x\snowluma``。直接比字符串永远不相等。
    def _norm(text: str) -> str:
        return text.replace("//", "/").replace("\\", "/").lower()

    if recorded_root and _norm(recorded_root) not in _norm(command):
        return False                                # 不是同一个 SnowLuma
    return True


def _serve(endpoint, cfg, *, config_path: Path | None, root: Path,
           open_browser: bool | None, owned=None) -> int:
    """Wire up the account and run the UI until it exits.

    Split out of :func:`run` so the part that decides *which port to talk to*
    can be tested on its own -- this half actually opens sockets and blocks on
    uvicorn, which no unit test should do.
    """
    from ..terminal.onebot import OneBotNetwork

    network = OneBotNetwork(endpoint.url, endpoint.uin,
                            token=endpoint.token or None).start()

    folders = cfg.usable_cover_folders()
    if not folders and cfg.cover_folders:
        _log("配置里的图片文件夹没有可用图片；握手将无法发起")
    elif not folders:
        _log("还没有配置图片文件夹；握手将无法发起")

    queue = PictureQueue(folders=folders, max_items=cfg.max_queue, pick=cfg.pick)
    models = _model_library(cfg, root)
    # 指纹册放在配置文件旁边（%APPDATA%\markovcryptor\peers.json）。
    # 这是除配置之外唯一落盘的东西，存的是公钥指纹，不是密钥。
    peers_path = cfg.path.parent / "peers.json" if getattr(cfg, "path", None) else None
    account = Account(network, psk=cfg.psk, queue=queue, models=models,
                      peers_path=peers_path)

    try:
        account.start(wait=launcher.DEFAULT_CONNECT_WAIT)
    except Exception as exc:                       # noqa: BLE001
        _log(f"连不上 QQ：{exc}")
        for hint in getattr(exc, "hints", ()):
            _log(f"  怎么办：{hint}")
        network.stop()
        return 2

    # 记住这次发现到的账号和端口，下次可以少猜。
    cfg.onebot_url = endpoint.url
    cfg.onebot_token = endpoint.token
    cfg.save(config_path)

    # 端口：用户没指定就从 8741 开始往上找空位。
    # 原来写死 8741，被别的程序占了就直接抛 OSError —— 用户看到的是一个
    # 没有任何提示的启动失败，也不知道该改什么。
    preferred = cfg.port or DEFAULT_CLIENT_PORT
    try:
        port = lifecycle.find_free_port(preferred)
    except OSError as exc:
        raise launcher.LauncherError(
            f"找不到可用的界面端口：{exc}",
            hint="在配置里把 port 改成一个别的值，或关掉占用端口的程序。")

    # 记下"这个客户端正在这个端口上"，供下一个实例发现并接管。
    _claim_instance(port)
    atexit.register(_clear_instance)

    if port != preferred:
        owner = lifecycle.port_owner_hint(preferred)
        detail = f"（被 {owner} 占用）" if owner else ""
        _log(f"端口 {preferred} 已被占用{detail}，改用 {port}")

    webui = Path(__file__).resolve().with_name("webui")

    # 前端关掉 / 后端该退出的统一收尾。必须幂等：看门狗、用户点退出、
    # Ctrl+C 三条路都可能触发它。
    shutdown = _make_shutdown(account, network, owned)

    watchdog = lifecycle.LivenessWatchdog(on_expire=shutdown).start()
    app = create_client_app(account, webui_dir=webui,
                            watchdog=watchdog, on_quit=shutdown)
    url = f"http://127.0.0.1:{port}/"

    _log(f"客户端已启动：{url}")
    _log(f"  本机 QQ {account.uin}，{len(account.directory)} 个好友，"
         f"图片队列上限 {queue.max_items}")

    should_open = cfg.open_browser if open_browser is None else open_browser

    import uvicorn

    try:
        # 不用 uvicorn.run()：它装自己的信号处理并且不给我们"主动停"的把手。
        # 用 Server + 一个监视线程，就能在用户点退出时优雅收尾。
        # **pythonw 下 uvicorn 会自己崩掉。**
        #
        # pythonw 是 GUI 子系统程序，``sys.stderr`` 是 ``None``。uvicorn 默认
        # 调用 ``logging.config.dictConfig()`` 去配一个写 stderr 的
        # StreamHandler，拿到 None 就抛：
        #
        #     ValueError: Unable to configure formatter 'default'
        #
        # 然后整个进程退出码 1 —— 日志停在"客户端已启动"，窗口不出现，
        # 用户看到的就是"127.0.0.1 拒绝连接"。用 python.exe 跑完全正常，
        # 所以这个 bug 只在双击 start.bat 时出现。
        #
        # 我们自己有日志（observability），不需要 uvicorn 那套。
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning",
                                log_config=None,
                                access_log=False)
        server = uvicorn.Server(config)

        # **浏览器必须等端口真的在听之后再开。**
        #
        # 原来是"先开浏览器、再 server.run()"，靠谁快谁慢赌 —— 快机器上
        # 看不出问题，冷启动/虚拟机/杀毒扫描时浏览器抢在前面，用户看到
        # 一个 ERR_CONNECTION_REFUSED 的窗口，然后关掉，整个程序以退出码
        # 1 结束。看起来像"启动失败"，其实是打开得太早。
        if should_open:
            threading.Thread(target=_open_when_ready,
                             args=(url, port, app), name="open-ui",
                             daemon=True).start()

        def watch_quit():
            # 前端点了退出 / 看门狗超时 / 后端被要求关 —— 统一从这里停。
            while not server.should_exit:
                if app.state.quit_requested or watchdog.fired:
                    server.should_exit = True
                    break
                time.sleep(0.3)

        threading.Thread(target=watch_quit, name="quit-watch", daemon=True).start()
        server.run()
    finally:
        watchdog.stop()
        shutdown()
    return 0


def _open_when_ready(url: str, port: int, app, *,
                     timeout: float = 20.0) -> None:
    """Wait until the port accepts connections, then open the browser.

    端口还没起来就开窗口，用户会看到一个 ERR_CONNECTION_REFUSED 的页面。
    20 秒足够慢机器起 uvicorn，也短到不至于让用户以为卡死。
    """
    from . import launcher as launcher_mod

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if launcher_mod._port_open("127.0.0.1", port):
            break
        time.sleep(0.1)
    else:
        event("browser", "open_skipped_port_never_opened",
              level=logging.WARNING, port=port)
        return

    try:
        label, browser_pid = browser_mod.open_ui(url, port=port)
    except Exception as exc:                       # noqa: BLE001 - 开浏览器失败不该杀后端
        # **必须说出来。** 这里原来只写结构化日志（默认看不到），表现为
        # "客户端已启动"之后什么都没有、然后退出码 1 —— 用户以为启动失败，
        # 其实后端好好的，只是窗口没打开。
        _log(f"  打不开浏览器（{exc}）；请手动访问 {url}")
        event("browser", "open_failed", level=logging.WARNING, error=exc)
        return
    _log(f"  浏览器：{label}")
    _log("  关闭窗口即退出；后端也会在前端消失后自行退出。")
    if browser_pid:
        # --app 模式仍带系统标题栏；窗口要几秒后才存在，所以另开线程。
        threading.Thread(target=_adopt_window, args=(browser_pid, app),
                         name="window-adopt", daemon=True).start()


def _adopt_window(pid: int, app) -> None:
    """Strip the OS title bar once our Chromium window exists.

    Best-effort: if it fails (non-Windows, changed flags, window closed early)
    the app still works, it just keeps its caption.
    """
    from . import window as window_mod

    try:
        hwnd = window_mod.prepare(pid)
    except Exception as exc:                        # noqa: BLE001 - cosmetic
        event("window", "adopt_failed", level=logging.DEBUG, error=exc)
        return
    if hwnd:
        app.state.window_handle = hwnd
        _log("  已隐藏系统标题栏")
    else:
        event("window", "adopt_skipped", level=logging.DEBUG, pid=pid)


def _make_shutdown(account, network, owned) -> "callable":
    """Build the idempotent teardown used by every exit path."""
    done = threading.Event()

    def shutdown() -> None:
        if done.is_set():
            return
        done.set()
        event("lifecycle", "shutdown_begin")
        # 先断 QQ：让 QQ 那边立刻看到我们下线，而不是等 websocket 超时。
        try:
            account.close()
        except Exception as exc:                    # noqa: BLE001 - shutdown path
            event("lifecycle", "account_close_failed", level=logging.WARNING,
                  error=exc)
        try:
            network.stop()
        except Exception:                           # noqa: BLE001
            pass
        # 再收掉自己起的子进程（SnowLuma 会连带它的 node 子进程）。
        if owned is not None:
            killed = owned.terminate_all()
            _forget_child()
            if killed:
                event("lifecycle", "shutdown_done", killed=",".join(killed))
        event("lifecycle", "shutdown_end")

    return shutdown


def _install_crash_trace() -> None:
    """把**硬崩溃**也记下来。

    ``pythonw`` 下，Python 异常还能被 main() 兜住；但 C 层崩溃
    （access violation、PIL/numpy 里的段错误）会让进程直接消失，日志
    停在最后一行、什么都不留 —— 用户看到的就是"点一下闪退"。

    ``faulthandler`` 能捕获这类信号并打印 Python 栈，即使在 pythonw 下
    也能写进我们指定的文件。
    """
    try:
        import faulthandler
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``keep_open=True`` 让它在整个进程生命期内保持打开。
        handle = open(path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=handle, all_threads=True)
        # 保存引用，避免被 GC 关掉。
        _install_crash_trace._handle = handle      # type: ignore[attr-defined]
    except Exception:                              # noqa: BLE001 - 尽力而为
        pass


def main(argv: list[str] | None = None) -> int:
    """Entry point for the packaged launcher (no arguments expected).

    **pythonw 没有 stdout/stderr**，退出码 1 之外什么都不给。所以这里把
    所有未捕获异常先写进日志再说 —— 否则用户只看到"启动失败（退出码 1）"，
    日志最后一行还是"客户端已启动"，完全没法查。
    """
    _install_crash_trace()
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException as exc:                   # noqa: BLE001 - 最后一道网
        import traceback
        _log(f"未捕获的异常，程序退出：{type(exc).__name__}: {exc}")
        for line in traceback.format_exc().splitlines()[-12:]:
            _log("  " + line)
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    snowluma = None
    if "--snowluma" in args:
        index = args.index("--snowluma")
        if index + 1 < len(args):
            snowluma = Path(args[index + 1])
    return run(snowluma=snowluma)


if __name__ == "__main__":
    raise SystemExit(main())
