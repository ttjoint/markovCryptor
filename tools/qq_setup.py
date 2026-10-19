"""QQ 双端测试环境：一份 SnowLuma 挂两个 QQ，各自一套 OneBot 端口。

为什么是"一份"而不是"两份"
--------------------------

实测发现 SnowLuma **单个实例能同时 hook 多个已登录的 QQ**（日志里两个
UIN 各自 `login detected` + `pipe connected`）。原先设想"复制两份、各挂
一个号"只有在**跨机器**时才需要：两个 QQ 都跑在同一台机器上时，一个实例
就能把两个号都拿下来，省一半进程和内存。

但要注意：两个号默认都读 `onebot_<自己UIN>.json`，如果两份配置里的端口
相同，第二个账号会 `EADDRINUSE` 起不来（这条踩过）。所以每个 UIN 必须
配**不同端口**，本工具就是干这个的。

    ┌────────────── SnowLuma（一个进程，WebUI :5099）──────────────┐
    │  QQ 1741473279 ──► OneBot WS :9711 / HTTP :9712              │
    │  QQ 3925697820 ──► OneBot WS :9721 / HTTP :9722              │
    └──────────────────────────────────────────────────────────────┘

用法
----

    # 1. 首次：写配置（QQ 号从 `--a-uin` / `--b-uin` 来，两个都必须已登录）
    python tools/qq_setup.py init --snowluma DIR --a-uin 111 --b-uin 222

    # 2. 起 SnowLuma（自动 hook 所有已登录 QQ）
    python tools/qq_setup.py up --snowluma DIR

    # 3. 验图片能否原样往返（P0，最重要的一步）
    python tools/qq_setup.py probe --snowluma DIR

    # 4. 收工
    python tools/qq_setup.py down

配置写进 `DIR/config/onebot_<uin>.json`，**不改 SnowLuma 任何源码**。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from markovcryptor.onebot_config import (      # noqa: E402
    OneBotSettings,
    discover,
    write_config,
)

#: 每个账号一套端口。区间留宽，方便手动加第三个号。
DEFAULT_PORTS = {
    "a": {"ws": 9711, "http": 9712},
    "b": {"ws": 9721, "http": 9722},
}
WEBUI_PORT = 5099


def cmd_init(args: argparse.Namespace) -> int:
    snowluma = Path(args.snowluma).resolve()

    if not (snowluma / "index.mjs").is_file():
        raise SystemExit(f"{snowluma} 下没有 index.mjs，这不像 SnowLuma 的 release 目录")

    written = []
    for side, uin in (("a", args.a_uin), ("b", args.b_uin)):
        if not uin:
            continue
        ports = DEFAULT_PORTS[side]
        settings = OneBotSettings(uin=str(uin), ws_port=ports["ws"],
                                  http_port=ports["http"])
        try:
            path, wrote = write_config(snowluma, settings, force=args.force)
        except FileNotFoundError as error:
            raise SystemExit(str(error)) from error
        if wrote:
            print(f"  写入 {path.name}: WS {ports['ws']} / HTTP {ports['http']}  "
                  f"token={settings.token}")
        else:
            # 已有配置就别覆盖：里面可能已经被 SnowLuma 补过字段。
            print(f"  保留 {path.name}（已存在；要重写加 --force）")
        written.append((uin, ports))

    if not written:
        raise SystemExit("至少给一个 --a-uin 或 --b-uin")

    print(f"""
