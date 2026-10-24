"""Protocol + session + terminal tests.

These exercise the feasibility claims in ``docs/TERMINAL.md`` without any UI:
the envelope round-trips, the handshake reaches a usable state from either side,
covert traffic decrypts both ways, and -- most importantly -- ordinary or hostile
traffic never leaks into the covert window.
"""

import io
import unittest

import numpy as np
from PIL import Image

import fixtures  # noqa: F401  (path setup for shared fixtures)
from markovcryptor import image_stego
from markovcryptor.core.errors import BadKey, CapacityExceeded, InvalidInput, NoHiddenFrame
from markovcryptor.terminal import LoopbackNetwork, Packet, Session, State, Terminal
from markovcryptor.terminal.protocol import (
    Kind,
    Mode,
    build_handshake,
    open_body,
    parse_handshake,
    peek_kind,
    seal_body,
    wrap,
)
from markovcryptor.terminal.terminal import _decode_packet, _encode_packet


def _png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _synthetic_cover(size=256, seed=42):
    """A deterministic test cover: tests only need a decodable image.

    ``seed`` lets a test tell two covers apart, which matters when checking that
    a handshake reply used *our* picture and not the peer's.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(64, 192, (size, size, 3), dtype=np.uint8), "RGB")


def _cover(size=256, seed=42):
    return _png_bytes(_synthetic_cover(size, seed))


def _tiny_cover():
    """A picture too small to hold a 38-byte handshake (64x64 -> capacity 0)."""
    return _cover(size=64)


def _pair(**kwargs):
    """Two terminals, each with its own one-picture queue.

    A handshake reply must travel in a picture the responder owns, so both sides
    are given a queue here. Tests that want "no picture yet" set
    ``bob.take_cover = None`` (or empty his queue) to check the held path.
    """
    net = LoopbackNetwork()
    alice = Terminal("alice", net, **kwargs).attach()
    bob = Terminal("bob", net, **kwargs).attach()
    for terminal in (alice, bob):
        _give_queue(terminal)
    return net, alice, bob


def _give_queue(terminal, count=1):
    """Stand-in for a client's picture queue; the web layer injects the real one."""
    queue = [_cover(seed=index + 1) for index in range(count)]
    terminal.take_cover = lambda: queue.pop(0) if queue else None
    return queue


def _handshake(net, alice, bob):
    """Complete a handshake the way two real users would.

    收方**不会自动回复** —— 它只挂起请求，等用户点「接受」。所以这里要模拟
    那个点击（``respond_handshake``），否则会话永远建不起来。

    这条行为变更是有意的：自动回复意味着"从收到到发出图只要 0.2 秒"，
    在 QQ 侧看就是机器行为，而且客户端挂着时任何人都能自动建立连接。
    """
    alice.send_handshake("bob", (_cover(),))
    net.pump()
    if bob.pending_request is not None:
        bob.respond_handshake()
        net.pump()


