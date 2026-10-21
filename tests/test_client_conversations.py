"""Conversations: lazy creation, isolation between friends, unread accounting.

The isolation tests matter most. A QQ account serves many friends over one
connection, so "A's message must not reach B's session" is the property that
decides whether this client is usable at all -- and it is invisible until a
second friend exists.
"""

from __future__ import annotations

import unittest

from markovcryptor.client.conversations import Conversations
from markovcryptor.client.directory import Directory
from markovcryptor.terminal.onebot import InProcessOneBot
from markovcryptor.terminal.terminal import Packet, _encode_packet

SELF = "10001"
BOB = "20001"
CAROL = "20002"


def _network_with_friends():
    hub = InProcessOneBot()
    hub.friends = {SELF: [
        {"user_id": int(BOB), "nickname": "鲍勃", "remark": ""},
        {"user_id": int(CAROL), "nickname": "卡罗尔", "remark": "小卡"},
    ]}
    net = hub.connect(SELF)
    return hub, net


def _message(user_id, text=None, image=False, segs=None):
    message = list(segs or [])
    if text is not None:
        message.append({"type": "text", "data": {"text": text}})
    if image:
        # 内联 base64 图片走 _decode_base64 分支，不需要网络
        message.append({"type": "image", "data": {
            "file": "base64://" + _PNG_B64, "sub_type": 0}})
    return {"post_type": "message", "message_type": "private",
            "user_id": int(user_id), "message_id": 1, "message": message}


#: 1x1 PNG，够让解码器认出来就行（隐写层会拒绝太小，正好测"普通图也显示"）
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4"
            "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


class LazyCreationTest(unittest.TestCase):
    def setUp(self):
        self.hub, self.net = _network_with_friends()
        self.directory = Directory()
        self.directory.refresh(self.hub.friends[SELF])
        self.conversations = Conversations(self.net, self.directory).attach()

    def test_nothing_exists_before_contact(self):
        """30 个好友不能预建 30 个 Terminal。"""
        self.assertEqual(len(self.conversations), 0)

    def test_ensure_creates_once_and_reuses(self):
        first = self.conversations.ensure(BOB)
        second = self.conversations.ensure(BOB)
        self.assertIs(first, second, "同一好友必须是同一个会话")
        self.assertEqual(len(self.conversations), 1)

    def test_ensure_uses_display_name_from_directory(self):
        self.assertEqual(self.conversations.ensure(CAROL).name, "小卡",
                         "备注优先于昵称")

    def test_inbound_from_stranger_creates_a_conversation(self):
        """陌生人能给你发消息，不能因此丢掉。"""
        self.assertNotIn("99999", self.conversations)
        self.conversations._on_raw("99999", _message("99999", text="你好"))
        self.assertIn("99999", self.conversations)


class IsolationTest(unittest.TestCase):
    """一个 QQ 号挂多个好友时，消息绝不能串台。"""

    def setUp(self):
        self.hub, self.net = _network_with_friends()
        self.directory = Directory()
        self.directory.refresh(self.hub.friends[SELF])
        self.conversations = Conversations(self.net, self.directory).attach()
        self.bob = self.conversations.ensure(BOB)
        self.carol = self.conversations.ensure(CAROL)

    def _route(self, sender, packet):
        """走真实的 per-peer sink 路径投递一个包。"""
        for sink in self.net._sinks_for(sender):
            sink(sender, packet)

    def test_bob_packet_does_not_enter_carol(self):
        """看终端的 inbox：包到底进了谁的处理管道。"""
        self._route(BOB, _encode_packet(Packet(text="鲍勃的密文")))
        self.assertEqual(len(self.bob.terminal.inbox), 1, "鲍勃要收到")
        self.assertEqual(len(self.carol.terminal.inbox), 0,
                         "卡罗尔的终端一个包都不能收到")

    def test_carol_packet_does_not_enter_bob(self):
        self._route(CAROL, _encode_packet(Packet(text="卡罗尔的密文")))
        self.assertEqual(len(self.carol.terminal.inbox), 1)
        self.assertEqual(len(self.bob.terminal.inbox), 0)

    def test_timeline_records_are_separate_too(self):
        """除了终端，时间线也不能串。

        用一条会进时间线的消息（带不支持段，产生一行小字）。单纯的明文
        包不会新增条目 —— 可见内容由 _on_raw 记，隐写层只补 covert/notes，
        这是刻意的分工，见 Conversation.record_inbound。
        """
        self.conversations._on_raw(BOB, _message(
            BOB, segs=[{"type": "record", "data": {}}]))
        self.assertGreater(len(self.bob.entries), 0)
        self.assertEqual(self.carol.entries, [])

    def test_each_conversation_has_its_own_terminal(self):
        """独立密钥：两条会话不能共享一个 Session。"""
        self.assertIsNot(self.bob.terminal, self.carol.terminal)
        self.assertIsNot(self.bob.terminal.session, self.carol.terminal.session)


