"""身份绑定与 TOFU 指纹。

这两件事解决的是同一个洞：X25519 握手**没有身份认证**。能改写消息流的人
可以分别和两端握手，全程解密再转发，双方都以为在做端到端加密
（`handshake.py` 的 docstring 自己写着 "textbook unauthenticated DH"）。

两层防护，各管一段：

* **帧内 MAC** —— 不知道共享口令的人伪造不了握手帧
* **TOFU 指纹** —— 口令相同的人冒充也能被发现，因为公钥变了
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from markovcryptor.core import identity as I
from markovcryptor.core.keys import normalise_key

PSK = normalise_key("shared-passphrase")
ALICE_PUB = bytes(range(32))
BOB_PUB = b"\x07" * 32
MALLORY_PUB = b"\x42" * 32


class MacTest(unittest.TestCase):
    def test_round_trip(self):
        frame = I.build_handshake_payload(ALICE_PUB, ALICE_PUB, PSK)
        self.assertEqual(len(frame), I.HANDSHAKE_PAYLOAD_BYTES)
        self.assertEqual(I.verify_handshake_payload(frame, ALICE_PUB, PSK),
                         ALICE_PUB)

    def test_wrong_passphrase_is_rejected(self):
        """口令不同就解不开 —— 这是 MAC 存在的直接目的。"""
        frame = I.build_handshake_payload(ALICE_PUB, ALICE_PUB, PSK)
        with self.assertRaises(ValueError) as caught:
            I.verify_handshake_payload(frame, ALICE_PUB,
                                       normalise_key("someone-else"))
        self.assertIn("校验失败", str(caught.exception))

    def test_tampered_public_key_is_rejected(self):
        """改了公钥必须失效，否则 MAC 形同虚设。"""
        frame = bytearray(I.build_handshake_payload(ALICE_PUB, ALICE_PUB, PSK))
        frame[0] ^= 0x01
        with self.assertRaises(ValueError):
            I.verify_handshake_payload(bytes(frame), ALICE_PUB, PSK)

    def test_tampered_mac_is_rejected(self):
        frame = bytearray(I.build_handshake_payload(ALICE_PUB, ALICE_PUB, PSK))
        frame[-1] ^= 0x01
        with self.assertRaises(ValueError):
            I.verify_handshake_payload(bytes(frame), ALICE_PUB, PSK)

    def test_frame_bound_to_its_sender(self):
        """把 Alice 的帧原样冒充成 Bob 的会失败 —— MAC 绑了发送方角色。"""
        frame = I.build_handshake_payload(ALICE_PUB, ALICE_PUB, PSK)
        with self.assertRaises(ValueError):
            I.verify_handshake_payload(frame, BOB_PUB, PSK)

    def test_malformed_length_is_reported(self):
        with self.assertRaises(ValueError) as caught:
            I.verify_handshake_payload(ALICE_PUB, ALICE_PUB, PSK)
        self.assertIn("长度异常", str(caught.exception))


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_short(self):
        first = I.fingerprint(ALICE_PUB)
        self.assertEqual(first, I.fingerprint(ALICE_PUB), "同一公钥指纹必须稳定")
        self.assertEqual(len(first), 16, "16 个十六进制字符够人眼比对")
        self.assertNotEqual(first, I.fingerprint(BOB_PUB))

    def test_different_keys_never_collide_in_practice(self):
        seen = {I.fingerprint(bytes([i]) * 32) for i in range(256)}
        self.assertEqual(len(seen), 256)


class KnownPeersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "peers.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_contact_remembered(self):
        peers = I.KnownPeers(self.path)
        status, fp = peers.check("1000", ALICE_PUB)
        self.assertEqual(status, "first")
        self.assertEqual(fp, I.fingerprint(ALICE_PUB))

    def test_second_contact_matches(self):
        peers = I.KnownPeers(self.path)
        peers.check("1000", ALICE_PUB)
        status, fp = peers.check("1000", ALICE_PUB)
        self.assertEqual(status, "same")
        self.assertEqual(fp, I.fingerprint(ALICE_PUB))

    def test_changed_key_is_flagged(self):
        """**这是 TOFU 的全部意义**：公钥变了必须报出来。

        可能是对方重装了，也可能是有人插在中间 —— 两种都需要用户自己判断。
        """
        peers = I.KnownPeers(self.path)
        peers.check("1000", ALICE_PUB)
        status, fp = peers.check("1000", MALLORY_PUB)
        self.assertEqual(status, "changed")
        self.assertEqual(fp, I.fingerprint(MALLORY_PUB))
        self.assertNotEqual(fp, I.fingerprint(ALICE_PUB))

    def test_survives_restart(self):
        """内存里存等于每次重启都重新信任 —— 中间人只需等你重启。"""
        first = I.KnownPeers(self.path)
        first.check("1000", ALICE_PUB)

        second = I.KnownPeers(self.path)
        self.assertEqual(second.known("1000"), I.fingerprint(ALICE_PUB))
        status, _ = second.check("1000", MALLORY_PUB)
        self.assertEqual(status, "changed", "重启后仍然要认得旧指纹")

    def test_trust_adopts_the_new_key(self):
        """用户确认"是本人换设备了"，就更新记忆。"""
        peers = I.KnownPeers(self.path)
        peers.check("1000", ALICE_PUB)
        peers.check("1000", MALLORY_PUB)
        peers.trust("1000", MALLORY_PUB)

        reloaded = I.KnownPeers(self.path)
        status, _ = reloaded.check("1000", MALLORY_PUB)
        self.assertEqual(status, "same")

    def test_forget_clears_the_memory(self):
        peers = I.KnownPeers(self.path)
        peers.check("1000", ALICE_PUB)
        self.assertTrue(peers.forget("1000"))
        self.assertIsNone(peers.known("1000"))
        self.assertFalse(peers.forget("1000"))

    def test_corrupt_file_does_not_block_startup(self):
        """指纹册坏了要能启动，但必须留下警告 —— 静默失效比崩溃更糟。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        peers = I.KnownPeers(self.path)
        self.assertIsNone(peers.known("1000"))
        # 还能继续正常工作
        status, _ = peers.check("1000", ALICE_PUB)
        self.assertEqual(status, "first")

    def test_missing_file_is_normal(self):
        peers = I.KnownPeers(self.path)
        self.assertIsNone(peers.known("1000"))

    def test_no_path_means_memory_only(self):
        """测试和嵌入式用法不该被迫写盘。"""
        peers = I.KnownPeers(None)
        peers.check("1000", ALICE_PUB)
        self.assertEqual(peers.known("1000"), I.fingerprint(ALICE_PUB))

    def test_written_file_holds_only_fingerprints(self):
        """存的是公开指纹，不该出现任何密钥或消息内容。"""
        peers = I.KnownPeers(self.path)
        peers.check("1000", ALICE_PUB)
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("1000", raw)
        self.assertIn(I.fingerprint(ALICE_PUB), raw)
        self.assertNotIn(ALICE_PUB.hex(), raw, "不该存原始公钥")
        self.assertNotIn("shared-passphrase", raw, "更不该存口令")


if __name__ == "__main__":
    unittest.main()