SnowLuma 配置就绪（{len(written)} 个账号，互不抢端口）。
下一步：python tools/qq_setup.py up --snowluma {snowluma}
注意：两个 QQ 必须先在本机手动登录好 —— SnowLuma 是 hook 已登录的 QQ 进程，
     它不负责登录，这一步自动化不了。""")
    return 0


def _launcher(snowluma: Path) -> list[str]:
    """挑启动方式：Windows 用 index.mjs，其他平台同理（release 自带 node 运行时名不同）。"""
    entry = snowluma / "index.mjs"
    if not entry.is_file():
        raise SystemExit(f"{entry} 不存在")
    return ["node", str(entry)]


def cmd_up(args: argparse.Namespace) -> int:
    snowluma = Path(args.snowluma).resolve()
    command = _launcher(snowluma)
    env = dict(os.environ)
    # 自动 hook 发现到的所有已登录 QQ，不用去 WebUI 里一个个点。
    env["SNOWLUMA_HOOK_AUTOLOAD"] = "1"

    log = snowluma / "logs" / "mkx-up.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f"启动：{' '.join(command)}")
    print(f"日志：{log}")
    handle = log.open("wb")
    process = subprocess.Popen(command, cwd=snowluma, env=env,
                               stdout=handle, stderr=subprocess.STDOUT)
    print(f"PID {process.pid}；等 OneBot 端口就绪…")

    wanted = [DEFAULT_PORTS[side]["ws"] for side in ("a", "b")]
    for _ in range(60):
        time.sleep(1)
        if process.poll() is not None:
            print(f"进程已退出（code={process.returncode}），看日志：{log}",
                  file=sys.stderr)
            return 1
        ready = [port for port in wanted if _port_open(port)]
        if len(ready) == len(wanted):
            print(f"就绪：{ready}")
            print(f"WebUI: http://127.0.0.1:{WEBUI_PORT}/")
            return 0
    print(f"超时：端口 {wanted} 未全部就绪，看日志 {log}", file=sys.stderr)
    return 1


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((host, port)) == 0


def cmd_down(args: argparse.Namespace) -> int:
    """只结束我们自己起的 SnowLuma（按命令行特征），不碰用户的 QQ。"""
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
            "Where-Object { $_.CommandLine -match 'index\\.mjs' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True)
        killed = [line.strip() for line in result.stdout.split() if line.strip().isdigit()]
        print(f"已结束 {len(killed)} 个 SnowLuma 进程 {killed}")
        return 0
    result = subprocess.run(["pkill", "-f", "index.mjs"], capture_output=True)
    print("已结束 SnowLuma 进程" if result.returncode == 0 else "没有找到在跑的 SnowLuma")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """转给 qq_probe：从已写好的配置里推出地址和 token。"""
    snowluma = Path(args.snowluma).resolve()
    found = discover(snowluma)
    if len(found) < 2:
        raise SystemExit(
            f"需要两个账号的配置，现在只有 {sorted(found)}。先跑 qq_setup.py init")

    (a_uin, a), (b_uin, b) = sorted(found.items())[:2]
    command = [sys.executable, str(Path(__file__).with_name("qq_probe.py")),
               "--a-uin", a_uin, "--a-ws", a.ws_url, "--a-token", a.token,
               "--b-uin", b_uin, "--b-ws", b.ws_url, "--b-token", b.token]
    if args.only:
        command += ["--only", args.only]
    print(f"探测：{a_uin} -> {b_uin}")
    return subprocess.call(command)


def cmd_status(args: argparse.Namespace) -> int:
    snowluma = Path(args.snowluma).resolve()
    found = discover(snowluma)
    if not found:
        print("没有 onebot_<uin>.json 配置，先跑 init")
        return 1
    web_up = _port_open(WEBUI_PORT)
    rows = []
    for uin, settings in found.items():
        rows.append((uin, settings.ws_port, _port_open(settings.ws_port)))
    for uin, port, ws_up in rows:
        print(f"  QQ {uin}  OneBot :{port} {'ok' if ws_up else '--'}  "
              f"WebUI :{WEBUI_PORT} {'ok' if web_up else '--'}")
    return 0 if all(row[2] for row in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="写每个 QQ 的 OneBot 配置（不同端口）")
    init.add_argument("--snowluma", required=True, help="已解压的 SnowLuma 目录")
    init.add_argument("--a-uin", help="第一个 QQ 号")
    init.add_argument("--b-uin", help="第二个 QQ 号")
    init.add_argument("--force", action="store_true", help="覆盖已有配置文件")
    init.set_defaults(func=cmd_init)

    up = sub.add_parser("up", help="启动 SnowLuma 并等 OneBot 端口就绪")
    up.add_argument("--snowluma", required=True)
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="结束 SnowLuma（不动 QQ）")
    down.set_defaults(func=cmd_down)

    probe = sub.add_parser("probe", help="验图片能否原样往返")
    probe.add_argument("--snowluma", required=True)
    probe.add_argument("--only", choices=("single", "multi", "big"))
    probe.set_defaults(func=cmd_probe)

    status = sub.add_parser("status", help="看各账号端口是否在监听")
    status.add_argument("--snowluma", required=True)
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
