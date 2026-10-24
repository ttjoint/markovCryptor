"""历史消息**一律不解码**。

用户的问题：

> 由于可能未与 QQ 同步期间积累超级多历史（尤其考虑多好友）那么我觉得不应
> 当从上次结束开始完全分析所有用户从 nonebot 抓下来的新"历史消息"那样可能
> 潜在巨大 [开销]

> 或者说干脆无视所有我开软件之前的包是否加密，一律忽视

**一律忽视**是对的，而且理由不止性能：

1. **开销**：一个好友几百条历史、几十个好友，逐张解码是几十秒的 CPU。
   而 1600×1600 一张就要 3.3 秒。
2. **重放攻击**：历史图可以被任何人反复重放。自动处理等于把「确认」这一步
   交给攻击者控制。
3. **时效性**：三天前的握手请求早无意义，捡回来只会制造混乱。
4. **与手动确认矛盾**：刚决定「必须人确认」，自动捡历史等于绕过它。

这些测试锁住「历史不进解码器」这条边界。
"""

from __future__ import annotations

import base64
import io
import unittest

from PIL import Image

from markovcryptor import image_stego
from markovcryptor.client.account import Account
from markovcryptor.client.services.history import merge_history
from markovcryptor.core.identity import build_handshake_payload
from markovcryptor.core.keys import normalise_key
from markovcryptor.terminal.onebot import InProcessOneBot
from markovcryptor.terminal.protocol import Mode, build_handshake

ALICE, BOB = "1000", "2000"


def _handshake_png(psk="pbx1") -> bytes:
    """A real handshake image, as it would sit in QQ's history."""
    public = bytes(range(32))
    mac = build_handshake_payload(public, public, normalise_key(psk))[32:]
    frame = build_handshake(public, Mode.REQUEST, mac=mac)
    stego, _ = image_stego.embed(
        Image.new("RGB", (256, 256), "white"),
        image_stego.build_frame(frame),
        image_stego.normalise_key(psk))
    buffer = io.BytesIO()
    stego.save(buffer, "PNG")
    return buffer.getvalue()


def _data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


class _FakeRef:
    def __init__(self, url: str) -> None:
        self._url = url

    def locate(self) -> str:
        return self._url


class _FakeParsed:
    def __init__(self, url: str, *, text: str = "", sender: str = BOB) -> None:
        self.text = text
        self.unsupported = []
        self.at = 0.0
        self.message_id = "hist-1"
        self.sender = sender
        self.images = [_FakeRef(url)]


class HistoryIsNeverDecodedTest(unittest.TestCase):
    def setUp(self):
        self.hub = InProcessOneBot()
        self.hub.friends = {BOB: []}
        self.account = Account(self.hub.connect(ALICE)).start()

    def _merge(self, parsed) -> object:
        self.account.load_history = lambda peer, count=20: [parsed]
        conversation = self.account.conversations.ensure(BOB)
        merge_history(self.account, conversation, BOB, 20)
        return conversation

    def test_history_handshake_image_is_not_a_request(self):
        """历史里的握手图**不能**变成待确认请求。

        否则任何人往聊天里丢一张旧握手图，就能在你打开会话时弹出一个
        「对方请求建立加密连接」—— 而那张图可能是几个月前重放的。
        """
        conversation = self._merge(_FakeParsed(_data_url(_handshake_png())))
        self.assertIsNone(conversation.terminal.pending_request,
                          "历史图不该被解码成握手请求")
        self.assertFalse(conversation.terminal.established)

    def test_history_image_still_shows_up(self):
        """不解码 ≠ 不显示。图片该正常出现在聊天记录里。

        下载是后台线程（读线程不能被 CDN 卡住），所以这里等一下。
        """
        import time

        png = _handshake_png()
        url = _data_url(png)
        # 预置缓存：真实环境里这些图是真的从 CDN 下载的，这里只是想验证
        # "下载到的图会被附到条目上"，不测网络。
        self.account.images._items[url] = png

        conversation = self._merge(_FakeParsed(url))
        entries = conversation.snapshot()
        self.assertEqual(len(entries), 1, "历史条目应当被保留")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not entries[0].images:
            time.sleep(0.02)
        self.assertTrue(entries[0].images, "图片应当被附上（后台下载）")

    def test_history_text_is_kept_verbatim(self):
        """历史正文照常显示 —— 只跳过隐写解码，不是跳过整条消息。"""
        conversation = self._merge(
            _FakeParsed(_data_url(_handshake_png()), text="普通聊天"))
        entries = conversation.snapshot()
        self.assertEqual(entries[0].text, "普通聊天")

    def test_merging_never_calls_the_decoder(self):
        """硬证明：合并历史时不碰 ``_inspect_image``。

        这条比断言"没产生 pending"更直接 —— 它证明的是一张图都没有被送去
        解码，而不管解码结果是什么。
        """
        calls = []
        original = type(self.account.conversations)._cover_for \
            if hasattr(type(self.account.conversations), "_cover_for") else None

        conversation = self.account.conversations.ensure(BOB)
        terminal = conversation.terminal
        terminal._inspect_image = lambda raw: calls.append(raw) or (None, None)

        self._merge(_FakeParsed(_data_url(_handshake_png())))
        self.assertEqual(calls, [], "历史合并不该触发任何解码")


class StartupDoesNotScanTest(unittest.TestCase):
    def test_new_account_has_no_conversations(self):
        """**启动时不预取任何人的历史。**

        几十个好友 × 几十条历史 × 每张解码几秒，是分钟级的开销。用户打开
        软件的第一秒不该耗在这上面。
        """
        hub = InProcessOneBot()
        hub.friends = {f"100{i}": [] for i in range(30)}
        account = Account(hub.connect(ALICE)).start()
        self.assertEqual(len(account.conversations), 0,
                         "启动时不该为好友建会话")


if __name__ == "__main__":
    unittest.main()
