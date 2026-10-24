"""握手去重 / 只回最新 / 单张图片载荷补满。

用户的两条要求：

1. **对方反复发握手时，只能回复最新一个**，且界面要说明是几秒前的请求；
   一次性建立一堆连接没有任何必要。对端收到一串回复时也只该采纳最新的。
2. **图片信道的每张图都应写满最大隐写额度的密文** —— 观察者只能知道
   发了几张图（图片数量本来就暴露信息量），单张长度不该再泄露第二份。
"""

from __future__ import annotations

import time
import unittest

import fixtures  # noqa: F401  (path setup for shared fixtures)

from markovcryptor.core.errors import InvalidInput
from markovcryptor.terminal import LoopbackNetwork, Terminal
from markovcryptor.terminal.protocol import (
    Kind,
    Mode,
    build_slice,
    parse_slice,
    peek_kind,
)
from markovcryptor.terminal.session import Session
from markovcryptor.terminal.terminal import split_payload

from test_terminal import _cover, _png_bytes, _synthetic_cover


class SlicePaddingTest(unittest.TestCase):
    def test_every_slice_fills_its_cover(self):
        """分片长度必须等于目标容量 —— 这是"看不出写了多长"的全部依据。"""
        envelope = b"x" * 36
        capacities = [441, 712, 258]
        pieces = split_payload(envelope, 3, capacities=capacities)
        self.assertEqual([len(p) for p in pieces], capacities)

    def test_padding_does_not_change_the_payload(self):
        """补了填充之后，重组出来必须和原文一模一样。"""
        envelope = b"payload-contents-here" * 2
        pieces = split_payload(envelope, 3, capacities=[441, 712, 258])
        chunks = [parse_slice(p)[2] for p in pieces]
        self.assertEqual(b"".join(chunks), envelope)

    def test_padded_slices_are_marked_not_guessed(self):
        """填充过的分片用独立 kind 标记。

        试过"看前 2 字节像不像长度"的启发式，但正文首字节完全可能落在
        合法范围 —— 那会变成偶发且极难复现的解码错误。
        """
        padded = split_payload(b"x" * 36, 2, capacities=[441, 441])
        for piece in padded:
            self.assertIs(peek_kind(piece), Kind.BODY_SLICE_PADDED)

    def test_without_capacity_no_padding(self):
        """不给容量就不补，行为与原来一致（文本信道用不到填充）。"""
        pieces = split_payload(b"x" * 30, 3)
        # 每片 = 7 字节片头 + 10 字节内容
        self.assertEqual([len(p) for p in pieces], [17, 17, 17])
        for piece in pieces:
            self.assertIs(peek_kind(piece), Kind.BODY_SLICE)

    def test_padding_is_random_not_zeros(self):
        """填充必须是随机的：可压缩的全零会让图片体积异常变小，
        那本身就成了一个可识别的特征。"""
        a = split_payload(b"x" * 20, 1, capacities=[400])[0]
        b = split_payload(b"x" * 20, 1, capacities=[400])[0]
        self.assertNotEqual(a, b, "两次填充应当不同（随机）")
        tail = a[-50:]
        self.assertNotEqual(tail, b"\x00" * 50, "不该用全零填充")

    def test_oversized_chunk_is_not_truncated(self):
        """密文比容量大时不能截断 —— 那会毁掉数据。"""
        # 每片名义上装 10 字节，但密文有 30 字节；容量不够时不补也不截断。
        pieces = split_payload(b"x" * 30, 3, capacities=[10, 10, 10])
        self.assertEqual(b"".join(parse_slice(p)[2] for p in pieces),
                         b"x" * 30)

    def test_slice_returns_original_for_round_trip(self):
        chunk = build_slice(0, 1, b"abc")
        index, count, got = parse_slice(chunk)
        self.assertEqual((index, count, got), (0, 1, b"abc"))

    def test_padded_slice_round_trip(self):
        chunk = build_slice(1, 2, b"abcDEFGH", real_length=3)
        index, count, got = parse_slice(chunk)
        self.assertEqual((index, count, got), (1, 2, b"abc"))


