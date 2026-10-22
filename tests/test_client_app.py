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