class EnvelopeTest(unittest.TestCase):
    def test_handshake_round_trip(self):
        payload = build_handshake(b"\x01" * 32, Mode.REQUEST)
        public, mac, mode = parse_handshake(payload)
        self.assertEqual(public, b"\x01" * 32)
        self.assertEqual(mac, b"", "不带 MAC 的帧仍可解析（兼容路径）")
        self.assertIs(mode, Mode.REQUEST)

    def test_handshake_carries_a_mac(self):
        """握手帧必须能带身份校验码 —— X25519 本身没有身份认证。"""
        mac = b"\xab" * 16
        payload = build_handshake(b"\x01" * 32, Mode.REQUEST, mac=mac)
        public, got_mac, mode = parse_handshake(payload)
        self.assertEqual(public, b"\x01" * 32)
        self.assertEqual(got_mac, mac)
        self.assertIs(mode, Mode.REQUEST)

    def test_handshake_fits_the_smallest_capable_cover(self):
        # The 38-byte envelope against real channel capacity, not a guess: this
        # ties the "a small picture can still start a session" claim to the
        # numbers in docs/CHANNELS.md.
        envelope = len(build_handshake(b"\x02" * 32, Mode.RESPONSE))
        self.assertLessEqual(envelope, image_stego.max_payload_bytes(120, 120))
        self.assertGreater(envelope, image_stego.max_payload_bytes(96, 96))

    def test_body_round_trip(self):
        key = b"\x03" * 32
        sealed = seal_body(Kind.BODY_TEXT, key, "秘密".encode())
        self.assertEqual(open_body(sealed, Kind.BODY_TEXT, key).decode(), "秘密")

    def test_wrong_key_is_badkey_not_garbage(self):
        sealed = seal_body(Kind.BODY_TEXT, b"\x04" * 32, b"hello")
        with self.assertRaises(BadKey):
            open_body(sealed, Kind.BODY_TEXT, b"\x05" * 32)

    def test_kind_is_authenticated_so_it_cannot_be_relabelled(self):
        key = b"\x06" * 32
        sealed = seal_body(Kind.BODY_TEXT, key, b"hello")
        # Same tag byte, different type letter -> the AAD changes -> rejected.
        tampered = sealed[:4] + Kind.BODY_IMAGE.value.encode() + sealed[5:]
        with self.assertRaises(BadKey):
            open_body(tampered, Kind.BODY_IMAGE, key)

    def test_peek_rejects_non_envelope(self):
        with self.assertRaises(NoHiddenFrame):
            peek_kind(b"JUNK1234")

    def test_envelope_too_short(self):
        with self.assertRaises(NoHiddenFrame):
            peek_kind(b"PB")

    def test_bad_public_key_length(self):
        with self.assertRaises(InvalidInput):
            build_handshake(b"\x00" * 31, Mode.REQUEST)


class SessionTest(unittest.TestCase):
    def test_direction_keys_are_opposite(self):
        a, b = Session(), Session()
        pa, _ = a.begin()
        b.absorb(pa, Mode.REQUEST)
        pb, _ = b.answer()
        a.absorb(pb, Mode.RESPONSE)
        self.assertTrue(a.established and b.established)
        self.assertEqual(a.send_key, b.recv_key)
        self.assertEqual(a.recv_key, b.send_key)
        self.assertNotEqual(a.send_key, a.recv_key)

    def test_responder_is_established_as_soon_as_it_answers(self):
        # The responder holds both keys, so waiting for another round trip would
        # deadlock: the initiator has nothing left to send.
        a, b = Session(), Session()
        pa, _ = a.begin()
        b.absorb(pa, Mode.REQUEST)
        self.assertFalse(b.established)
        b.answer()
        self.assertTrue(b.established)

    def test_cannot_send_ciphertext_before_established(self):
        with self.assertRaises(InvalidInput):
            Session().seal(Kind.BODY_TEXT, b"x")

    def test_fresh_handshake_means_old_body_fails(self):
        """重握手完成后，旧密钥的密文必须失效。

        注意：失效发生在重握手**完成**时，不是收到请求时。协议是无状态的，
        对端随时可以换新密钥；在它拿到我们的回复之前，旧会话仍然可用 ——
        这是刻意的，否则一次重握手就会把正在工作的信道打断。
        """
        a, b = Session(), Session()
        pa, _ = a.begin()
        b.absorb(pa, Mode.REQUEST)
        pb, _ = b.answer()
        a.absorb(pb, Mode.RESPONSE)
        old_body = a.seal(Kind.BODY_TEXT, b"old")

        a2, _ = Session(), None
        pa2, _ = a2.begin()
        b.absorb(pa2, Mode.REQUEST)
        # 请求已收到但还没回复：旧密钥仍然可用。
        self.assertEqual(b.open(Kind.BODY_TEXT, old_body), b"old")

        b.answer()
        b.commit_answer()
        with self.assertRaises(BadKey):
            b.open(Kind.BODY_TEXT, old_body)
        self.assertIsNotNone(old_body)


class PacketCodecTest(unittest.TestCase):
    def test_round_trip(self):
        from markovcryptor.terminal.terminal import _decode_packet

        original = Packet(text="你好", images=(b"IMG1", b"IMG2"))
        text, images = _decode_packet(_encode_packet(original))
        self.assertEqual(text, "你好")
        self.assertEqual(images, (b"IMG1", b"IMG2"))

    def test_text_only(self):
        from markovcryptor.terminal.terminal import _decode_packet

        text, images = _decode_packet(_encode_packet(Packet(text="hi")))
        self.assertEqual((text, images), ("hi", ()))

    def test_truncated_image_is_rejected(self):
        from markovcryptor.terminal.terminal import _decode_packet

        data = b"PKT1" + (1).to_bytes(2, "big") + (99).to_bytes(4, "big") + b"short"
        with self.assertRaises(InvalidInput):
            _decode_packet(data)


