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


def run(*, snowluma: Path | None = None, config_path: Path | None = None,
        wait: float = launcher.DEFAULT_LOGIN_WAIT,
        open_browser: bool | None = None) -> int:
    """Start everything. Returns a process exit code."""
    cfg = ClientConfig.load(config_path)

    try:
        root = find_snowluma(snowluma)
        _log(f"SnowLuma 目录：{root}")

        # 配置存在 ≠ SnowLuma 在跑：上次退出后文件留着，进程早没了。
        # 所以判断依据是**端口是否在听**，不是配置文件在不在。
        # 已经在听就复用（第二份会 EADDRINUSE）；没在听就必须起一份。
        endpoint = launcher.discover_endpoint(root)
        if endpoint is not None and launcher.wait_for_onebot(endpoint.url, timeout=1.0):
            _log(f"复用已在运行的 SnowLuma，QQ {endpoint.uin}")
        elif endpoint is not None:
            _log(f"发现已登录的 QQ {endpoint.uin}，启动 SnowLuma…")
            launcher.start_snowluma(root)
        else:
            _log("正在等待 QQ 登录…（SnowLuma 只能挂到已登录的 QQ 上）")
            launcher.start_snowluma(root)
            endpoint = launcher.wait_for_endpoint(
                root, timeout=wait,
                on_wait=lambda: _log("  仍在等待：请打开 QQ 并登录"))

        if not launcher.wait_for_onebot(endpoint.url):
            raise launcher.LauncherError(
                f"OneBot 端口没起来：{endpoint.url}",
                hint="看 snowluma/logs 下的日志；也可能是端口被别的程序占了。")

    except launcher.LauncherError as exc:
        _log(f"启动失败：{exc}")
        if exc.hint:
            _log(f"  怎么办：{exc.hint}")
        return 2
    except Exception as exc:                       # noqa: BLE001 - startup boundary
        _log(f"启动失败：{exc}")
        return 2

    # 到这里 QQ 一定在线，可以建账号了。
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
    account = Account(network, psk=cfg.psk, queue=queue, models=models)

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

    port = cfg.port or DEFAULT_CLIENT_PORT
    webui = Path(__file__).resolve().with_name("webui")
    app = create_client_app(account, webui_dir=webui)
    url = f"http://127.0.0.1:{port}/"

    _log(f"客户端已启动：{url}")
    _log(f"  本机 QQ {account.uin}，{len(account.directory)} 个好友，"
         f"图片队列上限 {queue.max_items}")

    should_open = cfg.open_browser if open_browser is None else open_browser
    if should_open:
        label = browser_mod.open_ui(url)
        _log(f"  浏览器：{label}")

    import uvicorn

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        network.stop()
        account.close()
    return 0


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
