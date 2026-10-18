"""Tests for the GIF variant of the MKX1 channel."""

from __future__ import annotations

import glob
import os
import unittest
from io import BytesIO

from PIL import Image

from markovcryptor import gif_stego as G
from markovcryptor import image_stego as S


def _first_frame(path: str) -> Image.Image:
    image = Image.open(path)
    image.seek(0)
    return image.convert("RGB")


def _sample_gif() -> str:
    files = sorted(glob.glob(os.path.join("data", "gif", "*.gif")))
    if not files:
        raise unittest.SkipTest("data/gif 中没有 GIF 样本")
    return files[0]


class GifStegoTest(unittest.TestCase):
    key = S.normalise_key("gif-test-key")
    payload = bytes(range(32))

    def frame(self, payload: bytes | None = None) -> bytes:
        return S.build_frame(payload if payload is not None else self.payload)

    def test_embed_and_extract(self):
        source = Image.open(_sample_gif())
        data, report = G.embed_gif(source, self.frame(), self.key)
        self.assertIn(report["step"], G.GIF_STEPS)
        got, info = G.extract_gif(data, self.key)
        self.assertEqual(got, self.payload)
        self.assertEqual(info["gif_frames"], report["frames"])

    def test_output_is_a_valid_gif(self):
        source = Image.open(_sample_gif())
        data, report = G.embed_gif(source, self.frame(), self.key)
        reloaded = Image.open(BytesIO(data))
        self.assertEqual(reloaded.format, "GIF")
        self.assertEqual(reloaded.n_frames, report["frames"])

    def test_wrong_key_rejected(self):
        source = Image.open(_sample_gif())
        data, _ = G.embed_gif(source, self.frame(), self.key)
        with self.assertRaises(S.StegoError):
            G.extract_gif(data, S.normalise_key("different-key"))

    def test_clean_gif_is_rejected(self):
        source = Image.open(_sample_gif())
        frames, durations = G.frames_of(source)
        clean = G.encode_gif([G._to_palette(f) for f in frames], durations)
        with self.assertRaises(G.GifStegoError):
            G.extract_gif(clean, self.key)

    def test_palette_quantisation_is_lossless_for_gif_frames(self):
        # The whole scheme depends on this: a frame that a GIF can already
        # represent must survive a round trip through the palette unchanged.
        for path in sorted(glob.glob(os.path.join("data", "gif", "*.gif")))[:4]:
            source = Image.open(path)
            frames, _durations = G.frames_of(source)
            for frame in frames[:2]:
                palette = G._to_palette(frame)
                again = palette.convert("RGB")
                original = frame.quantize(
                    256, method=Image.MEDIANCUT, dither=Image.NONE
                ).convert("RGB")
                self.assertEqual(list(again.getdata()), list(original.getdata()), path)

    def test_every_sample_survives(self):
        files = sorted(glob.glob(os.path.join("data", "gif", "*.gif")))
        if not files:
            raise unittest.SkipTest("data/gif 中没有 GIF 样本")
        for path in files:
            with self.subTest(path=os.path.basename(path)):
                data, _report = G.embed_gif(Image.open(path), self.frame(), self.key)
                got, _info = G.extract_gif(data, self.key)
                self.assertEqual(got, self.payload)

    def test_size_overhead_is_small(self):
        # Embedding touches one frame only, so the file may not balloon.  We
        # compare against a plain re-encode, since that isolates the cost of
        # steganography from the cost of PIL's own GIF writer.
        files = sorted(glob.glob(os.path.join("data", "gif", "*.gif")))
        if not files:
            raise unittest.SkipTest("data/gif 中没有 GIF 样本")
        for path in files[:6]:
            with self.subTest(path=os.path.basename(path)):
                source = Image.open(path)
                frames, durations = G.frames_of(Image.open(path))
                control = G.encode_gif(
                    [G._to_palette(f) for f in frames], durations
                )
                data, _ = G.embed_gif(source, self.frame(), self.key)
                growth = (len(data) - len(control)) / len(control)
                self.assertLess(growth, 0.10, f"{growth:.1%} 增长过大")

    def test_multiple_frames(self):
        path = _sample_gif()
        source = Image.open(path)
        if getattr(source, "n_frames", 1) < 2:
            self.skipTest("样本只有一帧")
        data, report = G.embed_gif(source, self.frame(), self.key,
                                   frames_to_use=(0, 1))
        self.assertEqual(report["embedded_frames"], [0, 1])
        for index in (0, 1):
            got, _ = G.extract_gif(data, self.key, frame_index=index)
            self.assertEqual(got, self.payload)

    def test_capacity_report(self):
        info = G.capacity(Image.open(_sample_gif()))
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

    def test_tiny_gif_is_upscaled(self):
        frames, durations = [], []
        for i in range(3):
            image = Image.new("RGB", (40, 40), (40 + i * 30, 90, 140 - i * 20))
            frames.append(image)
            durations.append(50)
        raw = G.encode_gif([G._to_palette(f) for f in frames], durations)
        source = Image.open(BytesIO(raw))
        payload = bytes(range(32))
        data, report = G.embed_gif(source, S.build_frame(payload), self.key)
        got, _ = G.extract_gif(data, self.key)
        self.assertEqual(got, payload)

    def test_low_color_gif_works(self):
        frames = [Image.new("RGB", (256, 256), (30, 60, 90)) for _ in range(2)]
        raw = G.encode_gif([G._to_palette(f) for f in frames], [40, 40])
        source = Image.open(BytesIO(raw))
        payload = bytes(range(32))
        data, _ = G.embed_gif(source, S.build_frame(payload), self.key)
        self.assertEqual(G.extract_gif(data, self.key)[0], payload)


if __name__ == "__main__":
    unittest.main()