class TerminalFlowTest(unittest.TestCase):
    def test_plain_message_shows_and_stays_out_of_covert(self):
        net, alice, bob = _pair()
        alice.send_packet("bob", Packet(text="普通消息", images=(_cover(),)))
        net.pump()
        inbound = bob.inbox[-1]
        self.assertEqual(inbound.chat_text, "普通消息")
        self.assertEqual(len(inbound.chat_images), 1)
        self.assertEqual(inbound.covert, ())
        self.assertEqual(inbound.notes, ())

    def test_handshake_establishes_both_sides(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        self.assertTrue(alice.established)
        self.assertTrue(bob.established)
        self.assertEqual(alice.session.send_key, bob.session.recv_key)

    def test_response_waits_for_the_user(self):
        """收到握手请求**不能**自动回复，必须等用户确认。

        理由三条，都不是洁癖：

        * 自动回复从收到到发出图可能只要 0.2 秒，QQ 侧看就是机器行为，
          而这是个靠"看起来像正常聊天"活着的工具
        * 用户失去核对指纹的机会，TOFU 就白做了
        * **客户端挂着时，任何知道口令的人都能自动把连接建立起来**

        这条测试原来是 ``test_auto_response_needs_no_user_action``，断言
        相反的行为 —— 那是旧设计，已废弃。
        """
        net, alice, bob = _pair()
        alice.send_handshake("bob", (_cover(),))
        net.pump()

        senders = [sender for sender, _target, _pkt in net.history]
        self.assertEqual(senders.count("bob"), 0,
                         "bob 不该在用户确认前发出任何东西")
        self.assertIsNotNone(bob.pending_request, "请求应当被挂起")
        self.assertIn("等待你确认", bob.inbox[-1].handshake)

        # 用户点了「接受」
        self.assertTrue(bob.respond_handshake())
        net.pump()
        self.assertIsNone(bob.pending_request)
        self.assertTrue(alice.established and bob.established)

    def test_covert_text_arrives_in_covert_window_only(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        alice.send_covert_in_images("bob", "隐秘消息", (_cover(),), chat_text="公开说明")
        net.pump()
        inbound = bob.inbox[-1]
        self.assertEqual(inbound.covert, ("隐秘消息",))
        self.assertEqual(inbound.chat_text, "公开说明")

    def test_covert_works_in_both_directions(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        alice.send_covert_in_images("bob", "去", (_cover(),))
        net.pump()
        bob.send_covert_in_images("alice", "回", (_cover(),))
        net.pump()
        self.assertEqual(alice.inbox[-1].covert, ("回",))

    def test_ciphertext_send_requires_a_session(self):
        _net, alice, _bob = _pair()
        with self.assertRaises(InvalidInput) as caught:
            alice.send_covert_in_images("bob", "x", (_cover(),))
        self.assertIn("密钥交换", " ".join(caught.exception.hints))
        self.assertTrue(caught.exception.hints)

    def test_handshake_requires_an_image(self):
        _net, alice, _bob = _pair()
        with self.assertRaises(InvalidInput) as caught:
            alice.send_handshake("bob", ())
        self.assertIn("至少附带一张图片", str(caught.exception))

    def test_text_channel_unavailable_is_explained(self):
        net, alice, _bob = _pair()
        _handshake(net, alice, _bob)
        with self.assertRaises(InvalidInput) as caught:
            alice.send_covert_text("bob", "hi")
        self.assertIn("中文信道模型", str(caught.exception))
        self.assertTrue(caught.exception.hints)

    def test_too_small_cover_names_the_picture_and_the_shortfall(self):
        _net, alice, _bob = _pair()
        tiny = _png_bytes(_synthetic_cover(64))
        with self.assertRaises(CapacityExceeded) as caught:
            alice.send_handshake("bob", (tiny,))
        self.assertTrue(caught.exception.reasons)
        self.assertIn("第 1 张", caught.exception.reasons[0])
        self.assertTrue(caught.exception.hints)

    def test_handshake_refuses_more_than_one_image(self):
        # 握手是 38 字节的控制报文；多带图只会制造无载荷的图或浪费填充。
        _net, alice, _bob = _pair()
        with self.assertRaises(InvalidInput) as caught:
            alice.send_handshake("bob", (_cover(), _cover()))
        self.assertIn("只用一张图片", str(caught.exception))


class HandshakeAuthenticationTest(unittest.TestCase):
    """握手帧必须通过身份校验才允许进入会话状态。

    X25519 是**无认证**的 DH：能改写消息流的人可以分别和两端握手，全程
    解密再转发。这里的 MAC 用共享口令派生，挡住"不知道口令的人伪造帧"。
    知道口令的人冒充要靠 TOFU 指纹发现（见 tests/test_identity.py）。
    """

    def _pair(self, *, bob_psk="pbx1"):
        net = LoopbackNetwork()
        alice = Terminal("alice", net, psk="pbx1")
        alice.take_cover = lambda: _cover()
        alice.attach()
        bob = Terminal("bob", net, psk=bob_psk)
        bob.take_cover = lambda: _cover()
        bob.attach()
        return net, alice, bob

    def test_matching_passphrases_handshake(self):
        net, alice, bob = self._pair()
        _handshake(net, alice, bob)
        self.assertTrue(alice.established)
        self.assertTrue(bob.established)

    def test_mismatched_passphrase_is_rejected(self):
        """口令不同 → 连隐写载荷都提取不出来，更谈不上握手。

        注意这落在"图片里没有我们的帧"这个分支，而不是 MAC 失败分支 ——
        因为**不同口令连 QIM 系数都解不出来**。这是更强的拒绝：攻击者连
        "这里有个握手帧"都发现不了。

        界面上的表现是"这张图没有隐藏内容"，与普通表情包一致。这是有意的：
        把每张解不开的图都报成错误，等于告诉旁观者"这个客户端在找东西"。
        """
        net, alice, bob = self._pair(bob_psk="different-secret")
        alice.send_handshake("bob", (_cover(),))
        net.pump()

        self.assertFalse(bob.established, "口令不同不该建立会话")
        self.assertIsNone(bob.pending_request, "被拒的请求不能挂起等确认")

    def test_tampered_frame_is_rejected(self):
        """公钥与 MAC 对不上 → 拒绝。"""
        from markovcryptor.terminal.protocol import build_handshake
        net, alice, bob = self._pair()
        wrong_key = bytes([17]) * 32
        bad = build_handshake(wrong_key, Mode.REQUEST, mac=bytes(16))
        # 走真实路径：把伪造帧嵌进图片再发送，和攻击者能做到的一致。
        stego, _ = image_stego.embed(_synthetic_cover(), image_stego.build_frame(bad),
                                     image_stego.normalise_key("pbx1"))
        stego = _png_bytes(stego)
        net.send("alice", "bob", _encode_packet(Packet(images=(stego,))))
        net.pump()
        self.assertFalse(bob.established)
        self.assertIsNone(bob.pending_request)

    def test_missing_mac_is_rejected(self):
        """不带 MAC 的旧格式帧一律拒绝 —— 那是可以随便伪造的。"""
        from markovcryptor.terminal.protocol import build_handshake
        net, alice, bob = self._pair()
        legacy = build_handshake(bytes([34]) * 32, Mode.REQUEST)   # 无 MAC
        stego, _ = image_stego.embed(_synthetic_cover(), image_stego.build_frame(legacy),
                                     image_stego.normalise_key("pbx1"))
        stego = _png_bytes(stego)
        net.send("alice", "bob", _encode_packet(Packet(images=(stego,))))
        net.pump()
        self.assertFalse(bob.established)
        self.assertIsNone(bob.pending_request)


class HostileInputTest(unittest.TestCase):
    """Everything here must leave the covert window untouched."""

    def _assert_plain(self, inbound):
        self.assertEqual(inbound.covert, ())

    def test_forged_ciphertext_from_a_stranger_is_ignored(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        # An outsider knows the carrier psk but not the session key.
        forged = seal_body(Kind.BODY_IMAGE, b"\x11" * 32, "伪造".encode())
        image = Image.open(io.BytesIO(_cover())).convert("RGB")
        stego, _ = image_stego.embed(image, wrap(forged),
                                     image_stego.normalise_key("pbx1"))
        bob.deliver("eve", _encode_packet(Packet(images=(stego_bytes(stego),))))
        inbound = bob.inbox[-1]
        self._assert_plain(inbound)
        self.assertEqual(len(inbound.chat_images), 1)

    def test_wrong_psk_carrier_is_treated_as_plain(self):
        net = LoopbackNetwork()
        bob = Terminal("bob", net, psk="pbx1").attach()
        stranger = Terminal("eve", net, psk="another-psk").attach()
        stranger.send_packet("bob", Packet(images=(_cover(),)))
        net.pump()
        self._assert_plain(bob.inbox[-1])
        self.assertEqual(len(bob.inbox[-1].chat_images), 1)

    def test_garbage_image_is_survivable(self):
        net = LoopbackNetwork()
        bob = Terminal("bob", net).attach()
        alice = Terminal("alice", net).attach()
        alice.send_packet("bob", Packet(text="带坏图",
                                        images=(b"\x89PNG\r\n\x1a\n nonsense",)))
        net.pump()
        inbound = bob.inbox[-1]
        self._assert_plain(inbound)
        self.assertEqual(inbound.chat_text, "带坏图")
        self.assertTrue(inbound.notes)  # undecodable is worth telling the user

    def test_body_in_wrong_carrier_is_noted_not_decrypted(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        # A BODY_TEXT envelope hidden in an image: legal crypto, wrong carrier.
        envelope = alice.session.seal(Kind.BODY_TEXT, "放错载体".encode())
        image = Image.open(io.BytesIO(_cover())).convert("RGB")
        stego, _ = image_stego.embed(image, wrap(envelope),
                                     image_stego.normalise_key("pbx1"))
        bob.deliver("alice", _encode_packet(Packet(images=(stego_bytes(stego),))))
        inbound = bob.inbox[-1]
        self._assert_plain(inbound)
        self.assertTrue(any("载体不符" in note for note in inbound.notes))

    def test_plain_text_that_looks_like_hex_is_still_plain(self):
        net, _alice, bob = _pair()
        bob.deliver("alice", _encode_packet(Packet(text="deadbeef")))
        self.assertIsNone(bob.inbox[-1].covert or None)
        self.assertEqual(bob.inbox[-1].chat_text, "deadbeef")


class SendRulesTest(unittest.TestCase):
    """The two rules about *which* picture leaves the terminal.

    1. Never invent one. A covert or handshake send with no picture is refused.
    2. Use exactly the pictures the user picked, in the order they picked them,
       one payload slice each -- never substitute, reorder, or leave one unused.
    """

    def test_covert_without_images_is_refused(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        with self.assertRaises(InvalidInput) as caught:
            alice.send_covert_in_images("bob", "x", ())
        self.assertIn("至少一张图片", str(caught.exception))
        self.assertTrue(caught.exception.hints)

    def test_handshake_without_images_is_refused(self):
        _net, alice, _bob = _pair()
        with self.assertRaises(InvalidInput) as caught:
            alice.send_handshake("bob", ())
        self.assertIn("附带一张图片", str(caught.exception))
        self.assertTrue(caught.exception.hints)

    def test_nothing_is_sent_when_refused(self):
        net, alice, _bob = _pair()
        before = len(net.history)
        with self.assertRaises(InvalidInput):
            alice.send_handshake("bob", ())
        self.assertEqual(len(net.history), before, "被拒绝的发送不应产生任何报文")

    def test_every_image_carries_a_slice(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        images = (_cover(), _cover(), _cover())
        alice.send_covert_in_images("bob", "三张图各带一段", images)
        net.pump()
        inbound = bob.inbox[-1]
        # 三张图都在，且都成功解出分片（合起来才是完整密文）。
        self.assertEqual(len(inbound.chat_images), 3)
        self.assertEqual(inbound.covert, ("三张图各带一段",))

    def test_order_is_preserved(self):
        """Picture i must carry slice i; swapping them would break decryption."""
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        alice.send_covert_in_images("bob", "顺序", (_cover(256), _cover(320),
                                                    _cover(200)))
        net.pump()
        self.assertEqual(bob.inbox[-1].covert, ("顺序",))

    def test_one_image_too_small_fails_the_whole_send(self):
        net, alice, _bob = _pair()
        _handshake(net, alice, _bob)
        before = len(net.history)
        tiny = _png_bytes(_synthetic_cover(64))
        with self.assertRaises(CapacityExceeded) as caught:
            alice.send_covert_in_images("bob", "hi", (_cover(), tiny))
        # 报错要说清是第几张，且不发半截消息。
        self.assertIn("第 2 张", " ".join(caught.exception.reasons))
        self.assertTrue(caught.exception.hints)
        self.assertEqual(len(net.history), before, "有一张装不下就不应发出")

    def test_more_images_than_bytes_is_refused(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        # 空分片 = 有图不带载荷，正是观察者能一眼挑出的东西，所以直接拒绝。
        with self.assertRaises(InvalidInput) as caught:
            alice.send_covert_in_images("bob", "", (_cover(),))
        self.assertIn("密文内容为空", str(caught.exception))

    def test_split_payload_is_consecutive_and_lossless(self):
        from markovcryptor.terminal.protocol import parse_slice
        from markovcryptor.terminal.terminal import split_payload

        blob = bytes(range(200))
        for parts in (1, 3, 7, 200):
            envelopes = split_payload(blob, parts)
            self.assertEqual(len(envelopes), parts)
            chunks = []
            for position, envelope in enumerate(envelopes):
                index, count, chunk = parse_slice(envelope)
                self.assertEqual((index, count), (position, parts))
                # 每片都必须是合法 PBX1 信封：带无载荷图的拼接是不允许的。
                self.assertEqual(peek_kind(envelope), Kind.BODY_SLICE)
                self.assertTrue(chunk, "不允许出现空分片")
                chunks.append(chunk)
            self.assertEqual(b"".join(chunks), blob)
            sizes = {len(chunk) for chunk in chunks}
            self.assertLessEqual(max(sizes) - min(sizes), 1, "应当近等分")

    def test_incomplete_pieces_are_dropped_not_guessed(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        alice.send_covert_in_images("bob", "两段密文", (_cover(), _cover()))
        sender, target, packet = net.history[-1]
        text, raws = _decode_packet(packet)
        # 丢一张，模拟图片在途中损坏。
        bob.deliver(sender, _encode_packet(Packet(text=text, images=raws[:1])))
        inbound = bob.inbox[-1]
        self.assertEqual(inbound.covert, (), "分片不齐时不应拼出半截密文")
        self.assertTrue(any("分片不齐" in note for note in inbound.notes))

    def test_handshake_reply_uses_our_own_picture_not_theirs(self):
        """回包用本端队列的头一张图，不拿对端发来的图。

        拿对端的图回写有两个问题：一是等于替对方发了他自己的图（聊天记录里
        会出现两条一样的图），二是"本端只发自己选的图"这条规矩就破了。
        """
        net, alice, bob = _pair()
        theirs = _cover(seed=7)          # alice 的图
        ours = _cover(seed=99)           # bob 队列里的唯一一张
        bob.take_cover = lambda: ours
        alice.send_handshake("bob", (theirs,))
        net.pump()
        # 回复要用户点「接受」才发 —— 同时这也正是这条测试要验证的东西：
        # 回复里用的是**我们自己**的图，不是对方那张。
        self.assertIsNotNone(bob.pending_request)
        self.assertTrue(bob.respond_handshake())
        net.pump()

        bob_packets = [pkt for sender, _target, pkt in net.history if sender == "bob"]
        self.assertEqual(len(bob_packets), 1)
        _text, raws = _decode_packet(bob_packets[0])
        self.assertEqual(len(raws), 1)
        # 嵌了载荷再编码，字节会变；要比的是图像内容是谁的。
        sent_pic = Image.open(io.BytesIO(raws[0])).convert("RGB")
        ours_pic = Image.open(io.BytesIO(ours)).convert("RGB")
        theirs_pic = Image.open(io.BytesIO(theirs)).convert("RGB")
        self.assertLess(_mean_diff(sent_pic, ours_pic), 3.0, "回包没用本端队列的图")
        self.assertGreater(_mean_diff(sent_pic, theirs_pic), 3.0, "回包用的是对端的图")
        self.assertTrue(bob.established and alice.established)

    def test_reply_waits_when_this_side_has_no_picture(self):
        """没图就先等着：保持 WAITING，等到有图再自动回，绝不丢请求。"""
        net, alice, bob = _pair()
        held = _give_queue(bob, count=0)
        alice.send_handshake("bob", (_cover(),))
        net.pump()

        # 对端还没收到回包，双方都不该声称建立。
        self.assertFalse(bob.established)
        self.assertFalse(alice.established)
        self.assertIsNotNone(bob.pending_request)
        self.assertEqual(bob.state.label, "等待对端回复")
        self.assertEqual(
            [p for s, _t, p in net.history if s == "bob"], [],
            "没图的时候不该发出任何报文")

        # 用户补上图片，回包才出去，然后双方同时可用。
        held.append(_cover())
        self.assertTrue(bob.retry_pending())
        net.pump()
        self.assertTrue(bob.established)
        self.assertTrue(alice.established)
        self.assertEqual(len([p for s, _t, p in net.history if s == "bob"]), 1)
        # 再调一次不会重复回包。
        self.assertFalse(bob.retry_pending())

    def test_pending_reply_is_held_until_a_capable_picture_arrives(self):
        """图太小照样算"没有可用图"：留着请求，等更大的图。"""
        net, alice, bob = _pair()
        too_small = _png_bytes(_synthetic_cover(48))
        queue = _give_queue(bob, count=0)
        queue.append(too_small)
        alice.send_handshake("bob", (_cover(),))
        net.pump()
        self.assertFalse(alice.established)
        self.assertIsNotNone(bob.pending_request)

        # 用户点「接受」，但队列里只有一张装不下握手帧的图 → 拒绝并保持挂起
        self.assertFalse(bob.respond_handshake(),
                         "图太小就不该发，而且请求要留着")
        self.assertIsNotNone(bob.pending_request, "失败的回复不能丢掉请求")

        queue.append(_cover())
        self.assertTrue(bob.retry_pending(), "补上能用的图之后应当能回复")
        net.pump()
        self.assertTrue(alice.established)


class LoopbackNetworkTest(unittest.TestCase):
    def test_unknown_target_raises(self):
        from markovcryptor.terminal.transport import TransportError

        with self.assertRaises(TransportError):
            LoopbackNetwork().send("a", "nobody", b"x")

    def test_pump_reports_delivered_count(self):
        net = LoopbackNetwork()
        got = []
        net.register("x", lambda sender, data: got.append((sender, data)))
        net.send("y", "x", b"one")
        net.send("y", "x", b"two")
        self.assertEqual(net.pump(), 2)
        self.assertEqual(got, [("y", b"one"), ("y", b"two")])


def _mean_diff(left, right):
    """Average per-channel difference; stego shifts pixels only slightly."""
    import numpy as np
    a = np.asarray(left, dtype=np.int16)
    b = np.asarray(right, dtype=np.int16)
    if a.shape != b.shape:
        return float("inf")
    return float(np.abs(a - b).mean())


def stego_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()


class RekeyTest(unittest.TestCase):
    """对端可以随时用新密钥重新握手，且不能打断正在工作的信道。

    协议是无状态的：对端不保存任何会话状态，随时可能丢弃旧密钥、发一张新的
    握手图。这必须支持 —— 但也必须**不伤到已建立的信道**。

    实际踩到的 bug：对端重握手、本端恰好没有待发图片时，`absorb()` 无条件把
    state 改成 WAITING，于是双方都变成"等待对端回复"，谁都发不了密文。
    一次重握手就把好好的信道打死，而且没法自行恢复。
    """

    def _pair(self):
        net = LoopbackNetwork()
        alice = Terminal("alice", net)
        alice.take_cover = lambda: _cover()
        alice.attach()
        bob = Terminal("bob", net)
        bob.take_cover = lambda: _cover()
        bob.attach()
        return net, alice, bob

    def _confirm(self, net, responder):
        """模拟用户点「接受」—— 收方不再自动回复。"""
        if responder.pending_request is not None:
            responder.respond_handshake()
            net.pump()

    def test_peer_rekey_keeps_session_usable(self):
        """重握手期间旧会话必须继续能收发密文。"""
        net, alice, bob = self._pair()
        alice.send_handshake("bob", (_cover(),))
        net.pump()
        self._confirm(net, bob)
        self.assertTrue(alice.session.established)
        self.assertTrue(bob.session.established)

        # bob 换新密钥，alice 此刻没有图 —— 这正是打死信道的那个场景。
        alice.take_cover = lambda: None
        bob.send_handshake("alice", (_cover(),))
        net.pump()

        self.assertEqual(alice.session.state.label, "已建立",
                         "重握手请求不能把已建立的会话打回等待")
        self.assertTrue(alice.session.rekeying, "应当知道有一个重握手在进行")
        self.assertIsNotNone(alice.pending_request, "请求被挂起等图")

        alice.send_covert_in_images("bob", "重握手期间", (_cover(),))
        net.pump()
        self.assertEqual(bob.inbox[-1].covert, ("重握手期间",),
                         "重握手期间旧密钥必须还能用")

    def test_rekey_completes_when_cover_becomes_available(self):
        """补上图之后重握手真正完成，generation 前进，新密钥双向可用。"""
        net, alice, bob = self._pair()
        alice.send_handshake("bob", (_cover(),))
        net.pump()
        self._confirm(net, bob)
        first_gen = alice.session.generation

        alice.take_cover = lambda: None
        bob.send_handshake("alice", (_cover(),))
        net.pump()

        alice.take_cover = lambda: _cover()
        self.assertTrue(alice.retry_pending(), "补图后应当自动完成回复")
        net.pump()

        self.assertEqual(alice.session.generation, first_gen + 1)
        self.assertEqual(bob.session.generation, first_gen + 1,
                         "两侧 generation 必须同步前进")
        self.assertFalse(alice.session.rekeying, "完成后不该还挂着")
        self.assertTrue(alice.session.established)
        self.assertTrue(bob.session.established)

        alice.send_covert_in_images("bob", "新密钥A", (_cover(),))
        net.pump()
        self.assertEqual(bob.inbox[-1].covert, ("新密钥A",))
        bob.send_covert_in_images("alice", "新密钥B", (_cover(),))
        net.pump()
        self.assertEqual(alice.inbox[-1].covert, ("新密钥B",),
                         "新密钥下反向也必须通")

    def test_repeated_rekeys_keep_working(self):
        """连续多次重握手都要能走通，不能只在第一次正确。"""
        net, alice, bob = self._pair()
        alice.send_handshake("bob", (_cover(),))
        net.pump()
        self._confirm(net, bob)

        for round_index in range(3):
            bob.send_handshake("alice", (_cover(),))
            net.pump()
            alice.send_covert_in_images("bob", f"第{round_index}轮", (_cover(),))
            net.pump()
            self.assertEqual(bob.inbox[-1].covert, (f"第{round_index}轮",),
                             f"第 {round_index} 轮重握手后信道必须仍可用")

    def test_failed_response_does_not_break_the_live_session(self):
        """回复发不出去（图太小）时，旧会话必须完好无损。

        answer() 曾经会立刻改密钥；若随后嵌图失败，密钥已经换了、回复却没发出，
        对端还在用旧密钥 —— 双方永久错位。
        """
        net, alice, bob = self._pair()
        alice.send_handshake("bob", (_cover(),))
        net.pump()
        self._confirm(net, bob)
        gen_before = alice.session.generation

        # 先让 alice 手上只有一张装不下 38 字节握手的小图，再让 bob 重握手 ——
        # 否则 alice 会立刻用大图回复，重握手当场就完成了，测不到失败路径。
        alice.take_cover = lambda: _tiny_cover()
        bob.send_handshake("alice", (_cover(),))
        net.pump()
        self.assertIsNotNone(alice.pending_request, "小图回复失败，请求应当仍挂着")
        alice.retry_pending()
        net.pump()

        self.assertEqual(alice.session.generation, gen_before,
                         "回复没发出去就不能换密钥")
        alice.send_covert_in_images("bob", "仍然可用", (_cover(),))
        net.pump()
        self.assertEqual(bob.inbox[-1].covert, ("仍然可用",))
