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
        # 收方不自动回包 —— 模拟用户点「接受」。
        bob.respond_handshake()
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
        # 收方不自动回包 —— 模拟用户点「接受」。
        bob.respond_handshake()
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
        # 收方不自动回包 —— 模拟用户点「接受」。
        bob.respond_handshake()
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
        # 收方不自动回包 —— 模拟用户点「接受」。
        bob.respond_handshake()
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
        # 收方不自动回包 —— 模拟用户点「接受」。
        bob.respond_handshake()
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


class PeerRoutingTest(unittest.TestCase):
    """One QQ account, several friends. Each conversation must be separate.

    The account talks to many friends at once, and every friend is its own
    session with its own keys. A sink that receives *every* peer would hand
    friend A's picture to friend B's session, where it fails authentication and
    surfaces as a mysterious "解不开" note -- a bug that only appears once a
    second friend exists, which is exactly when it is hardest to explain.
    """

    def _network_with_friends(self, friends=("20001", "20002", "20003")):
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        net = hub.connect("10001")
        got = {friend: [] for friend in friends}
        for friend in friends:
            net.register("10001",
                         lambda sender, packet, f=friend: got[f].append(sender),
                         peer=friend)
        return hub, net, got

    # ------------------------------------------------------- routing decision

    def test_only_the_addressed_friend_is_selected(self):
        """路由判定：只挑那个 peer 的 sink，别人的一个都不能碰。"""
        _, net, _ = self._network_with_friends()
        chosen = net._sinks_for("20002")
        self.assertEqual(len(chosen), 1, "只有一个 sink 该收到这条消息")

    def test_unknown_sender_selects_nobody(self):
        """陌生人发来的消息不能随便塞进某个会话。"""
        _, net, _ = self._network_with_friends()
        self.assertEqual(net._sinks_for("29999"), [],
                         "没有任何 sink 认领陌生人 —— 不能退化成随便挑一个")

    def test_broadcast_sink_still_works_for_the_demo(self):
        """测试端只注册一个 sink、不指定 peer —— 老行为必须保留。"""
        from markovcryptor.terminal.onebot import InProcessOneBot

        net = InProcessOneBot().connect("10001")
        net.register("10001", lambda sender, packet: None)
        self.assertEqual(len(net._sinks_for("20001")), 1)

    def test_reregistering_a_peer_replaces_its_sink(self):
        """重连时不能留下死 sink，否则每个包会被投递两次。"""
        from markovcryptor.terminal.onebot import InProcessOneBot

        net = InProcessOneBot().connect("10001")
        net.register("10001", lambda s, p: None, peer="20001")
        net.register("10001", lambda s, p: None, peer="20001")
        self.assertEqual(len(net._sinks_for("20001")), 1)

    # ------------------------------------------------------------- end to end

    def test_two_friends_do_not_cross_talk(self):
        """真实链路：两个好友同时发图，各自只进各自的会话。"""
        hub, net, got = self._network_with_friends(("20002", "20003"))
        bob = hub.connect("20002")
        carol = hub.connect("20003")

        bob.send("20002", "10001", _encode_packet(Packet(text="来自 bob")))
        carol.send("20003", "10001", _encode_packet(Packet(text="来自 carol")))
        hub.pump()

        self.assertEqual(got["20002"], ["20002"], "bob 的消息只进 bob 的会话")
        self.assertEqual(got["20003"], ["20003"], "carol 的消息只进 carol 的会话")


