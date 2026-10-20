"""两个 QQ 号真实互通：握手 + 密文收发，跑在真的 OneBot 连接上。

这跟 `interfaces/desktop/legacy/chat_gui.py`（一个进程里两扇窗）不是一回事，
而且不能是：QQ 上的"两端"是两个**账号**，各自有一条自己的 OneBot 连接。
所以这里一个进程里跑两个 `Side`，各自连自己的端口 —— 正好对应同机双号
（见 `docs/QQ_TESTBED.md`）；跨机时每台机器各跑 `--side a` 或 `--side b`。

为什么值得单独一个入口：本地 GUI 的两个窗口共用内存里的队列，什么都"一定
送达"；真实链路会丢图、乱序、超时，而且对端可能是别人发的普通照片。这些
正是 `terminal/onebot.py` 要处理的东西，得有个能直接压它、又不用盯着两个
窗口点的工具。

用法（同机双号）：:

    python tools/qq_chat.py --a-uin 3925697820 --a-port 9711 \\
                            --b-uin 1741473279 --b-port 9721

    [alice] handshake           # Alice 发起握手，公钥藏在图里
    [alice] send 今晚八点老地方  # 密文藏进图，多次一条消息
    [alice] /as bob             # 切到 Bob 这边
    [bob]   state               # 看会话状态

不接真 QQ 也能验：``--selftest`` 用本地假链路跑完整往返，退出码说明结果。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _utf8_stdout() -> None:
    """中文 Windows 的控制台是 GBK，打印 ``⇒`` 会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                          # noqa: BLE001 - 尽力而为
            pass


_utf8_stdout()

from PIL import Image  # noqa: E402

from markovcryptor.observability import configure  # noqa: E402
from markovcryptor.terminal import Terminal  # noqa: E402
from markovcryptor.terminal.onebot import OneBotNetwork  # noqa: E402


# --------------------------------------------------------------------- covers


