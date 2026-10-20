"""OneBot transport tests: the seam between the terminal and a real QQ.

Nothing here touches a network or a real QQ account. ``FakeOneBot`` is an
in-process stand-in that speaks the same JSON, which is what keeps CI green on a
machine with no QQ installed and -- more importantly -- what lets the *grouping*
rules be tested exactly rather than hoped at.

The claims under test, in order of how easy they are to get wrong:

1. a packet's pictures are marked and can be put back together after arriving
   separately and out of order;
2. an ordinary chat picture (no trailer) is still delivered, and is never
   spliced into somebody else's packet;
3. base64 segments are read without a download, and a dead URL is dropped
   without killing the reader;
4. the actions we emit are the ones OneBot actually understands.
"""

import asyncio
import base64
import io
import json
import time
import unittest

import fixtures  # noqa: F401  (path setup for shared fixtures)
from markovcryptor.terminal.onebot import (
    TRAILER_MAGIC,
    OneBotNetwork,
    Reassembler,
    attach_trailer,
    frames_for,
    split_segments,
    strip_trailer,
)
from markovcryptor.terminal.terminal import Packet, _decode_packet, _encode_packet
from markovcryptor.terminal.transport import TransportError


def _png(color=(120, 130, 140), size=(256, 256)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def _b64_png(color=(120, 130, 140)) -> str:
    return "base64://" + base64.b64encode(_png(color)).decode("ascii")


class TrailerTest(unittest.TestCase):
    def test_trailer_round_trips(self):
        raw = _png()
        marked = attach_trailer(raw, 0x0123456789ABCDEF, 2, 5)
        body, packet_id, index, count = strip_trailer(marked)
        self.assertEqual(body, raw)
        self.assertEqual(packet_id, 0x0123456789ABCDEF)
        self.assertEqual((index, count), (2, 5))

    def test_image_survives_the_trailer(self):
        """The picture must still decode -- the trailer rides after IEND."""
        from PIL import Image
        raw = _png()
        marked = attach_trailer(raw, 7, 0, 1)
        image = Image.open(io.BytesIO(marked))
        image.load()
        self.assertEqual(image.size, (256, 256))
        self.assertEqual(marked[: len(raw)], raw, "原图字节应当原封不动留在前面")

    def test_unmarked_image_is_not_an_error(self):
        """An ordinary chat photo is the normal case, not a fault."""
        self.assertIsNone(strip_trailer(_png()))

    def test_trailer_with_nonsense_numbers_is_ignored(self):
        """Corruption must not splice a picture into the wrong position."""
        raw = _png() + TRAILER_MAGIC + (1).to_bytes(8, "big") + bytes((9, 3))
        self.assertIsNone(strip_trailer(raw), "index >= count 应当被拒绝而不是硬猜")

    def test_rejects_out_of_range(self):
        with self.assertRaises(TransportError):
            attach_trailer(_png(), 1, 3, 3)


class ReassemblerTest(unittest.TestCase):
    def test_out_of_order_pictures_are_put_back_in_order(self):
        """QQ does not promise order. Index, not arrival, decides position."""
        first, second, third = _png((1, 1, 1)), _png((2, 2, 2)), _png((3, 3, 3))
        done: list[bytes] = []
        reasm = Reassembler()
        done += reasm.feed("bob", None, [attach_trailer(third, 5, 2, 3)])
        done += reasm.feed("bob", None, [attach_trailer(first, 5, 0, 3)])
        self.assertEqual(done, [], "还差一片就不该交付")
        done += reasm.feed("bob", "说明文字", [attach_trailer(second, 5, 1, 3)])
        self.assertEqual(len(done), 1)
        text, images = _decode_packet(done[0])
        self.assertEqual(text, "说明文字")
        self.assertEqual(images, (first, second, third), "必须按 index 还原顺序")

    def test_two_peers_can_use_the_same_packet_id(self):
        """Ids are per-sender; a collision across peers must not merge them."""
        mine, theirs = _png((1, 2, 3)), _png((4, 5, 6))
        reasm = Reassembler()
        self.assertEqual(reasm.feed("alice", None, [attach_trailer(mine, 9, 0, 2)]), [])
        self.assertEqual(reasm.feed("bob", None, [attach_trailer(theirs, 9, 0, 2)]), [])
        alice = reasm.feed("alice", None, [attach_trailer(_png((7, 7, 7)), 9, 1, 2)])
        bob = reasm.feed("bob", None, [attach_trailer(_png((8, 8, 8)), 9, 1, 2)])
        self.assertEqual(len(alice), 1)
        self.assertEqual(len(bob), 1)
        self.assertEqual(_decode_packet(alice[0])[1][0], mine)
        self.assertEqual(_decode_packet(bob[0])[1][0], theirs)

    def test_loose_pictures_are_their_own_packet(self):
        """A whole message of untrailered pictures is one packet, as sent."""
        one, two = _png((1, 1, 1)), _png((2, 2, 2))
        done = Reassembler().feed("bob", "在吗", [one, two])
        self.assertEqual(len(done), 1)
        text, images = _decode_packet(done[0])
        self.assertEqual(text, "在吗")
        self.assertEqual(images, (one, two))

    def test_loose_pictures_never_join_a_partial(self):
        """The dangerous case: an ordinary photo must not complete our packet."""
        mine = _png((1, 1, 1))
        reasm = Reassembler()
        self.assertEqual(reasm.feed("bob", None, [attach_trailer(mine, 4, 0, 2)]), [])
        loose = reasm.feed("bob", None, [_png((9, 9, 9))])
        self.assertEqual(len(loose), 1)
        self.assertEqual(_decode_packet(loose[0])[1], (_png((9, 9, 9)),),
                         "散图自成一路，不能顶缺片")
        # The partial is still waiting for its real second picture.
        rest = reasm.feed("bob", None, [attach_trailer(_png((2, 2, 2)), 4, 1, 2)])
        self.assertEqual(len(rest), 1)

    def test_incomplete_packet_expires(self):
        """A lost picture must not leak the partial forever."""
        reasm = Reassembler(timeout=0.05)
        reasm.feed("bob", None, [attach_trailer(_png(), 1, 0, 2)])
        self.assertEqual(len(reasm._partials), 1)
        time.sleep(0.1)
        # 只收文字也会触发过期清理；过期的那包不能被交付。
        self.assertEqual(reasm.feed("bob", "闲聊", []),
                         [_encode_packet(Packet(text="闲聊"))])
        self.assertEqual(reasm._partials, {}, "过期的那包必须清掉")
        # 晚到的第二片不能再把旧包“补”完整。
        self.assertEqual(reasm.feed("bob", None, [attach_trailer(_png(), 1, 1, 2)]), [],
                         "孤片不该凭空成包")


class FramesForTest(unittest.TestCase):
    def test_one_picture_is_one_message(self):
        actions = frames_for("1741473279", _encode_packet(Packet(images=(_png(),))))
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action["action"], "send_private_msg")
        self.assertEqual(action["params"]["user_id"], 1741473279,
                         "user_id 必须是数字，字符串会被 OneBot 拒掉")
        self.assertEqual(action["params"]["message"][0]["type"], "image")

    def test_many_pictures_go_in_one_message(self):
        """一条 QQ 消息本来就装得下文本 + 多张图，不要拆。

        P0 实测就是这么发的（一条 `send_private_msg` 带 3 个 image 段），
        收回来的也是合并成一条、顺序不变、逐字节一致。
        """
        images = (_png((1, 1, 1)), _png((2, 2, 2)))
        actions = frames_for("1", _encode_packet(Packet(images=images)))
        self.assertEqual(len(actions), 1, "两张图应当一条消息发完")
        message = actions[0]["params"]["message"]
        self.assertEqual([seg["type"] for seg in message], ["image", "image"],
                         "顺序就是用户选的顺序")

    def test_every_picture_carries_its_own_trailer(self):
        """装在一条消息里，也照样带分组头。

        QQ 可能把一条多图消息拆开推送（取决于版本/风控/是否群聊），
        那时收侧只能靠这个头重组。14 字节的保险，看不见也不影响像素。
        """
        images = (_png((1, 1, 1)), _png((2, 2, 2)))
        actions = frames_for("1", _encode_packet(Packet(images=images)))
        message = actions[0]["params"]["message"]
        self.assertEqual(len(message), 2)
        seen = []
        for segment in message:
            raw = base64.b64decode(segment["data"]["file"][len("base64://"):])
            body, packet_id, index, count = strip_trailer(raw)
            seen.append((packet_id, index, count))
            self.assertIn(body, images)
        self.assertEqual(len({pid for pid, _, _ in seen}), 1, "同一包必须同一个 id")
        self.assertEqual([i for _, i, _ in seen], [0, 1])
        self.assertEqual({c for _, _, c in seen}, {2})

    def test_frames_split_is_the_documented_fallback(self):
        """一条消息装不下时（群聊/严格版本）要能改成一图一条。

        分组头一样，所以收侧无法区分两种发法 —— 这正是它能中途切换的原因。
        """
        from markovcryptor.terminal.onebot import frames_split

        images = (_png((1, 1, 1)), _png((2, 2, 2)), _png((3, 3, 3)))
        actions = frames_split("1", _encode_packet(Packet(images=images)))
        self.assertEqual(len(actions), 3)
        ids, indexes = set(), []
        for action in actions:
            raw = base64.b64decode(
                action["params"]["message"][-1]["data"]["file"][len("base64://"):])
            _body, pid, index, count = strip_trailer(raw)
            ids.add(pid)
            indexes.append(index)
            self.assertEqual(count, 3)
        self.assertEqual(len(ids), 1)
        self.assertEqual(indexes, [0, 1, 2])

    def test_text_rides_with_the_first_picture(self):
        """A separate text message would arrive with no packet id at all."""
        actions = frames_for("1", _encode_packet(
            Packet(images=(_png(), _png()), text="封面说明")))
        self.assertEqual(actions[0]["params"]["message"][0]["type"], "text")
        self.assertEqual(actions[0]["params"]["message"][0]["data"]["text"], "封面说明")
        for later in actions[1:]:
            kinds = [seg["type"] for seg in later["params"]["message"]]
            self.assertEqual(kinds, ["image"], "文字只跟第一张走，不重复")

    def test_text_only_is_a_plain_message(self):
        actions = frames_for("1", _encode_packet(Packet(text="晚安")))
        self.assertEqual(len(actions), 1)
        segments = actions[0]["params"]["message"]
        self.assertEqual(segments, [{"type": "text", "data": {"text": "晚安"}}])

    def test_empty_packet_is_refused(self):
        with self.assertRaises(TransportError):
            frames_for("1", _encode_packet(Packet()))

    def test_non_numeric_target_is_named(self):
        with self.assertRaises(TransportError) as caught:
            frames_for("alice", _encode_packet(Packet(text="hi")))
        self.assertIn("QQ", str(caught.exception))


