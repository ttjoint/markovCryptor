"""Tests for the GIF variant of the MKX1 channel."""

from __future__ import annotations

import unittest
from io import BytesIO

import numpy as np
from PIL import Image

from markovcryptor import gif_stego as G
from markovcryptor import image_stego as S

import fixtures as F


def _sample_gif() -> Image.Image:
    """The versioned fixture animation (3 frames, 120x120).

    Previously this read ``data/gif/*.gif``, which is not versioned — so on a
    clean checkout every GIF test quietly skipped and the channel was untested.
    """
    return F.animation_image()


class GifStegoTest(unittest.TestCase):
    key = S.normalise_key("gif-test-key")
    payload = bytes(range(32))

    def frame(self, payload: bytes | None = None) -> bytes:
        return S.build_frame(payload if payload is not None else self.payload)

    def test_embed_and_extract(self):
        source = _sample_gif()
        data, report = G.embed_gif(source, self.frame(), self.key)
        self.assertIn(report["step"], G.GIF_STEPS)
        got, info = G.extract_gif(data, self.key)
        self.assertEqual(got, self.payload)
        self.assertEqual(info["gif_frames"], report["frames"])

    def test_output_is_a_valid_gif(self):
        source = _sample_gif()
        data, report = G.embed_gif(source, self.frame(), self.key)
        reloaded = Image.open(BytesIO(data))
        self.assertEqual(reloaded.format, "GIF")
        self.assertEqual(reloaded.n_frames, report["frames"])

    def test_wrong_key_rejected(self):
        source = _sample_gif()
        data, _ = G.embed_gif(source, self.frame(), self.key)
        with self.assertRaises(S.StegoError):
            G.extract_gif(data, S.normalise_key("different-key"))

    def test_clean_gif_is_rejected(self):
        source = _sample_gif()
        frames, durations = G.frames_of(source)
        clean = G.encode_gif([G._to_palette(f) for f in frames], durations)
        with self.assertRaises(G.GifStegoError):
            G.extract_gif(clean, self.key)

    def test_palette_quantisation_is_lossless_for_gif_frames(self):
        # The whole scheme depends on this: a frame that a GIF can already
        # represent must survive a round trip through the palette unchanged.
        for label, source in (("fixture", _sample_gif()),):
            frames, _durations = G.frames_of(source)
            for frame in frames[:2]:
                palette = G._to_palette(frame)
                again = palette.convert("RGB")
                original = frame.quantize(
                    256, method=Image.MEDIANCUT, dither=Image.NONE
                ).convert("RGB")
                self.assertEqual(
                    list(again.get_flattened_data()),
                    list(original.get_flattened_data()),
                    label,
                )

    def test_every_sample_survives(self):
        data, _report = G.embed_gif(_sample_gif(), self.frame(), self.key)
        got, _info = G.extract_gif(data, self.key)
        self.assertEqual(got, self.payload)

    def test_size_overhead_is_small(self):
        # Embedding touches one frame only, so the file may not balloon.  We
        # compare against a plain re-encode, since that isolates the cost of
        # steganography from the cost of PIL's own GIF writer.
        source = _sample_gif()
        frames, durations = G.frames_of(source)
        control = G.encode_gif([G._to_palette(f) for f in frames], durations)
        data, _ = G.embed_gif(source, self.frame(), self.key)
        growth = (len(data) - len(control)) / len(control)
        self.assertLess(growth, 0.10, f"{growth:.1%} 增长过大")

    def test_multiple_frames(self):
        source = _sample_gif()
        if getattr(source, "n_frames", 1) < 2:
            self.skipTest("样本只有一帧")
        data, report = G.embed_gif(source, self.frame(), self.key,
                                   frames_to_use=(0, 1))
        self.assertEqual(report["embedded_frames"], [0, 1])
        for index in (0, 1):
            got, _ = G.extract_gif(data, self.key, frame_index=index)
            self.assertEqual(got, self.payload)

    def test_capacity_report(self):
        info = G.capacity(_sample_gif())
        self.assertGreater(info["max_payload_bytes"], 32)
        self.assertGreaterEqual(info["frames"], 1)

    def test_oversized_payload_is_rejected(self):
        # The largest legal payload is 65535 bytes; a 40x40 GIF would have to be
        # scaled ~180x to hold that, beyond MAX_UPSCALE, so it must refuse.
        tiny = Image.new("RGB", (40, 40), (20, 90, 160))
        raw = G.encode_gif([G._to_palette(tiny)], [40])
        source = Image.open(BytesIO(raw))
        too_big = bytes(65_535)
        with self.assertRaises(S.StegoError):
            G.embed_gif(source, self.frame(too_big), self.key)


class GifGeometryTest(unittest.TestCase):
    """Small GIFs must be usable, either by upscaling or by a coarser step."""

    key = S.normalise_key("gif-geometry")

    def _animated(self, size, count, base=(40, 90, 140)):
        frames = [
            Image.fromarray(
                np.random.default_rng(11 + i).integers(
                    0, 256, (size[1], size[0], 3), dtype=np.uint8), "RGB")
            for i in range(count)
        ]
        raw = G.encode_gif([G._to_palette(f) for f in frames], [50] * count)
        return Image.open(BytesIO(raw))

    def test_small_gif_is_upscaled(self):
        # 90x90 carries no handshake on its own; a modest enlargement does.
        source = self._animated((90, 90), 3)
        payload = bytes(range(32))
        data, report = G.embed_gif(source, S.build_frame(payload), self.key)
        self.assertTrue(report["resized"], "expected an enlargement")
        self.assertEqual(G.extract_gif(data, self.key)[0], payload)

    def test_tiny_gif_is_refused_not_blown_up(self):
        # 40x40 would need 3x, i.e. 9x the pixels on *every* frame, which turns
        # a few KB into a few hundred KB to carry 32 bytes.
        source = self._animated((40, 40), 3)
        with self.assertRaises(G.GifStegoError) as ctx:
            G.embed_gif(source, S.build_frame(bytes(range(32))), self.key)
        self.assertIn("太小", str(ctx.exception))

    def test_low_color_gif_works(self):
        frames = [Image.new("RGB", (256, 256), (30, 60, 90)) for _ in range(2)]
        raw = G.encode_gif([G._to_palette(f) for f in frames], [40, 40])
        source = Image.open(BytesIO(raw))
        payload = bytes(range(32))
        data, _ = G.embed_gif(source, S.build_frame(payload), self.key)
        self.assertEqual(G.extract_gif(data, self.key)[0], payload)


if __name__ == "__main__":
    unittest.main()
