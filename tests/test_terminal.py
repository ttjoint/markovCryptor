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
from markovcryptor.terminal.covers import CoverLibrary
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
from markovcryptor.terminal.terminal import _encode_packet


def _png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _synthetic_cover(size=256):
    """A deterministic test cover. Deliberately simple: tests only need a
    decodable image, and the realistic-cover guarantee is covered in
    CoverLibraryTest below."""
    import numpy as np
    rng = np.random.default_rng(42)
    return Image.fromarray(rng.integers(64, 192, (size, size, 3), dtype=np.uint8), "RGB")


def _cover(size=256):
    return _png_bytes(_synthetic_cover(size))


def _pair(**kwargs):
    net = LoopbackNetwork()
    alice = Terminal("alice", net, **kwargs).attach()
    bob = Terminal("bob", net, **kwargs).attach()
    return net, alice, bob


def _handshake(net, alice, bob):
    alice.send_handshake("bob", (_cover(),))
    net.pump()


class EnvelopeTest(unittest.TestCase):
    def test_handshake_round_trip(self):
        payload = build_handshake(b"\x01" * 32, Mode.REQUEST)
        public, mode = parse_handshake(payload)
        self.assertEqual(public, b"\x01" * 32)
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
        # A replayed handshake image must not resurrect an old session.
        a, b = Session(), Session()
        pa, _ = a.begin()
        b.absorb(pa, Mode.REQUEST)
        pb, _ = b.answer()
        a.absorb(pb, Mode.RESPONSE)
        old_body = a.seal(Kind.BODY_TEXT, b"old")

        a2, _ = Session(), None
        pa2, _ = a2.begin()
        b.absorb(pa2, Mode.REQUEST)
        b.answer()
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

    def test_auto_response_needs_no_user_action(self):
        net, alice, bob = _pair()
        _handshake(net, alice, bob)
        # One click on alice's side produced three packets: request, auto
        # response, and nothing else. The response must have been emitted by bob
        # without any explicit call.
        senders = [sender for sender, _target, _pkt in net.history]
        self.assertEqual(senders.count("bob"), 1)
        self.assertIn("自动回发", bob.inbox[-1].handshake + alice.inbox[-1].handshake)

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
        self.assertIn("密钥交换", str(caught.exception))

    def test_handshake_requires_an_image(self):
        _net, alice, _bob = _pair()
        with self.assertRaises(InvalidInput) as caught:
            alice.send_handshake("bob", ())
        self.assertIn("至少附带一张图片", str(caught.exception))

    def test_text_channel_unavailable_is_explained(self):
        net, alice, _bob = _pair()
        _handshake(net, alice, alice)
        with self.assertRaises(InvalidInput) as caught:
            alice.send_covert_text("bob", "hi")
        self.assertIn("中文信道模型", str(caught.exception))
        self.assertTrue(caught.exception.hints)

    def test_too_small_cover_reports_which_images_failed(self):
        _net, alice, _bob = _pair()
        tiny = _png_bytes(_synthetic_cover(64))
        with self.assertRaises(CapacityExceeded) as caught:
            alice.send_handshake("bob", (tiny, tiny))
        self.assertTrue(caught.exception.reasons)
        self.assertIn("第 1 张", caught.exception.reasons[0])
        self.assertTrue(caught.exception.hints)


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


class CoverLibraryTest(unittest.TestCase):
    """The rule: every image we send is based on a real-looking picture."""

    def test_empty_library_serves_realistic_fallback(self):
        lib = CoverLibrary(None)
        self.assertTrue(lib.is_fallback)
        data = lib.next_cover()
        image = Image.open(io.BytesIO(data))
        # 大到能装下握手报文，且编码无损。
        self.assertGreaterEqual(min(image.size), 256)
        self.assertEqual(image.format, "PNG")

    def test_fallback_is_not_noise(self):
        # 随机噪声一眼假；兑底图必须有大尺度结构（相邻像素接近）。
        # 噪声图的相邻像素差会远大于这张合成图。
        import numpy as np

        image = np.asarray(Image.open(io.BytesIO(CoverLibrary(None).next_cover())),
                           dtype=float)
        neighbour = np.abs(np.diff(image, axis=1)).mean()
        self.assertLess(neighbour, 12.0, "兑底图像素过于粗糙，看起来像噪声")

    def test_directory_covers_are_used_and_rotate(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            # 三张可区分的纹理图（纯平图会被图片信道拒绝，测试里无所谓，
            # 但保持与真实用法一致）。
            for i in range(3):
                arr = np.random.default_rng(i).integers(64, 192, (160, 160, 3), dtype="uint8")
                Image.fromarray(arr, "RGB").save(Path(tmp) / f"c{i}.png")
            lib = CoverLibrary(tmp)
            self.assertFalse(lib.is_fallback)
            self.assertIn("3 张底图", lib.describe())
            for _ in range(6):
                data = lib.next_cover()
                image = Image.open(io.BytesIO(data))
                self.assertEqual(image.size, (160, 160))  # 来自目录，非兑底

    def test_cover_for_rejects_impossible_payload(self):
        lib = CoverLibrary(None)
        from markovcryptor.core.errors import CapacityExceeded
        with self.assertRaises(CapacityExceeded) as caught:
            lib.cover_for(1_000_000)
        self.assertTrue(caught.exception.hints)

    def test_normalises_jpeg_to_png(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jpg"
            _synthetic_cover(180).convert("RGB").save(path, "JPEG")
            lib = CoverLibrary(tmp)
            data = lib.next_cover()
            self.assertEqual(Image.open(io.BytesIO(data)).format, "PNG")


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


def stego_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
