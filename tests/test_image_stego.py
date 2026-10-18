"""Tests for MKX1 image steganography + X25519 handshake."""

from __future__ import annotations

import io

import numpy as np
import random
import unittest

from PIL import Image

from markovcryptor import image_stego as S
from markovcryptor.handshake import HandshakeParty, HandshakeError, seal, open_

import fixtures as F


def _cover() -> Image.Image:
    return F.photo()


class ImageStegoTest(unittest.TestCase):
    def setUp(self):
        self.key = S.normalise_key("测试密钥")
        self.payload = bytes(range(32))

    def test_frame_roundtrip(self):
        frame = S.build_frame(self.payload)
        self.assertEqual(S.parse_frame(frame), self.payload)

    def test_embed_extract_direct(self):
        stego, rep = S.embed(_cover(), S.build_frame(self.payload), self.key)
        out, info = S.try_extract(stego, self.key)
        self.assertEqual(out, self.payload)
        self.assertGreater(info["mean_confidence"], 0.5)

    def test_png_roundtrip(self):
        stego, _ = S.embed(_cover(), S.build_frame(self.payload), self.key)
        buf = io.BytesIO()
        stego.save(buf, "PNG")
        buf.seek(0)
        out, _ = S.try_extract(Image.open(buf), self.key)
        self.assertEqual(out, self.payload)

    def test_jpeg_q88(self):
        # JPEG survival depends on cover size: a bigger image keeps the QIM
        # coefficients well away from the quantisation grid.
        stego, _ = S.embed(_cover().resize((640, 640)), S.build_frame(self.payload), self.key)
        buf = io.BytesIO()
        stego.save(buf, "JPEG", quality=88)
        buf.seek(0)
        out, _ = S.try_extract(Image.open(buf), self.key)
        self.assertEqual(out, self.payload)

    def test_wrong_key_rejected(self):
        stego, _ = S.embed(_cover(), S.build_frame(self.payload), self.key)
        with self.assertRaises(S.StegoError):
            S.try_extract(stego, S.normalise_key("错误密钥"))

    def test_clean_cover_rejected(self):
        with self.assertRaises(S.StegoError):
            S.try_extract(_cover(), self.key)

    def test_capacity_plan(self):
        info = S.plan(512, 339)
        self.assertGreaterEqual(info["blocks_available"], info["blocks_needed"])
        self.assertTrue(info["usable"])
        self.assertGreater(info["max_payload_bytes"], 100)


