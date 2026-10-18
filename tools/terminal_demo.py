"""Two-terminal demo you can drive by hand.

The unit tests prove the protocol; this exists to check the part tests are bad at
judging -- whether the *behaviour* feels right when you actually use it. Run it
and you get two endpoints in one process, a command prompt, and both windows
printed after every step.

    python -m tools.terminal_demo
    python -m tools.terminal_demo --steps     # non-interactive walkthrough

The plaintext side and the covert side are printed separately, which is the whole
point: you should see ordinary messages land in ``聊天`` and hidden ones land only
in ``隐密``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from markovcryptor.terminal import Inbound, LoopbackNetwork, Packet, Terminal  # noqa: E402
from markovcryptor.terminal.terminal import _png_bytes, _synthetic_cover  # noqa: E402

HELP = """\
命令：
  send <文本>            以 alice 身份明文发送一条消息（带一张图）
  hs                     让 alice 发起密钥交换（bob 会自动回发）
  covert <文本>          alice 把密文藏进图片发给 bob
  reply <文本>           bob 回一条密文
  fake                   伪造一条密文（模拟第三方/坏密钥）
  show                   重新打印两个窗口
  quit                   退出
"""


class Demo:
    def __init__(self) -> None:
        self.net = LoopbackNetwork()
        self.alice = Terminal("alice", self.net).attach()
        self.bob = Terminal("bob", self.net).attach()
        self.cover = _png_bytes(_synthetic_cover(256))
        self.windows: dict[str, list[Inbound]] = {"alice": [], "bob": []}
        for name, terminal in (("alice", self.alice), ("bob", self.bob)):
            terminal.subscribe(self._collector(name))

    def _collector(self, name: str):
        def sink(inbound: Inbound) -> None:
            self.windows[name].append(inbound)
        return sink

    # ------------------------------------------------------------- commands

    def plain(self, text: str) -> None:
        self.alice.send_packet("bob", Packet(text=text, images=(self.cover,)))
        self.net.pump()

    def handshake(self) -> None:
        self.alice.send_handshake("bob", (self.cover,))
        self.net.pump()

    def covert(self, text: str, *, from_bob: bool = False) -> None:
        if from_bob:
            self.bob.send_covert_in_images("alice", text, (self.cover,),
                                           chat_text="（公开说明）")
        else:
            self.alice.send_covert_in_images("bob", text, (self.cover,),
                                             chat_text="（公开说明）")
        self.net.pump()

    def forged(self) -> None:
        """A stranger knows the carrier psk but not the session key."""
        from markovcryptor import image_stego
        from markovcryptor.terminal.protocol import Kind, seal_body, wrap
        from markovcryptor.terminal.terminal import _encode_packet

        envelope = seal_body(Kind.BODY_IMAGE, b"\x5a" * 32, "伪造的密语".encode())
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(self.cover)).convert("RGB")
        stego, _ = image_stego.embed(image, wrap(envelope),
                                     image_stego.normalise_key("pbx1"))
        buffer = io.BytesIO()
        stego.save(buffer, "PNG")
        self.bob.deliver("eve", _encode_packet(Packet(images=(buffer.getvalue(),))))

    # ---------------------------------------------------------------- display

    def show(self) -> None:
        for name in ("alice", "bob"):
            terminal = self.alice if name == "alice" else self.bob
            print(f"\n═══ {name} ═══  会话：{terminal.state.label}")
            chat = [m for m in self.windows[name]]
            if not chat:
                print("  聊天：（空）")
                print("  隐密：（空）")
                continue
            print("  聊天：")
            shown = False
            for inbound in chat:
                if inbound.chat_text:
                    print(f"    · {inbound.chat_text}")
                    shown = True
                if inbound.chat_images:
                    print(f"    · [图片 ×{len(inbound.chat_images)}]")
                    shown = True
                for note in inbound.notes:
                    print(f"    ! {note}")
                    shown = True
            if not shown:
                print("    （无可见内容）")
            print("  隐密：")
            secret = [text for inbound in chat for text in inbound.covert]
            for text in secret:
                print(f"    ✦ {text}")
            if not secret:
                print("    （空）")


def scripted(demo: Demo) -> None:
    """The walkthrough from docs/TERMINAL.md §7, in order."""
    steps = [
        ("明文消息", lambda: demo.plain("今天天气不错")),
        ("密钥交换（bob 应自动回发）", demo.handshake),
        ("alice 发密文", lambda: demo.covert("机密：明早八点")),
        ("bob 回密文", lambda: demo.covert("收到", from_bob=True)),
        ("第三方伪造密文（应被忽略）", demo.forged),
    ]
    for index, (label, action) in enumerate(steps, 1):
        print(f"\n─── 第 {index} 步：{label} ───")
        action()
        demo.show()


def interactive(demo: Demo) -> None:
    print(HELP)
    demo.show()
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        verb, _, rest = line.partition(" ")
        try:
            if verb in ("quit", "exit", "q"):
                break
            elif verb == "send":
                demo.plain(rest)
            elif verb == "hs":
                demo.handshake()
            elif verb == "covert":
                demo.covert(rest)
            elif verb == "reply":
                demo.covert(rest, from_bob=True)
            elif verb == "fake":
                demo.forged()
            elif verb == "show":
                pass
            elif verb == "help":
                print(HELP)
                continue
            else:
                print(f"未知命令 {verb!r}；输入 help 查看")
                continue
            demo.show()
        except Exception as exc:                    # noqa: BLE001 - a REPL should survive
            print(f"出错：{exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", action="store_true",
                        help="跑一遍固定场景后退出，不进入交互")
    args = parser.parse_args(argv)

    demo = Demo()
    if args.steps:
        scripted(demo)
        return 0
    interactive(demo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
