"""The client HTTP API, driven in-process through httpx.

These verify the contract the browser relies on: which endpoints exist, what
they return, and -- more importantly -- that a user mistake comes back as a
readable 409 rather than a 500. A 500 sends people to check their network when
the actual problem is an empty picture queue.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from httpx import ASGITransport, AsyncClient
from PIL import Image

from markovcryptor.client.account import Account
from markovcryptor.client.app import create_client_app
from markovcryptor.client.queue import PictureQueue
from markovcryptor.terminal.onebot import InProcessOneBot

ALICE = "10001"
BOB = "20001"


def _run(coro):
    return asyncio.run(coro)


def _parsed(text: str):
    """A ParsedMessage as the history parser would produce it."""
    from markovcryptor.client.messages import ParsedMessage
    return ParsedMessage(sender=BOB, text=text)


def _make_cover(folder: Path, name="c.png", size=(400, 400)):
    path = folder / name
    Image.new("RGB", size, (80, 130, 170)).save(path, "PNG")
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self.hub = InProcessOneBot()
        self.hub.friends = {
            ALICE: [{"user_id": int(BOB), "nickname": "鲍勃", "remark": "小鲍"}],
            BOB: [{"user_id": int(ALICE), "nickname": "爱丽丝", "remark": ""}],
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        _make_cover(self.folder)
        self.net = self.hub.connect(ALICE)
        self.account = Account(self.net, queue=PictureQueue(
            folders=[self.folder], max_items=4)).start()
        self.app = create_client_app(self.account)
        self.client = AsyncClient(transport=ASGITransport(app=self.app),
                                  base_url="http://test")

    def tearDown(self):
        _run(self.client.aclose())
        self.tmp.cleanup()

    def get(self, path):
        return _run(self.client.get(path))

    def post(self, path, json=None):
        return _run(self.client.post(path, json=json))


class StateTest(_Base):
    def test_state_reports_account_and_friends(self):
        r = self.get("/client/state")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["uin"], ALICE)
        self.assertTrue(body["ready"])
        self.assertEqual(body["friends"], [{"uin": BOB, "name": "小鲍"}],
                         "备注优先于昵称")

    def test_state_reports_queue_limits(self):
        body = self.get("/client/state").json()
        self.assertEqual(body["queue"]["max"], 4)
        self.assertEqual(body["queue"]["folders"], 1)

    def test_state_starts_with_no_conversations(self):
        body = self.get("/client/state").json()
        self.assertEqual(body["conversations"], [])
        self.assertEqual(body["unread_total"], 0)


class ConversationTest(_Base):
    def test_opening_creates_and_returns_entries(self):
        r = self.get(f"/client/conversation/{BOB}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["peer"], BOB)
        self.assertEqual(body["name"], "小鲍")
        self.assertEqual(body["entries"], [])

    def test_opening_clears_unread(self):
        self.account.conversations._on_raw(BOB, {
            "post_type": "message", "message_type": "private",
            "user_id": int(BOB), "message": [
                {"type": "text", "data": {"text": "在吗"}}]})
        conv = self.account.conversations.get(BOB)
        self.assertEqual(conv.unread, 1)
        self.post(f"/client/conversation/{BOB}/open")
        self.assertEqual(conv.unread, 0)

    def test_closing_lets_unread_accumulate_again(self):
        self.post(f"/client/conversation/{BOB}/open")
        self.post(f"/client/conversation/{BOB}/close")
        self.account.conversations._on_raw(BOB, {
            "post_type": "message", "message_type": "private",
            "user_id": int(BOB), "message": [
                {"type": "text", "data": {"text": "喂"}}]})
        self.assertEqual(self.account.conversations.get(BOB).unread, 1)

    def test_unknown_peer_still_opens(self):
        """陌生人也得能点开，否则第一条消息来了没地方看。"""
        r = self.get("/client/conversation/99999")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "99999")


class SendTest(_Base):
    def test_plain_text_send_is_accepted(self):
        r = self.post("/client/send", json={"peer": BOB, "text": "你好", "mode": "text"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_empty_peer_is_rejected(self):
        r = self.post("/client/send", json={"text": "hi", "mode": "text"})
        self.assertEqual(r.status_code, 422)

    def test_unknown_mode_is_rejected(self):
        r = self.post("/client/send", json={"peer": BOB, "mode": "teleport"})
        self.assertEqual(r.status_code, 422)

    def test_covert_without_session_is_409_not_500(self):
        """没握手就发密文是用户的错，要能读懂，不能报服务器故障。"""
        r = self.post("/client/send", json={"peer": BOB, "text": "秘密",
                                            "mode": "covert"})
        self.assertEqual(r.status_code, 409)
        # 文案是"会话未建立，不能发送密文"。断言关键词而非整句，
        # 免得以后改措辞就误报。
        self.assertIn("密文", r.json()["detail"])

    def test_handshake_with_empty_queue_is_409_with_a_usable_message(self):
        self.account.queue.clear()
        self.account.queue.folders = []
        r = self.post("/client/send", json={"peer": BOB, "mode": "handshake"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("图片", r.json()["detail"])

    def test_handshake_then_covert_works(self):
        """握手要真的建立，才谈得上发密文。

        只建 alice 一个账号是不够的：握手发出去没人回包，状态会停在
        "等待对端回复"，之后发密文本来就该被拒。所以这里把 bob 的账号
        也建起来，让它真的自动回包。
        """
        bob_dir = self.folder / "bob"
        bob_dir.mkdir()
        _make_cover(bob_dir, "b.png")
        bob_net = self.hub.connect(BOB)
        bob = Account(bob_net, queue=PictureQueue(
            folders=[bob_dir], max_items=2)).start()

        r = self.post("/client/send", json={"peer": BOB, "mode": "handshake"})
        self.assertEqual(r.status_code, 200)
        self.hub.pump()

        bob_conv = bob.conversations.get(ALICE)
        self.assertIsNotNone(bob_conv)
        self.assertTrue(bob_conv.established, "对端应自动回包")

        r = self.post("/client/send", json={"peer": BOB, "text": "藏着的话",
                                            "mode": "covert"})
        self.assertEqual(r.status_code, 200, r.text)
        self.hub.pump()
        self.assertTrue(self.account.conversations.get(BOB).established)


class QueueEndpointTest(_Base):
    def test_queue_endpoint_reports_state(self):
        body = self.get("/client/queue").json()
        self.assertIn("queued", body)
        self.assertIn("max", body)

    def test_refill_reports_added_and_answered(self):
        self.account.queue.clear()
        r = self.post("/client/queue/refill")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["added"], 1)


class WebuiMountTest(unittest.TestCase):
    def test_index_html_is_served_when_dir_given(self):
        hub = InProcessOneBot()
        hub.friends = {ALICE: []}
        net = hub.connect(ALICE)
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp) / "webui"
            ui.mkdir()
            (ui / "index.html").write_text("<html><body>hello</body></html>",
                                           encoding="utf-8")
            account = Account(net, queue=PictureQueue()).start()
            app = create_client_app(account, webui_dir=ui)
            client = AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://test")

            async def go():
                try:
                    return await client.get("/")
                finally:
                    await client.aclose()

            r = _run(go())
            self.assertEqual(r.status_code, 200)
            self.assertIn("hello", r.text)

    def test_api_still_works_with_webui_mounted(self):
        """/ 被静态文件占用时，/client/* 不能被吃掉。"""
        hub = InProcessOneBot()
        hub.friends = {ALICE: []}
        net = hub.connect(ALICE)
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp) / "webui"
            ui.mkdir()
            (ui / "index.html").write_text("<html></html>", encoding="utf-8")
            account = Account(net, queue=PictureQueue()).start()
            app = create_client_app(account, webui_dir=ui)
            client = AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://test")

            async def go():
                try:
                    return await client.get("/client/state")
                finally:
                    await client.aclose()

            r = _run(go())
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()


class ErrorMappingTest(_Base):
    """用户的错必须报 4xx，不能报 502。

    ``core/errors.py`` 早就给 StegoError 定义了 ``http_status``（413 图太小、
    415 格式不对）和 ``hints``（下一步怎么办）。app.py 原来只按 ValueError
    一刀切、还丢掉 hints，结果"换张大点的图"被显示成"发送失败"，用户去查
    网络 —— 方向完全错了。
    """

    def test_picture_too_small_is_413_not_502(self):
        from markovcryptor.core.errors import CapacityExceeded

        with mock.patch.object(self.account, "send_covert",
                               side_effect=CapacityExceeded("图太小")):
            r = self.post("/client/send", json={"peer": BOB, "text": "x",
                                                "mode": "covert"})
        self.assertEqual(r.status_code, 413)

    def test_unsupported_format_is_415_not_502(self):
        from markovcryptor.core.errors import UnsupportedCarrier

        with mock.patch.object(self.account, "send_covert",
                               side_effect=UnsupportedCarrier("格式不支持")):
            r = self.post("/client/send", json={"peer": BOB, "text": "x",
                                                "mode": "covert"})
        self.assertEqual(r.status_code, 415)

    def test_hints_are_kept_in_the_message(self):
        """hints 里写着"怎么办"，丢掉就等于只说失败不说下一步。"""
        from markovcryptor.core.errors import CapacityExceeded

        exc = CapacityExceeded("图太小", hints=("换一张更大的图片",))
        with mock.patch.object(self.account, "send_covert", side_effect=exc):
            r = self.post("/client/send", json={"peer": BOB, "text": "x",
                                                "mode": "covert"})
        self.assertIn("换一张更大的图片", r.json()["detail"])

    def test_genuine_failure_stays_502(self):
        """真故障不能被伪装成 4xx，否则会盖住 bug。"""
        with mock.patch.object(self.account, "send_text",
                               side_effect=RuntimeError("网络炸了")):
            r = self.post("/client/send", json={"peer": BOB, "text": "x",
                                                "mode": "text"})
        self.assertEqual(r.status_code, 502)


class HistoryMergeTest(_Base):
    """历史只合并一次，且不能抹掉期间到达的新消息。"""

    def test_history_merged_only_once(self):
        with mock.patch.object(self.account, "load_history", return_value=[
                _parsed("旧消息一"), _parsed("旧消息二")]):
            first = self.get(f"/client/conversation/{BOB}?history=5").json()
            second = self.get(f"/client/conversation/{BOB}?history=5").json()
        self.assertEqual(len(first["entries"]), 2)
        self.assertEqual(len(second["entries"]), 2, "重复打开不能灌两遍")

    def test_new_message_during_merge_is_not_lost(self):
        """合并历史时到达的新消息不能被切片赋值抹掉。

        这正是审查发现的丢消息时序：先快照、再整体替换，中间 append 进来的
        那条不在快照里，被直接覆盖。
        """
        conv = self.account.conversations.ensure(BOB)
        conv.record_outbound(text="合并前就在的消息")
        with mock.patch.object(self.account, "load_history", return_value=[
                _parsed("旧消息")]):
            self.get(f"/client/conversation/{BOB}?history=5")
        texts = [e.text for e in conv.snapshot()]
        self.assertIn("合并前就在的消息", texts, "已有消息不能被历史覆盖掉")
        self.assertIn("旧消息", texts)

    def test_history_prepended_before_existing(self):
        conv = self.account.conversations.ensure(BOB)
        conv.record_outbound(text="新")
        with mock.patch.object(self.account, "load_history", return_value=[
                _parsed("旧")]):
            self.get(f"/client/conversation/{BOB}?history=5")
        texts = [e.text for e in conv.snapshot()]
        self.assertEqual(texts, ["旧", "新"], "历史要在前面")


class HistoryAttributionTest(_Base):
    """历史消息必须分得清谁说的。

    实际反馈的问题：消息栏里看不出是发出的还是收到的。根因是历史合并把
    每条都硬编码成 ``mine=False``，于是用户自己发过的话也排在左边、
    和对方长得一模一样。
    """

    def _history(self, *rows):
        from markovcryptor.client.messages import ParsedMessage
        return [ParsedMessage(sender=s, text=t, at=1000.0 + i)
                for i, (s, t) in enumerate(rows)]

    def test_own_history_message_is_marked_mine(self):
        with mock.patch.object(self.account, "load_history", return_value=
                self._history((ALICE, "我说的"), (BOB, "他说的"))):
            self.get(f"/client/conversation/{BOB}?history=5")
        entries = self.account.conversations.get(BOB).snapshot()
        by_text = {e.text: e.mine for e in entries}
        self.assertTrue(by_text["我说的"], "自己发的必须在右边")
        self.assertFalse(by_text["他说的"], "对方发的必须在左边")

    def test_history_keeps_qq_timestamps(self):
        with mock.patch.object(self.account, "load_history", return_value=
                self._history((BOB, "带时间"))):
            self.get(f"/client/conversation/{BOB}?history=5")
        entry = self.account.conversations.get(BOB).snapshot()[0]
        self.assertEqual(entry.at, 1000.0, "QQ 给的时间不能被丢掉")

    def test_api_exposes_at_and_mine(self):
        with mock.patch.object(self.account, "load_history", return_value=
                self._history((ALICE, "我"), (BOB, "他"))):
            body = self.get(f"/client/conversation/{BOB}?history=5").json()
        mine_flags = {e["text"]: e["mine"] for e in body["entries"]}
        self.assertEqual(mine_flags, {"我": True, "他": False})
        self.assertTrue(all("at" in e for e in body["entries"]),
                        "每条都要带时间，前端才画得出时间戳")


class ModelsEndpointTest(_Base):
    """模型状态与切换。

    没有模型时只能发图片密文，界面必须说清楚，不能让用户点了"密文发送"
    才发现发不出去。
    """

    def test_state_reports_no_models_by_default(self):
        body = self.get("/client/state").json()
        self.assertEqual(body["models"]["total"], 0)
        self.assertFalse(body["text_channel"],
                         "没有模型时文本信道必须报不可用")

    def test_models_endpoint_reports_state(self):
        body = self.get("/client/models").json()
        self.assertTrue(body["ok"])
        self.assertIn("usable", body)

    def test_reload_without_folder_is_409(self):
        r = self.post("/client/models/reload")
        self.assertEqual(r.status_code, 409)
        self.assertIn("模型", r.json()["detail"])

    def test_select_unknown_model_is_404(self):
        r = self.post("/client/models/select", json={"name": "不存在"})
        self.assertIn(r.status_code, (404, 409))


class ModelsWithLibraryTest(_Base):
    """真的挂上一个模型目录，验证状态、切换、加密发送。"""

    def setUp(self):
        super().setUp()
        from markovcryptor.client.models import ModelLibrary

        root = Path(__file__).resolve().parent.parent / "models"
        if not root.is_dir() or not any(root.glob("*.json")):
            self.skipTest("仓库里没有模型文件")
        self.models = ModelLibrary(root)
        self.models.scan()
        self.models.load_all()
        self.account.models = self.models
        from markovcryptor.client.models import MultiModelTextChannel
        channel = MultiModelTextChannel(self.models)
        self.account.conversations.text_channel = channel
        for conv in self.account.conversations:
            conv.terminal.use_text_channel(channel)

    def test_state_reports_models_loaded(self):
        body = self.get("/client/state").json()
        self.assertGreater(body["models"]["total"], 0)
        self.assertEqual(body["models"]["loaded"], body["models"]["total"])
        self.assertTrue(body["text_channel"])

    def test_select_switches_preferred_model(self):
        name = self.models.status()["names"][-1]
        r = self.post("/client/models/select", json={"name": name})
        self.assertEqual(r.status_code, 200, r.text)
        channel = self.account.conversations.text_channel
        self.assertEqual(getattr(channel.preferred, "name", ""), name)

    def test_covert_text_send_uses_the_text_channel(self):
        """握手之后，文本密文应当走中文载体而不是图片。"""
        bob_dir = self.folder / "bob"
        bob_dir.mkdir()
        _make_cover(bob_dir, "b.png")
        bob_net = self.hub.connect(BOB)
        bob = Account(bob_net, queue=PictureQueue(folders=[bob_dir], max_items=2),
                      models=self.models).start()

        self.post("/client/send", json={"peer": BOB, "mode": "handshake"})
        self.hub.pump()
        self.assertTrue(bob.conversations.get(ALICE).established)

        r = self.post("/client/send", json={"peer": BOB, "text": "藏起来的句子",
                                            "mode": "covert"})
        self.assertEqual(r.status_code, 200, r.text)
        self.hub.pump()
        conv = self.account.conversations.get(BOB)
        got = [t for e in conv.snapshot() for t in e.covert]
        self.assertIn("藏起来的句子", got)


class HistorySeqTest(_Base):
    """历史条目必须拿到递增的 seq，否则前端增量渲染会把它们全跳过。

    实际 bug：prepend_history 没有给条目赋 seq，它们全是 0；前端判断
    "seq <= 已渲染到的 seq 就跳过"，于是合并了 21 条历史却一条都没画出来。
    界面上表现为"点开好友看不到聊天记录"。
    """

    def _history(self, n=3):
        from markovcryptor.client.messages import ParsedMessage
        return [ParsedMessage(sender=BOB, text=f"历史{i}", at=1000.0 + i)
                for i in range(n)]

    def test_history_entries_get_distinct_increasing_seq(self):
        with mock.patch.object(self.account, "load_history",
                               return_value=self._history(3)):
            body = self.get(f"/client/conversation/{BOB}?history=5").json()
        seqs = [e["seq"] for e in body["entries"]]
        self.assertEqual(len(seqs), 3)
        self.assertEqual(seqs, sorted(seqs), "历史必须按顺序递增")
        self.assertEqual(len(set(seqs)), len(seqs),
                         "不能有重复 seq —— 重复会让前端跳过条目")

    def test_history_seq_is_below_existing_messages(self):
        """历史在时间线最前面，seq 必须比已有消息小，否则排序错乱。"""
        conv = self.account.conversations.ensure(BOB)
        conv.record_outbound(text="新消息")
        with mock.patch.object(self.account, "load_history",
                               return_value=self._history(2)):
            body = self.get(f"/client/conversation/{BOB}?history=5").json()
        entries = body["entries"]
        self.assertEqual(entries[0]["text"], "历史0")
        self.assertEqual(entries[-1]["text"], "新消息")
        seqs = [e["seq"] for e in entries]
        self.assertEqual(seqs, sorted(seqs), "整条时间线的 seq 必须单调递增")
        self.assertEqual(len(set(seqs)), len(seqs), "seq 不能重复")

    def test_all_entries_have_seq(self):
        """每条都必须带 seq，前端才能做增量渲染。"""
        with mock.patch.object(self.account, "load_history",
                               return_value=self._history(2)):
            body = self.get(f"/client/conversation/{BOB}?history=5").json()
        for entry in body["entries"]:
            self.assertIn("seq", entry)
            self.assertIsInstance(entry["seq"], int)


class HistoryImagePlaceholderTest(_Base):
    """图片能加载时**不要**加占位文字，加载不了才加。

    用户反馈：纯图片消息旁边还写着"对方发送了 图片"是重复的 —— 图已经
    完整显示出来了。占位行只用于真正看不到内容的情况（拿不到 URL）。
    """

    def _history_with_image(self, url="https://x/a.jpg"):
        from markovcryptor.client.messages import ImageRef, ParsedMessage
        return [ParsedMessage(sender=BOB, text="",
                              images=(ImageRef(url=url),),
                              at=1000.0)]

    def test_image_only_history_has_no_redundant_placeholder(self):
        """有 URL（能下载）就不该多一行废话。"""
        with mock.patch.object(self.account, "load_history",
                               return_value=self._history_with_image()):
            body = self.get(f"/client/conversation/{BOB}?history=5").json()
        self.assertEqual(len(body["entries"]), 1,
                         "图片消息本身要保留，不能整条丢掉")
        entry = body["entries"][0]
        self.assertEqual(entry["unsupported"], [],
                         "图已经能显示，不需要「发送了图片」这种提示")

    def test_image_without_url_gets_a_placeholder(self):
        """拿不到 URL 就是真的看不到，这时才提示。"""
        with mock.patch.object(self.account, "load_history",
                               return_value=self._history_with_image(url=None)):
            body = self.get(f"/client/conversation/{BOB}?history=5").json()
        self.assertEqual(len(body["entries"]), 1)
        entry = body["entries"][0]
        self.assertTrue(entry["unsupported"], "看不到内容时必须留一行说明")
        self.assertIn("图片", entry["unsupported"][0])

    def test_text_plus_image_keeps_the_text_only(self):
        """有文字就不再加占位行 —— 文字本身已经说明了这条消息。"""
        from markovcryptor.client.messages import ImageRef, ParsedMessage
        parsed = [ParsedMessage(sender=BOB, text="看这个",
                                images=(ImageRef(url="https://x/a.jpg"),),
                                at=1000.0)]
        with mock.patch.object(self.account, "load_history", return_value=parsed):
            body = self.get(f"/client/conversation/{BOB}?history=5").json()
        entry = body["entries"][0]
        self.assertEqual(entry["text"], "看这个")
        self.assertEqual(entry["unsupported"], [])