class UnreadTest(unittest.TestCase):
    def setUp(self):
        self.hub, self.net = _network_with_friends()
        self.directory = Directory()
        self.directory.refresh(self.hub.friends[SELF])
        self.conversations = Conversations(self.net, self.directory).attach()
        self.bob = self.conversations.ensure(BOB)

    def test_unread_counts_while_closed(self):
        self.conversations._on_raw(BOB, _message(BOB, text="在吗"))
        self.conversations._on_raw(BOB, _message(BOB, text="喂"))
        self.assertEqual(self.bob.unread, 2)

    def test_opening_clears_unread(self):
        self.conversations._on_raw(BOB, _message(BOB, text="在吗"))
        self.bob.mark_open()
        self.assertEqual(self.bob.unread, 0)

    def test_no_unread_while_open(self):
        """用户正看着窗口，红点不该亮。"""
        self.bob.mark_open()
        self.conversations._on_raw(BOB, _message(BOB, text="在吗"))
        self.assertEqual(self.bob.unread, 0)

    def test_unread_total_sums_conversations(self):
        carol = self.conversations.ensure(CAROL)
        self.conversations._on_raw(BOB, _message(BOB, text="a"))
        self.conversations._on_raw(CAROL, _message(CAROL, text="b"))
        self.assertEqual(self.conversations.unread_total(), 2)
        self.assertEqual(carol.unread, 1)


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.hub, self.net = _network_with_friends()
        self.directory = Directory()
        self.directory.refresh(self.hub.friends[SELF])
        self.conversations = Conversations(self.net, self.directory).attach()
        self.bob = self.conversations.ensure(BOB)

    def test_visible_text_is_recorded(self):
        self.conversations._on_raw(BOB, _message(BOB, text="今晚八点"))
        self.assertEqual(self.bob.entries[-1].text, "今晚八点")
        self.assertFalse(self.bob.entries[-1].mine)

    def test_unsupported_segment_recorded_as_one_line(self):
        self.conversations._on_raw(BOB, _message(
            BOB, segs=[{"type": "record", "data": {}}]))
        self.assertIn("语音", self.bob.entries[-1].unsupported[0])

    def test_empty_message_adds_no_bubble(self):
        """解析出空内容的条目不能进时间线，否则界面出现空气泡。"""
        before = len(self.bob.entries)
        self.conversations._on_raw(BOB, _message(BOB))
        self.assertEqual(len(self.bob.entries), before)

    def test_outbound_recorded_after_send(self):
        self.conversations.send_text(BOB, "收到")
        last = self.bob.entries[-1]
        self.assertTrue(last.mine)
        self.assertEqual(last.text, "收到")

    def test_ordering_by_activity_not_alphabet(self):
        carol = self.conversations.ensure(CAROL)
        self.conversations._on_raw(BOB, _message(BOB, text="x"))
        self.conversations._on_raw(CAROL, _message(CAROL, text="y"))
        # 谁最后说话谁在最上面
        self.assertEqual(self.conversations.ordered()[0].peer, CAROL)

    def test_unused_conversation_sorts_last(self):
        """点开一个没说过话的好友，不能把他顶到正在聊的人上面。"""
        carol = self.conversations.ensure(CAROL)      # 只有它没有消息
        self.conversations._on_raw(BOB, _message(BOB, text="x"))
        self.assertEqual(self.conversations.ordered()[-1].peer, CAROL)


if __name__ == "__main__":
    unittest.main()