class HandshakeDedupTest(unittest.TestCase):
    """对方反复发握手时的行为。"""

    def _pair(self):
        net = LoopbackNetwork()
        alice = Terminal("alice", net, psk="pbx1")
        alice.take_cover = lambda: _png_bytes(_synthetic_cover())
        alice.attach()
        bob = Terminal("bob", net, psk="pbx1")
        bob.take_cover = lambda: _png_bytes(_synthetic_cover())
        bob.attach()
        return net, alice, bob

    def test_replayed_request_is_ignored(self):
        """同一张握手图重放多次，只算一次请求。

        否则用户会被一堆相同的请求淹没，很可能误点其中一个。
        """
        net, alice, bob = self._pair()
        alice.send_handshake("bob", (_png_bytes(_synthetic_cover()),))
        net.pump()
        first = bob.pending_request
        self.assertIsNotNone(first)

        # 真实重放：攻击者截获同一张图，再丢过来三次。
        # history 是 (sender, target, bytes)；直接重放同一段字节。
        packet = net.history[-1][2]
        for _ in range(3):
            net.send("mallory", "bob", packet)
            net.pump()

        self.assertEqual(bob.pending_request, first,
                         "重放不该刷新/替换挂起的请求")
        # 更硬的判据：重放不能往"见过的公钥"里再加记录。
        # 只比 pending 相等不够 —— 即使每次都重新记一遍，pending 看起来也一样。
        self.assertEqual(len(bob._seen_requests), 1,
                         "重放的公钥不该被重复记录")

    def test_newer_request_supersedes_the_older(self):
        """新请求取代旧的 —— 只保留最新一个，不排队。"""
        net, alice, bob = self._pair()
        alice.send_handshake("bob", (_png_bytes(_synthetic_cover()),))
        net.pump()
        old = bob.pending_request

        # 换一套密钥再发（公钥不同 = 新请求）
        alice.session = Session()
        alice.send_handshake("bob", (_png_bytes(_synthetic_cover()),))
        net.pump()

        self.assertIsNotNone(bob.pending_request)
        self.assertNotEqual(bob.pending_request, old,
                            "应当被最新请求取代")

    def test_pending_age_is_reported(self):
        """界面要显示"几秒前的请求" —— 用户得知道自己在点哪个。"""
        net, alice, bob = self._pair()
        alice.send_handshake("bob", (_png_bytes(_synthetic_cover()),))
        net.pump()
        self.assertGreaterEqual(bob.pending_age, 0.0)
        time.sleep(0.05)
        self.assertGreater(bob.pending_age, 0.0, "年龄应当随时间增长")

    def test_no_pending_means_zero_age(self):
        net, _alice, bob = self._pair()
        self.assertEqual(bob.pending_age, 0.0)


class DuplicateResponseTest(unittest.TestCase):
    """对端收到一串回复时，只该采纳最新的、不重复派生密钥。"""

    def test_repeated_response_does_not_rebuild_keys(self):
        """同一个回复到达多次，密钥只能派生一次。

        原来每次都 ``_derive()``，实测 generation 从 1 涨到 3 —— 刚建立好
        的会话被瞬间换掉，已经用它加密的消息就解不开了。
        """
        alice = Session()
        public, _mode = alice.begin()

        bob = Session()
        bob.absorb(public, Mode.REQUEST)
        reply, _ = bob.answer()
        bob.commit_answer()

        first = alice.absorb(reply, Mode.RESPONSE)
        self.assertTrue(first, "第一次应当建立")
        generation = alice.generation

        for _ in range(3):
            again = alice.absorb(reply, Mode.RESPONSE)
            self.assertFalse(again, "重复回复不该再算一次完成")

        self.assertEqual(alice.generation, generation,
                         "重复回复不能让密钥反复重建")

    def test_different_response_still_rebuilds(self):
        """真正的新回复（换钥）仍然要能生效 —— 去重不能把正常重钥堵死。"""
        alice = Session()
        public, _mode = alice.begin()
        bob = Session()
        bob.absorb(public, Mode.REQUEST)
        reply, _ = bob.answer()
        bob.commit_answer()
        alice.absorb(reply, Mode.RESPONSE)
        first_gen = alice.generation

        # 换一对新密钥再来一次
        public2, _mode = alice.begin()
        bob2 = Session()
        bob2.absorb(public2, Mode.REQUEST)
        reply2, _ = bob2.answer()
        bob2.commit_answer()
        self.assertTrue(alice.absorb(reply2, Mode.RESPONSE))
        self.assertGreater(alice.generation, first_gen, "新回复应当生效")


if __name__ == "__main__":
    unittest.main()