class SegmentTest(unittest.TestCase):
    def test_splits_text_and_images(self):
        text, images = split_segments([
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"file": "base64://abc", "url": "http://x/y.png"}},
            {"type": "face", "data": {"id": "1"}},
        ])
        self.assertEqual(text, "看这个")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["url"], "http://x/y.png")

    def test_string_message_format_still_reads(self):
        """A hand-edited config could say string; do not go silent."""
        network = OneBotNetwork("ws://127.0.0.1:1/", "1")
        collected: list = []
        network.register("1", lambda sender, packet: collected.append(packet))
        asyncio.run(network._handle_event({
            "post_type": "message", "message_type": "private",
            "user_id": 2, "message": "纯文本消息",
        }))
        self.assertEqual(len(collected), 1)
        self.assertEqual(_decode_packet(collected[0])[0], "纯文本消息")


class FakeOneBot:
    """Records what the transport sends, and can push events back."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self):
        await asyncio.Event().wait()          # blocks until cancelled


class NetworkTest(unittest.TestCase):
    def _network(self) -> tuple[OneBotNetwork, FakeOneBot, list]:
        link = FakeOneBot()
        collected: list[tuple[str, bytes]] = []
        network = OneBotNetwork("ws://127.0.0.1:1/", "3925697820",
                                connect_factory=lambda url, token: _Link(link))
        network.register("3925697820",
                         lambda sender, packet: collected.append((sender, packet)))
        return network, link, collected

    def test_publishing_puts_actions_on_the_wire(self):
        network, link, _ = self._network()

        async def scenario():
            task = asyncio.create_task(network._write_loop(link))
            network.send("3925697820", "1741473279",
                         _encode_packet(Packet(images=(_png(),))))
            for _ in range(100):
                if link.sent:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())
        self.assertEqual(len(link.sent), 1)
        self.assertEqual(link.sent[0]["params"]["user_id"], 1741473279)

    def test_incoming_base64_image_needs_no_download(self):
        """The common case on another machine: no fetchable URL, inline bytes."""
        network, _, collected = self._network()
        asyncio.run(network._handle_event({
            "post_type": "message", "message_type": "private", "user_id": 1741473279,
            "message": [{"type": "image", "data": {"file": _b64_png()}}],
        }))
        self.assertEqual(len(collected), 1)
        sender, packet = collected[0]
        self.assertEqual(sender, "1741473279")
        self.assertEqual(_decode_packet(packet)[1], (_png(),))

    def test_group_and_self_messages_are_ignored(self):
        network, _, collected = self._network()
        for payload in (
            {"post_type": "message", "message_type": "group", "group_id": 1,
             "user_id": 2, "message": "群里的话"},
            {"post_type": "message", "message_type": "private",
             "user_id": 3925697820, "message": "自己的回显"},
            {"post_type": "meta_event", "meta_event_type": "heartbeat"},
        ):
            asyncio.run(network._handle_event(payload))
        self.assertEqual(collected, [], "群聊/自己/心跳都不该进终端")

    def test_unreachable_image_is_dropped_not_fatal(self):
        network, _, collected = self._network()
        asyncio.run(network._handle_event({
            "post_type": "message", "message_type": "private", "user_id": 7,
            "message": [{"type": "image",
                         "data": {"file": "x.png", "url": "http://127.0.0.1:1/nope"}}],
        }))
        self.assertEqual(collected, [])
        self.assertEqual(network.stats["dropped"], 1, "坏图要计数，不要抛出去")

    def test_text_only_event_reaches_the_terminal(self):
        """Ordinary chat must flow: the terminal decides it is not ours."""
        network, _, collected = self._network()
        asyncio.run(network._handle_event({
            "post_type": "message", "message_type": "private", "user_id": 7,
            "message": [{"type": "text", "data": {"text": "在吗"}}],
        }))
        self.assertEqual(len(collected), 1)
        self.assertEqual(_decode_packet(collected[0][1])[0], "在吗")


class _Link:
    """Async context manager handing out a pre-made fake link."""

    def __init__(self, link):
        self.link = link

    async def __aenter__(self):
        return self.link

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()


class TwoSidesOverOneBotTest(unittest.TestCase):
    """The whole point: two terminals talk, handshake, then swap ciphertext.

    Uses ``InProcessOneBot`` -- the in-memory equivalent of two SnowLuma
    endpoints -- so the *real* transport code runs (framing, trailers,
    reassembly, base64) with no QQ account and no network.
    """

    def _pair(self, psk="共享口令-123"):
        from markovcryptor.terminal import Terminal
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        sides = {}
        for label, uin in (("alice", "10001"), ("bob", "10002")):
            network = hub.connect(uin)
            terminal = Terminal(uin, network, psk=psk).attach()
            terminal.take_cover = (lambda seed=[0]: _png((10 + seed[0], 20, 30)))
            sides[label] = (terminal, network)
        return hub, sides

    def test_handshake_establishes_matching_keys(self):
        hub, sides = self._pair()
        alice, _ = sides["alice"]
        bob, _ = sides["bob"]

        alice.send_handshake("10002", (_png((11, 21, 31)),))
        hub.pump()

        self.assertTrue(alice.established, "发起方应在收到回包后建立会话")
        self.assertTrue(bob.established, "应答方派生密钥后即为建立")
        self.assertEqual(alice.session.send_key, bob.session.recv_key,
                         "一端的发送键必须等于另一端的接收键")
        self.assertEqual(alice.session.recv_key, bob.session.send_key)

    def test_ciphertext_survives_out_of_order_delivery(self):
        """QQ does not promise order, so neither may we rely on it."""
        hub, sides = self._pair()
        alice, _ = sides["alice"]
        bob, _ = sides["bob"]
        alice.send_handshake("10002", (_png((11, 21, 31)),))
        hub.pump()

        secret = "今晚八点，老地方。"
        alice.send_covert_in_images("10002", secret,
                                    (_png((1, 2, 3)), _png((4, 5, 6))))
        hub.pump(reverse=True)          # 后进先出，故意乱序
        got = [text for item in bob.inbox for text in item.covert]
        self.assertEqual(got, [secret], "跨图分片必须按序号拼回原文")

    def test_both_directions_work(self):
        hub, sides = self._pair()
        alice, _ = sides["alice"]
        bob, _ = sides["bob"]
        alice.send_handshake("10002", (_png((11, 21, 31)),))
        hub.pump()

        alice.send_covert_in_images("10002", "去吗？", (_png(),))
        hub.pump()
        bob.send_covert_in_images("10001", "去。", (_png((2, 2, 2)),))
        hub.pump()

        self.assertEqual([t for i in bob.inbox for t in i.covert], ["去吗？"])
        self.assertEqual([t for i in alice.inbox for t in i.covert], ["去。"])

    def test_reassembles_when_qq_splits_the_message(self):
        """兜底路径：QQ 真把一条多图消息拆成几条，也要能拼回去。

        这不是假设——同一条实测里也提到"也可能被拆开"。分组头存在的唯一
        理由就是这个，所以必须有一条测试真的把它拆开。
        """
        hub, sides = self._pair()
        alice, _ = sides["alice"]
        bob, _ = sides["bob"]
        alice.send_handshake("10002", (_png((11, 21, 31)),))
        hub.pump()
        bob.inbox.clear()

        hub.split_messages = True
        secret = "拆开也要能拼回来"
        alice.send_covert_in_images("10002", secret,
                                    (_png((1, 2, 3)), _png((4, 5, 6)), _png((7, 8, 9))))
        hub.pump()

        got = [text for item in bob.inbox for text in item.covert]
        self.assertEqual(got, [secret], "被拆成三条消息后仍应按序号拼回原文")

    def test_ordinary_chat_never_reaches_the_covert_window(self):
        """The security claim, over the real transport.

        A photo the peer sends with no hidden frame must arrive as ordinary
        chat and must not put anything in the covert window.
        """
        hub, sides = self._pair()
        alice, net_a = sides["alice"]
        bob, _ = sides["bob"]
        alice.send_handshake("10002", (_png((11, 21, 31)),))
        hub.pump()
        bob.inbox.clear()

        # A plain message from Alice: an ordinary picture and a sentence.
        net_a.send("10001", "10002", _encode_packet(
            Packet(images=(_png((200, 100, 50)),), text="看这张照片")))
        hub.pump()

        # 普通内容**要**进明文窗——那才是聊天软件的样子；只要密文窗不动。
        self.assertEqual(len(bob.inbox), 1, "普通消息应当作为明文送达")
        inbound = bob.inbox[0]
        self.assertEqual(inbound.chat_text, "看这张照片")
        self.assertEqual(len(inbound.chat_images), 1)
        self.assertEqual(inbound.covert, (), "普通图片绝不能进密文窗")
        self.assertEqual(inbound.notes, (), "普通图片是常态，不该报警")
        self.assertEqual(len(bob.session.recv_key), 32, "会话不该被打扰")

    def test_packet_split_across_messages_arrives_whole(self):
        """Two pictures leave as two messages and come back as one packet."""
        hub, sides = self._pair()
        alice, net_a = sides["alice"]
        bob, _ = sides["bob"]

        first, second = _png((1, 1, 1)), _png((2, 2, 2))
        net_a.send("10001", "10002", _encode_packet(
            Packet(images=(first, second), text="两张图")))
        self.assertEqual(len(hub.pending), 1, "两张图一条消息发完")
        hub.pump()

        self.assertEqual(len(bob.inbox), 1, "应当是一个包")
        inbound = bob.inbox[0]
        self.assertEqual(inbound.chat_text, "两张图")
        self.assertEqual(len(inbound.chat_images), 2)
