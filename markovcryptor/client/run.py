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

import logging
import os
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
    uins = launcher.webui_uins()
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
        launcher.restart_snowluma(root, owned)
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
            # 记下来：退出时要收掉。只收**我们自己起的**那一份 ——
            # 用户手动开的 SnowLuma 不能因为关这个客户端就被杀掉。
            owned.add("SnowLuma", launcher.start_snowluma(root))
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
    if should_open:
        label, browser_pid = browser_mod.open_ui(url)
        _log(f"  浏览器：{label}")
        _log("  关闭窗口即退出；后端也会在前端消失后自行退出。")
        if browser_pid:
            # --app 模式仍带系统标题栏。几秒后窗口才存在，所以放后台线程，
            # 不阻塞 uvicorn 起来 —— 用户在等的是这个窗口。
            threading.Thread(
                target=_adopt_window,
                args=(browser_pid, app),
                name="window-adopt", daemon=True).start()

    import uvicorn

    try:
        # 不用 uvicorn.run()：它装自己的信号处理并且不给我们"主动停"的把手。
        # 用 Server + 一个监视线程，就能在用户点退出时优雅收尾。
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning")
        server = uvicorn.Server(config)

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
            if killed:
                event("lifecycle", "shutdown_done", killed=",".join(killed))
        event("lifecycle", "shutdown_end")

    return shutdown


def main(argv: list[str] | None = None) -> int:
    """Entry point for the packaged launcher (no arguments expected)."""
    args = list(sys.argv[1:] if argv is None else argv)
    snowluma = None
    if "--snowluma" in args:
        index = args.index("--snowluma")
        if index + 1 < len(args):
            snowluma = Path(args[index + 1])
    return run(snowluma=snowluma)


if __name__ == "__main__":
    raise SystemExit(main())
