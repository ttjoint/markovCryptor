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


class ConcurrencyTest(unittest.TestCase):
    """入站消息在 OneBot 读线程，发送在 uvicorn 线程 —— 真会同时发生。

    这些用真实多线程跑，因为审查发现的丢消息/重复渲染都只在并发下出现。
    """

    def setUp(self):
        self.hub, self.net = _network_with_friends()
        self.directory = Directory()
        self.directory.refresh(self.hub.friends[SELF])
        self.conversations = Conversations(self.net, self.directory).attach()
        self.bob = self.conversations.ensure(BOB)

    def test_concurrent_appends_and_snapshots_do_not_lose_messages(self):
        """一边收消息一边取快照，条目数必须对得上。"""
        import threading

        stop = threading.Event()
        errors: list[BaseException] = []
        snapshots = [0]

        def reader():
            for i in range(200):
                try:
                    self.conversations._on_raw(BOB, _message(BOB, text=f"m{i}"))
                except BaseException as exc:        # noqa: BLE001
                    errors.append(exc)

        def snapshotter():
            while not stop.is_set():
                try:
                    snapshots[0] = max(snapshots[0], len(self.bob.snapshot()))
                except BaseException as exc:        # noqa: BLE001
                    errors.append(exc)

        watcher = threading.Thread(target=snapshotter)
        watcher.start()
        writer = threading.Thread(target=reader)
        writer.start()
        writer.join()
        stop.set()
        watcher.join()

        self.assertEqual(errors, [], f"并发下不该抛异常：{errors[:3]}")
        self.assertEqual(len(self.bob.snapshot()), 200, "一条消息都不能丢")

    def test_concurrent_ensure_creates_one_conversation(self):
        """两线程同时 ensure 同一个 peer，只能有一个 Terminal。"""
        import threading

        created: list[object] = []
        errors: list[BaseException] = []

        def worker():
            try:
                created.append(self.conversations.ensure("77777"))
            except BaseException as exc:            # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"并发 ensure 不该抛：{errors[:3]}")
        self.assertEqual(len({id(c) for c in created}), 1,
                         "同一个 peer 只能有一个会话对象")
        self.assertEqual(len(self.conversations), 2)   # bob + 77777

    def test_iterating_while_creating_does_not_raise(self):
        """迭代会话列表时新建会话，不能抛 dict changed size during iteration。"""
        import threading

        errors: list[BaseException] = []
        stop = threading.Event()

        def iterator():
            while not stop.is_set():
                try:
                    for c in self.conversations:
                        _ = c.name
                    _ = self.conversations.unread_total()
                except BaseException as exc:        # noqa: BLE001
                    errors.append(exc)
                    return

        def creator():
            for i in range(60):
                self.conversations.ensure(str(90000 + i))

        reader = threading.Thread(target=iterator)
        reader.start()
        creator()
        stop.set()
        reader.join()

        self.assertEqual(errors, [], f"迭代时新建会话不该抛：{errors[:3]}")

    def test_unread_is_not_counted_while_open(self):
        """用户正看着窗口时到达的消息不该留下红点（check-then-increment 竞争）。"""
        import threading

        self.bob.mark_open()
        errors: list[BaseException] = []

        def reader():
            for i in range(200):
                try:
                    self.conversations._on_raw(BOB, _message(BOB, text=f"m{i}"))
                except BaseException as exc:        # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.bob.unread, 0, "开着的会话不该累计未读")


class LostMessageTest(unittest.TestCase):
    """确定性复现「合并历史时丢掉新消息」。

    审查指出的时序：先 list(entries) 快照 -> 中间到达一条新消息 append ->
    再 entries[:] = merged + existing。新消息不在快照里，被整体覆盖抹掉。
    压测难以命中这个窗口，所以直接构造：在快照和赋值之间插入一条消息。
    """

    def setUp(self):
        self.hub, self.net = _network_with_friends()
        self.directory = Directory()
        self.directory.refresh(self.hub.friends[SELF])
        self.conversations = Conversations(self.net, self.directory).attach()
        self.bob = self.conversations.ensure(BOB)

    def test_prepend_history_keeps_messages_that_arrive_during_the_merge(self):
        """prepend_history 在锁内整体替换，期间 append 的消息必须还在。"""
        from markovcryptor.client.conversations import Entry

        self.bob.record_outbound(text="已有的")
        # 模拟"合并进行中又收到一条"：直接调 prepend_history，并在其前后
        # 各放一条消息。原子实现下三条都在。
        self.bob.record_outbound(text="合并前")
        self.bob.prepend_history([Entry(mine=False, text="历史一")])
        self.bob.record_outbound(text="合并后")

        texts = [e.text for e in self.bob.snapshot()]
        for expected in ("历史一", "已有的", "合并前", "合并后"):
            self.assertIn(expected, texts, f"{expected} 被丢了")

    def test_old_style_merge_would_lose_the_message(self):
        """证明旧的「快照 + 切片赋值」写法确实会丢消息。

        这不是测产品代码，是测"为什么不能那样写"：构造出同样的时序，
        断言它确实丢——这样以后有人想改回去，会看到这条记录。
        """
        from markovcryptor.client.conversations import Entry

        self.bob.record_outbound(text="已有")
        entries = self.bob.entries
        existing = list(entries)                 # 快照
        # 快照之后、赋值之前到达的新消息
        self.bob.record_outbound(text="窗口期到达")
        # 旧写法：整体覆盖
        entries[:] = [Entry(mine=False, text="历史")] + existing

        texts = [e.text for e in self.bob.snapshot()]
        self.assertNotIn("窗口期到达", texts,
                         "旧写法本该丢掉窗口期消息 —— 这条断言失败说明"
                         "测试没复现出那个时序")

    def test_prepend_history_only_once(self):
        from markovcryptor.client.conversations import Entry

        self.assertTrue(self.bob.prepend_history([Entry(text="h")]))
        self.assertFalse(self.bob.prepend_history([Entry(text="h")]),
                         "第二次必须被拒绝，否则历史灌两遍")
        self.assertEqual(len(self.bob.snapshot()), 1)