class CoverConstraintTest(unittest.TestCase):
    """Documented limits on what a cover image may be."""

    key = S.normalise_key("k")

    def _roundtrip(self, image: Image.Image) -> bool:
        stego, _ = S.embed(image, S.build_frame(bytes(range(32))), self.key)
        # ``embed`` may raise the QIM step for awkward covers, so the reader
        # has to probe the known step choices instead of assuming the default.
        out, _ = S.try_extract(stego, self.key)
        return out == bytes(range(32))

    def test_too_small_rejected(self):
        for size in ((16, 16), (19, 512), (512, 19)):
            ok, reason = S.check_cover(Image.new("RGB", size))
            self.assertFalse(ok, size)
            self.assertTrue(reason)

    def test_small_covers_are_upscaled(self):
        # Anything below the working size is enlarged instead of rejected.
        for size in ((24, 24), (32, 32), (48, 64), (64, 40)):
            image = Image.fromarray(
                np.random.default_rng(4).integers(0, 256, (size[1], size[0], 3), dtype=np.uint8), "RGB")
            scaled, original, resized = S.upscale_cover(image)
            self.assertTrue(resized, size)
            self.assertEqual(original, size)
            self.assertGreaterEqual(min(scaled.size), S.SMALLEST_SIDE // 2)
            stego, _ = S.embed(scaled, S.build_frame(bytes(range(32))), self.key)
            out, _ = S.extract(stego, self.key)
            self.assertEqual(out, bytes(range(32)), size)

    def test_payload_capacity_grows_with_size(self):
        previous = 0
        for side in (128, 256, 512, 1024):
            current = S.max_payload_bytes(side, side)
            self.assertGreater(current, previous, side)
            previous = current

    def test_large_payload_roundtrip(self):
        cover = _cover().resize((768, 768))
        for size in (32, 600, 3000):
            payload = bytes((i * 37 + 11) % 256 for i in range(size))
            stego, report = S.embed(cover, S.build_frame(payload), self.key)
            out, _ = S.extract(stego, self.key)
            self.assertEqual(out, payload, size)
            self.assertGreaterEqual(report["rs_blocks"], 2)

    def test_flat_and_extreme_covers(self):
        # Saturated and flat covers force ``embed`` onto a coarser QIM step and
        # sometimes onto the Cb/Cr channels.  Pure green sits exactly on the RGB
        # gamut edge, so luma clipping can still eat the lattice; a clear error
        # is an acceptable outcome, silent corruption is not.
        always_ok = ((0, 0, 0), (255, 255, 255), (128, 128, 128),
                     (255, 0, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255))
        for colour in always_ok:
            self.assertTrue(self._roundtrip(Image.new("RGB", (512, 512), colour)), colour)

    def test_pure_green_never_corrupts(self):
        cover = Image.new("RGB", (512, 512), (0, 255, 0))
        payload = bytes(range(32))
        try:
            stego, _ = S.embed(cover, S.build_frame(payload), self.key)
        except S.StegoError:
            return                      # documented limit: refuses loudly
        out, _info = S.try_extract(stego, self.key)
        self.assertEqual(out, payload)  # if it claims success, it must be right

    def test_non_multiple_of_eight(self):
        for size in ((491, 512), (410, 512), (474, 512), (352, 512)):
            image = F.photo(size)
            self.assertTrue(self._roundtrip(image), size)

    def test_geometric_edits_break_the_frame(self):
        stego, _ = S.embed(_cover(), S.build_frame(bytes(range(32))), self.key)
        with self.assertRaises(S.StegoError):
            S.extract(stego.rotate(90, expand=True), self.key)
        with self.assertRaises(S.StegoError):
            S.extract(stego.resize((stego.width // 2, stego.height // 2)), self.key)


class HandshakeTest(unittest.TestCase):
    def test_full_exchange(self):
        random.seed(3)
        covers = [F.varied_photo(i) for i in (0, 1, 2)]
        alice = HandshakeParty("Alice", "口令")
        bob = HandshakeParty("Bob", "口令")
        bob.receive_stego_image(alice.make_stego_image(random.choice(covers))[0])
        alice.receive_stego_image(bob.make_stego_image(random.choice(covers))[0])
        sa, sb = alice.derive_session(), bob.derive_session()
        self.assertEqual(sa.shared, sb.shared)
        self.assertEqual(sa.send_key, sb.recv_key)
        self.assertEqual(sa.recv_key, sb.send_key)
        self.assertEqual(sa.transcript, sb.transcript)

    def test_wrong_psk_fails(self):
        cover = _cover()
        alice = HandshakeParty("Alice", "口令A")
        bob = HandshakeParty("Bob", "口令B")
        with self.assertRaises(S.StegoError):
            bob.receive_stego_image(alice.make_stego_image(cover)[0])

    def test_missing_peer(self):
        party = HandshakeParty("A", "p")
        with self.assertRaises(HandshakeError):
            party.derive_session()

    def test_aes_roundtrip(self):
        party = HandshakeParty("A", "p")
        nonce, ct = seal(party.psk[:32], "秘密".encode())
        self.assertEqual(open_(party.psk[:32], nonce, ct).decode(), "秘密")


class LeadProfileTest(unittest.TestCase):
    """The lead header adapts to the cover so small images carry more payload."""

    key = S.normalise_key("lead-profile")

    def _cover(self, side):
        return Image.fromarray(
            np.random.default_rng(side).integers(0, 256, (side, side, 3), dtype=np.uint8),
            "RGB")

    def test_compact_lead_is_cheaper(self):
        self.assertLess(S.LEAD_COMPACT.blocks, S.LEAD_STANDARD.blocks)
        self.assertLess(S.blocks_needed(32, S.LEAD_COMPACT),
                        S.blocks_needed(32, S.LEAD_STANDARD))

    def test_codewords_stay_lane_aligned(self):
        # Each 8x8 block carries LANES bits, so a lead codeword that is not a
        # whole number of lanes would desynchronise every following block.
        for profile in S.LEAD_PROFILES.values():
            self.assertEqual(profile.code_bytes * 8 % S.LANES, 0, profile)

    def test_small_cover_picks_compact(self):
        self.assertIs(S.lead_profile_for(S.blocks_total(128, 128), 32), S.LEAD_COMPACT)

    def test_large_cover_picks_standard(self):
        self.assertIs(S.lead_profile_for(S.blocks_total(2048, 2048), 32), S.LEAD_STANDARD)

    def test_both_profiles_round_trip(self):
        payload = bytes(range(32))
        seen = set()
        for side in (128, 2048):
            stego, report = S.embed(self._cover(side), S.build_frame(payload), self.key)
            seen.add(report["lead_profile"])
            got, _ = S.extract(stego, self.key, step=report["step"])
            self.assertEqual(got, payload, side)
        self.assertEqual(seen, {p.index for p in S.LEAD_PROFILES.values()},
                         "expected each profile to be exercised")

    def test_explicit_profile_is_honoured(self):
        payload = bytes(range(32))
        for profile in S.LEAD_PROFILES.values():
            frame = S.build_frame(payload, profile=profile)
            self.assertEqual(S.parse_frame(frame), payload)
            self.assertEqual(S.parse_frame(frame, profile=profile), payload)

    def test_unrecognised_header_is_rejected(self):
        with self.assertRaises(S.StegoError):
            S.parse_frame(b"XXXX" + bytes(40))


if __name__ == "__main__":
    unittest.main(verbosity=2)