class AskTest(unittest.TestCase):
    """请求-响应。隐写只发不收，但好友列表/历史消息全是这么取的。"""

    def test_friend_list_comes_back(self):
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        hub.friends = {"10001": [
            {"user_id": 20001, "nickname": "三马赫", "remark": ""},
            {"user_id": 20002, "nickname": "x", "remark": "备注名"},
        ]}
        net = hub.connect("10001")
        friends = net.friend_list()
        self.assertEqual([f["user_id"] for f in friends], [20001, 20002])
        self.assertEqual(friends[1]["remark"], "备注名")

    def test_login_info_answers(self):
        from markovcryptor.terminal.onebot import InProcessOneBot

        net = InProcessOneBot().connect("10001")
        self.assertEqual(net.login_info()["user_id"], 10001)

    def test_history_answers(self):
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        hub.history_of = {("10001", "20001"): [{"message_id": 1}]}
        net = hub.connect("10001")
        self.assertEqual(net.friend_history("20001"), [{"message_id": 1}])

    def test_unknown_action_says_so_instead_of_guessing(self):
        """不认识的动作要明确报错，别返回空让上层以为"没有好友"。"""
        from markovcryptor.terminal.onebot import InProcessOneBot
        from markovcryptor.terminal.transport import TransportError

        net = InProcessOneBot().connect("10001")
        with self.assertRaises(TransportError):
            net.ask("get_unheard_of_thing")

    def test_reply_is_routed_by_echo_and_not_treated_as_event(self):
        """同一根 socket 上动作回包和事件混在一起，只能靠 echo 分辨。"""
        import asyncio
        from markovcryptor.terminal.onebot import OneBotNetwork

        net = OneBotNetwork("ws://unused", "10001")

        async def scenario():
            fut = asyncio.get_running_loop().create_future()
            net._pending["mkx-ask-7"] = fut
            handled = net._resolve({"status": "ok", "echo": "mkx-ask-7",
                                    "data": {"user_id": 1}})
            return handled, fut.result()

        handled, data = asyncio.run(scenario())
        self.assertTrue(handled, "带 echo 的回包必须被认成回包")
        self.assertEqual(data["data"], {"user_id": 1})

    def test_read_loop_does_not_mistake_a_reply_for_a_message(self):
        """读循环必须靠 echo 把回包和事件分开。

        直接测 ``_resolve`` 是不够的：那只证明它"能认"，不能证明读循环
        "真的调它"。OneBot 把动作回包和消息事件塞在同一根 socket 上，
        漏了这一步的后果是回包被当成消息投进会话。
        """
        import asyncio
        from markovcryptor.terminal.onebot import OneBotNetwork

        net = OneBotNetwork("ws://unused", "10001")

        class Link:
            def __init__(self):
                self.lines = [
                    json.dumps({"status": "ok", "echo": "mkx-ask-1",
                                "data": {"user_id": 10001}}),
                ]
                self.done = False

            async def recv(self):
                if self.lines:
                    return self.lines.pop(0)
                await asyncio.sleep(3600)      # 读循环永远挂着，别退出

        leaked = []
        net._event_sinks.append(lambda s, p: leaked.append(p))

        async def scenario():
            fut = asyncio.get_running_loop().create_future()
            net._pending["mkx-ask-1"] = fut
            task = asyncio.create_task(net._read_loop(Link()))
            try:
                # 回包必须把等待者叫醒；不叫醒就是 ask 永远超时。
                data = await asyncio.wait_for(fut, 1.0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return data

        data = asyncio.run(scenario())
        self.assertEqual(data["data"], {"user_id": 10001}, "读循环要把回包交给等待者")
        self.assertEqual(leaked, [], "动作回包不是消息，不能投给界面")

    def test_late_reply_is_dropped_not_raised(self):
        """调用方超时放弃后，迟到的回包不能把读循环炸掉。"""
        from markovcryptor.terminal.onebot import OneBotNetwork

        net = OneBotNetwork("ws://unused", "10001")
        self.assertFalse(net._resolve({"echo": "nobody-waits-for-this"}))


class InboundEventTest(unittest.TestCase):
    """1.3：普通消息也要能上到界面。

    隐写层对"图里没有密文"刻意静默（那是最常见情况），若界面只监听隐写层，
    用户发一张普通图我们这边界面上什么都不显示 —— 联调时真的误判成丢消息。
    """

    def _feed(self, net, payload):
        import asyncio
        asyncio.run(net._handle_event(payload))

    def test_plain_message_reaches_the_callback(self):
        from markovcryptor.terminal.onebot import InProcessOneBot

        net = InProcessOneBot().connect("10001")
        seen = []
        net.on_event(lambda sender, payload: seen.append((sender, payload)))
        self._feed(net, {"post_type": "message", "message_type": "private",
                         "user_id": 20001,
                         "message": [{"type": "text", "data": {"text": "在吗"}}]})
        self.assertEqual(len(seen), 1, "普通文本消息必须上到界面")
        self.assertEqual(seen[0][0], "20001")

    def test_a_broken_callback_does_not_kill_the_reader(self):
        from markovcryptor.terminal.onebot import InProcessOneBot

        net = InProcessOneBot().connect("10001")
        seen = []

        def boom(sender, payload):
            raise RuntimeError("界面挂了")

        net.on_event(boom)
        net.on_event(lambda sender, payload: seen.append(sender))
        self._feed(net, {"post_type": "message", "message_type": "private",
                         "user_id": 20001,
                         "message": [{"type": "text", "data": {"text": "hi"}}]})
        self.assertEqual(seen, ["20001"], "一个回调炸了不能拖累后面那个")


class TransportErrorShapeTest(unittest.TestCase):
    """错误类型必须接受 hints，否则错误处理路径自己会崩。

    实际撞上过：`ask()` 在没连上时抛 `TransportError(..., hints=(...))`，
    而 `TransportError` 是裸 RuntimeError，不接受关键字参数 —— 于是"QQ 没
    登录"这个最该说人话的场景，用户看到的是一坨 TypeError traceback。
    错误路径崩掉比错误本身更难查。
    """

    def test_accepts_hints(self):
        from markovcryptor.terminal.transport import TransportError

        exc = TransportError("连不上", hints=("检查 QQ 是否登录",))
        self.assertEqual(exc.hints, ("检查 QQ 是否登录",))
        self.assertIn("连不上", str(exc))

    def test_hints_default_to_empty(self):
        from markovcryptor.terminal.transport import TransportError

        self.assertEqual(TransportError("x").hints, ())

    def test_ask_without_connection_raises_transport_error_not_typeerror(self):
        """没连上时 ask 抛的必须是 TransportError，不能是 TypeError。"""
        from markovcryptor.terminal.onebot import OneBotNetwork
        from markovcryptor.terminal.transport import TransportError

        net = OneBotNetwork("ws://unused", "10001")     # 从未 start()
        with self.assertRaises(TransportError) as ctx:
            net.ask("get_login_info")
        self.assertTrue(ctx.exception.hints, "要带一句可执行的建议")