def synthetic_cover(seed: int = 0, size: int = 512) -> bytes:
    """一张"看起来像照片"的载体。

    真的照片当然更好——越像随手拍的越好。但一个能自己造载体的工具才跑得起来，
    而且它的用途是**验证链路**，不是发出去骗人。渐变 + 噪声让 QIM 有足够的
    块可改，也让图不至于是一块纯色（纯色图会被一眼看出是生成的）。
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    height = width = size
    # 两个方向都广播到 (H, W)，否则 stack 会因为 (1,W) 和 (H,1) 形状不同而报错。
    y = np.broadcast_to(np.linspace(0, 1, height)[:, None], (height, width))
    x = np.broadcast_to(np.linspace(0, 1, width)[None, :], (height, width))
    base = np.stack([
        120 + 60 * np.sin(3.1 * x + seed),
        110 + 50 * np.cos(2.7 * y + seed),
        100 + 70 * np.sin(2.2 * (x + y)),
    ], axis=-1)
    noise = rng.normal(0, 6, (height, width, 3))
    pixels = np.clip(base + noise, 0, 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, "PNG")
    return buffer.getvalue()


def load_covers(directory: Path | None, count: int = 4) -> list[bytes]:
    """Use real photos when given, else make our own."""
    covers: list[bytes] = []
    if directory is not None:
        if not directory.is_dir():
            raise SystemExit(f"不是目录：{directory}")
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
                with Image.open(path) as image:
                    buffer = io.BytesIO()
                    image.convert("RGB").save(buffer, "PNG")
                    covers.append(buffer.getvalue())
        if not covers:
            raise SystemExit(f"{directory} 里没有图片")
    while len(covers) < count:
        covers.append(synthetic_cover(seed=len(covers)))
    return covers


# ----------------------------------------------------------------------- side


class Side:
    """One QQ account: its own OneBot connection, terminal and cover pool."""

    def __init__(self, label: str, uin: str, port: int, token: str,
                 peer_uin: str, covers: list[bytes], *, url: str | None = None,
                 psk: str = "共享口令-123", network=None) -> None:
        self.label = label
        self.uin = uin
        self.peer_uin = peer_uin
        self.covers = list(covers)
        self._cover_cursor = 0
        self.events: list[str] = []

        self.network = network or OneBotNetwork(
            url or f"ws://127.0.0.1:{port}/", uin, token=token)
        # attach() 已经把 deliver 注册到网络上了；再手动 register 一次会让
        # 每个包被投递两遍（握手回两遍、密文出现两条）。
        self.terminal = Terminal(uin, self.network, psk=psk).attach()
        # 自动应答要用**自己**的图，不能用收到的图重写——那会把对方那份载荷
        # 覆盖掉（`_auto_respond` 的注释里有完整理由）。
        self.terminal.take_cover = self.take_cover
        self.terminal.subscribe(self._on_inbound)

    # ---------------------------------------------------------------- covers

    def take_cover(self) -> bytes | None:
        """Hand out a cover, cycling so a second handshake still has one.

        Popping would be more "correct" in the queue sense, but then the third
        handshake in a session fails for no reason the user can see. Cycling
        costs nothing: a cover can be reused, it is not consumed.
        """
        if not self.covers:
            return None
        cover = self.covers[self._cover_cursor % len(self.covers)]
        self._cover_cursor += 1
        return cover

    # --------------------------------------------------------------- inbound

    def _on_inbound(self, inbound) -> None:
        """Print what arrived, in the two-window spirit but on one console."""
        prefix = f"[{self.label}] "
        if inbound.handshake:
            self.say(f"{prefix}⇐ 握手：{inbound.handshake}")
        if inbound.covert:
            for text in inbound.covert:
                self.say(f"{prefix}⇐ 密文解开：{text}")
        if inbound.chat_text:
            self.say(f"{prefix}⇐ 明文：{inbound.chat_text}")
        for note in inbound.notes:
            self.say(f"{prefix}⇐ 注意：{note}")

    @staticmethod
    def say(line: str) -> None:
        print(line, flush=True)

    # ---------------------------------------------------------------- actions

    def handshake(self) -> None:
        cover = self.take_cover()
        if cover is None:
            raise SystemExit(f"{self.label}：没有可用的底图")
        self.terminal.send_handshake(self.peer_uin, (cover,))
        self.say(f"[{self.label}] ⇒ 已发出握手图（公钥藏在图里）")

    def send_covert(self, text: str) -> None:
        """Hide ``text`` in pictures and send them.

        One picture is enough for a sentence (441 bytes at 512², and the
        envelope is length-stable for a given session). Asking for more would
        just spend more pictures for nothing, so use the minimum that fits.
        """
        picture = self.take_cover()
        if picture is None:
            raise SystemExit(f"{self.label}：没有可用的底图")
        self.terminal.send_covert_in_images(self.peer_uin, text, (picture,))
        self.say(f"[{self.label}] ⇒ 密文已藏进图里发出（明文不上网）")

    def state(self) -> str:
        session = self.terminal.session
        return (f"[{self.label}] 状态={session.state.label} "
                f"对端={self.peer_uin} "
                f"发送键={session.send_key[:6].hex() or '-'} "
                f"接收键={session.recv_key[:6].hex() or '-'}")


# ------------------------------------------------------------------ selftest


def run_selftest(psk: str, verbose: bool = True) -> int:
    """Full round trip over an in-process OneBot, no QQ and no network at all.

    This is the "does the whole thing actually work" check: two independent
    terminals, each with its own OneBot connection object, exchanging a
    handshake and then ciphertext through the real transport code path.
    """
    from markovcryptor.terminal.onebot import InProcessOneBot

    hub = InProcessOneBot()
    covers = [synthetic_cover(seed=i) for i in range(3)]
    sides = []
    for label, uin, peer in (("alice", "10001", "10002"),
                             ("bob", "10002", "10001")):
        network = hub.connect(uin)
        sides.append(Side(label, uin, 0, "", peer, covers, network=network))
    alice, bob = sides

    alice.handshake()
    hub.pump()
    if not (alice.terminal.established and bob.terminal.established):
        print("失败：握手后两侧没有都建立会话", file=sys.stderr)
        return 1
    if alice.terminal.session.send_key != bob.terminal.session.recv_key:
        print("失败：两侧派生的密钥不一致", file=sys.stderr)
        return 1
    if verbose:
        print(f"握手完成：{alice.state()}")
        print(f"          {bob.state()}")

    secret = "今晚八点，老地方。"
    alice.send_covert(secret)
    hub.pump()
    got = [text for item in bob.terminal.inbox for text in item.covert]
    if got != [secret]:
        print(f"失败：Bob 没解出密文（收到 {got}）", file=sys.stderr)
        return 1
    if verbose:
        print(f"密文往返成功：Bob 解出 {got[0]!r}")
        print("明文没上网：连线上只有图片和空文本")

    reply = "收到，不见不散。"
    bob.send_covert(reply)
    hub.pump()
    back = [text for item in alice.terminal.inbox for text in item.covert]
    if back != [reply]:
        print(f"失败：反向不通（收到 {back}）", file=sys.stderr)
        return 1
    if verbose:
        print(f"反向也通：Alice 解出 {back[0]!r}")
        print("自检全部通过。")
    return 0


# ------------------------------------------------------------------- network


class LocalTwoSides:
    """Both QQs in one process, each on its own OneBot endpoint."""

    def __init__(self, a: Side, b: Side) -> None:
        self.a = a
        self.b = b

    def start(self) -> None:
        for side in (self.a, self.b):
            side.network.start()
            side.say(f"[{side.label}] 连 {side.network.url} 等待 {side.peer_uin}…")

    def stop(self) -> None:
        for side in (self.a, self.b):
            side.network.stop()


def run_repl(sides: list[Side]) -> int:
    current = sides[0]
    print(__doc__.split("用法（同机双号）")[0].strip())
    print()
    print(f"当前：{current.label}（{current.uin}）→ {current.peer_uin}")
    print("命令：handshake / send <文字> / state / /as <alice|bob> / /quit")
    while True:
        try:
            line = input(f"[{current.label}] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit", "quit", "exit"):
            return 0
        if line.startswith("/as"):
            _, _, name = line.partition(" ")
            picked = next((s for s in sides if s.label == name.strip()), None)
            if picked is None:
                print(f"没有 {name.strip()!r}；可用：{[s.label for s in sides]}")
            else:
                current = picked
                print(f"当前：{current.label}（{current.uin}）→ {current.peer_uin}")
            continue
        try:
            if line in ("handshake", "hs"):
                current.handshake()
            elif line.startswith("send "):
                current.send_covert(line[len("send "):])
            elif line in ("state", "status"):
                print(current.state())
            else:
                print(f"不认识的命令：{line}")
        except SystemExit:
            raise
        except Exception as exc:                  # noqa: BLE001 - REPL must not die
            print(f"出错：{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="两个 QQ 号真实互通：OneBot 传输层上的握手 + 密文收发",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true",
                        help="不接 QQ，用本地假链路跑完整往返")
    parser.add_argument("--a-uin", help="Alice 的 QQ 号")
    parser.add_argument("--a-port", type=int, help="Alice 的 OneBot WS 端口")
    parser.add_argument("--a-token", default="", help="Alice 的 accessToken")
    parser.add_argument("--b-uin", help="Bob 的 QQ 号")
    parser.add_argument("--b-port", type=int, help="Bob 的 OneBot WS 端口")
    parser.add_argument("--b-token", default="", help="Bob 的 accessToken")
    parser.add_argument("--single", choices=("a", "b"),
                        help="只跑一侧（跨机时用），对端 QQ 用 --peer 指定")
    parser.add_argument("--peer", help="--single 时对端的 QQ 号")
    parser.add_argument("--cover", type=Path, help="底图目录；不给就自动生成")
    parser.add_argument("--psk", default="共享口令-123",
                        help="隐写层的共享口令，两侧必须一致")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    configure(args.log_level)

    if args.selftest:
        return run_selftest(args.psk)

    covers = load_covers(args.cover)

    if args.single:
        which = args.single
        uin = args.a_uin if which == "a" else args.b_uin
        port = args.a_port if which == "a" else args.b_port
        token = args.a_token if which == "a" else args.b_token
        if not (uin and port and args.peer):
            parser.error("--single 需要 --<侧>-uin、--<侧>-port 和 --peer")
        side = Side(which, uin, port, token, args.peer, covers, psk=args.psk)
        network = LocalTwoSides(side, side)
        network.start()
        try:
            return run_repl([side])
        finally:
            side.network.stop()

    if not (args.a_uin and args.a_port and args.b_uin and args.b_port):
        parser.error("需要 --a-uin/--a-port 和 --b-uin/--b-port，或用 --selftest")
    alice = Side("alice", args.a_uin, args.a_port, args.a_token, args.b_uin,
                 covers, psk=args.psk)
    bob = Side("bob", args.b_uin, args.b_port, args.b_token, args.a_uin,
               covers, psk=args.psk)
    runner = LocalTwoSides(alice, bob)
    runner.start()
    try:
        return run_repl([alice, bob])
    finally:
        runner.stop()


if __name__ == "__main__":
    raise SystemExit(main())
