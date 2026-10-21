"""Account: the wiring between transport, friends, chats and pictures.

These tests use the in-process OneBot hub, so two real ``Terminal`` objects on
two accounts actually exchange a handshake -- no stubs in the crypto path.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from markovcryptor.client.account import Account
from markovcryptor.client.queue import PictureQueue
from markovcryptor.terminal.onebot import InProcessOneBot

ALICE = "10001"
BOB = "20001"


def _cover(folder: Path, name="cover.png", size=(400, 400), color=(90, 120, 150)):
    path = folder / name
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def _queue_for(folder: Path, max_items=4) -> PictureQueue:
    return PictureQueue(folders=[folder], max_items=max_items)


class AccountStartupTest(unittest.TestCase):
    def _hub(self):
        hub = InProcessOneBot()
        hub.friends = {
            ALICE: [{"user_id": int(BOB), "nickname": "鲍勃", "remark": ""}],
            BOB: [{"user_id": int(ALICE), "nickname": "爱丽丝", "remark": ""}],
        }
        return hub

    def test_start_loads_friends_and_uin(self):
        hub = self._hub()
        net = hub.connect(ALICE)
        with tempfile.TemporaryDirectory() as tmp:
            acct = Account(net, queue=_queue_for(Path(tmp))).start()
            self.assertEqual(acct.uin, ALICE)
            self.assertEqual(len(acct.directory), 1)
            self.assertTrue(acct.ready)

    def test_start_is_idempotent_enough_to_call_twice(self):
        hub = self._hub()
        net = hub.connect(ALICE)
        with tempfile.TemporaryDirectory() as tmp:
            acct = Account(net, queue=_queue_for(Path(tmp))).start()
            acct.start()
            self.assertEqual(len(acct.directory), 1)


class HandshakeTest(unittest.TestCase):
    """两个账号真的握手 —— 图片从队列取，回复也从队列取。"""

    def setUp(self):
        self.hub = InProcessOneBot()
        self.hub.friends = {
            ALICE: [{"user_id": int(BOB), "nickname": "鲍勃", "remark": ""}],
            BOB: [{"user_id": int(ALICE), "nickname": "爱丽丝", "remark": ""}],
        }
        self.alice_net = self.hub.connect(ALICE)
        self.bob_net = self.hub.connect(BOB)
        self._tmps = []
        self.alice_dir = self._tmp()
        self.bob_dir = self._tmp()
        _cover(self.alice_dir, "a1.png", color=(10, 90, 200))
        _cover(self.bob_dir, "b1.png", color=(200, 90, 10))
        self.alice = Account(self.alice_net, queue=_queue_for(self.alice_dir)).start()
        self.bob = Account(self.bob_net, queue=_queue_for(self.bob_dir)).start()

    def _tmp(self) -> Path:
        d = tempfile.mkdtemp()
        self._tmps.append(d)
        return Path(d)

    def tearDown(self):
        import shutil
        for d in self._tmps:
            shutil.rmtree(d, ignore_errors=True)

    def test_handshake_establishes_on_both_sides(self):
        """发起方从队列取图，接收方自动回包也从队列取图。"""
        self.alice.start_handshake(BOB)
        self.hub.pump()

        bob_conv = self.bob.conversations.get(ALICE)
        self.assertIsNotNone(bob_conv, "对端要收到握手并建会话")
        self.assertTrue(bob_conv.terminal.established,
                        "对端应自动回包并建立会话")

        alice_conv = self.alice.conversations.get(BOB)
        self.assertTrue(alice_conv.terminal.established, "发起方也应建立")

    def test_handshake_uses_a_real_folder_picture(self):
        """握手要真的从文件夹取图，而不是凭空生成一张。

        不能断言"队列变短"：队列一取出就会从文件夹补满，长度回到上限是
        刻意的。要看的是它用过哪张。
        """
        self.alice.start_handshake(BOB)
        self.hub.pump()
        used = self.alice.queue._used
        self.assertTrue(used, "必须从文件夹里取过图")
        self.assertTrue(all(Path(p).parent == self.alice_dir for p in used),
                        f"取用的图必须来自用户文件夹，实际：{used}")

    def test_handshake_without_pictures_says_so(self):
        """队列空了要明确报错，不能偷偷生成图（风控）。"""
        self.alice.queue.clear()
        self.alice.queue.folders = []
        with self.assertRaises(ValueError) as ctx:
            self.alice.start_handshake(BOB)
        self.assertIn("图片", str(ctx.exception))

    def test_retry_after_queue_refilled(self):
        """队列空时握手回复被压后；补图后能补发。"""
        self.bob.queue.clear()
        self.bob.queue.folders = []
        self.alice.start_handshake(BOB)
        self.hub.pump()
        bob_conv = self.bob.conversations.get(ALICE)
        self.assertIsNotNone(bob_conv)
        self.assertFalse(bob_conv.terminal.established, "没图时不能完成")
        self.assertIsNotNone(bob_conv.terminal.pending_request, "应压后等待")

        # 补一张图再重试
        self.bob.queue.folders = [self.bob_dir]
        self.bob.queue.replenish()
        self.bob.retry_handshakes()
        self.hub.pump()
        self.assertTrue(bob_conv.terminal.established, "补图后应完成")


class CovertTest(unittest.TestCase):
    def setUp(self):
        self.hub = InProcessOneBot()
        self.hub.friends = {ALICE: [], BOB: []}
        self.alice_net = self.hub.connect(ALICE)
        self.bob_net = self.hub.connect(BOB)
        self._tmps = []
        self.alice_dir = self._tmp()
        self.bob_dir = self._tmp()
        _cover(self.alice_dir, "a.png", color=(10, 200, 90))
        _cover(self.bob_dir, "b.png", color=(200, 10, 90))
        self.alice = Account(self.alice_net, queue=_queue_for(self.alice_dir)).start()
        self.bob = Account(self.bob_net, queue=_queue_for(self.bob_dir)).start()
        self.alice.start_handshake(BOB)
        self.hub.pump()

    def _tmp(self) -> Path:
        d = tempfile.mkdtemp()
        self._tmps.append(d)
        return Path(d)

    def tearDown(self):
        import shutil
        for d in self._tmps:
            shutil.rmtree(d, ignore_errors=True)

    def test_covert_text_arrives_on_the_other_side(self):
        secret = "今晚八点，老地方。"
        self.alice.send_covert(BOB, secret)
        self.hub.pump()
        bob_conv = self.bob.conversations.get(ALICE)
        got = [t for e in bob_conv.entries for t in e.covert]
        self.assertIn(secret, got, "密文要能从对端解出来")

    def test_covert_send_without_session_refuses(self):
        """没握手就发密文必须明确拒绝，不能发出去半个包。"""
        carol_net = self.hub.connect("30001")
        acct = Account(carol_net, queue=_queue_for(self.bob_dir)).start()
        with self.assertRaises(Exception):
            acct.send_covert(BOB, "hi")


class PlainTextTest(unittest.TestCase):
    def test_plain_text_roundtrip(self):
        hub = InProcessOneBot()
        hub.friends = {ALICE: [], BOB: []}
        alice_net, bob_net = hub.connect(ALICE), hub.connect(BOB)
        with tempfile.TemporaryDirectory() as tmp:
            alice = Account(alice_net, queue=_queue_for(Path(tmp))).start()
            bob = Account(bob_net, queue=_queue_for(Path(tmp))).start()
            alice.send_text(BOB, "在吗")
            hub.pump()
            conv = bob.conversations.get(ALICE)
            self.assertIsNotNone(conv)
            self.assertTrue(any(e.text == "在吗" for e in conv.entries))


class NoDiskTest(unittest.TestCase):
    """退出即销毁，全程不写用户数据。"""

    def test_close_drops_everything(self):
        hub = InProcessOneBot()
        hub.friends = {ALICE: [{"user_id": int(BOB), "nickname": "b", "remark": ""}]}
        net = hub.connect(ALICE)
        with tempfile.TemporaryDirectory() as tmp:
            acct = Account(net, queue=_queue_for(Path(tmp))).start()
            acct.conversations.ensure(BOB)
            acct.close()
            self.assertEqual(len(acct.conversations), 0)
            self.assertEqual(len(acct.directory), 0)
            self.assertEqual(len(acct.queue), 0)
            self.assertFalse(acct.ready)


if __name__ == "__main__":
    unittest.main()
