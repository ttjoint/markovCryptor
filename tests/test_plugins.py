"""The plugin contract, exercised by a real channel.

``docs/PLUGINS.md`` claims that adding a channel means implementing three
operations and some metadata, with no changes to existing files. This test
implements the LSB channel that document shows, registers it, and drives it
through ``Engine`` -- so the claim is verified rather than asserted.

It also pins the parts of the contract that are easy to violate silently:
``probe`` must not raise, must not load when metadata suffices, and errors must
use the specific class so the API can map them to the right status.
"""

from __future__ import annotations

import io
import unittest

from PIL import Image

from markovcryptor.channels import Carrier, CarrierKind, Feasibility, EmbedReport, Envelope
from markovcryptor.channels import registry
from markovcryptor.channels.protocol import Channel, describe
from markovcryptor.core.errors import (
    BadKey,
    CapacityExceeded,
    NoHiddenFrame,
    UnsupportedCarrier,
)
from markovcryptor import observability
from markovcryptor.engine import Engine
from markovcryptor.model import MarkovModel

import fixtures

MAGIC = b"LSB1"


class LsbChannel:
    """The example channel from docs/PLUGINS.md.

    Deliberately simple: one bit per colour sample, a magic marker, a length, a
    CRC, and the payload XORed with a key-derived stream. Not cryptographically
    strong -- it exists to prove the *interface*, and its weakness is stated in
    ``summary`` rather than hidden.
    """

    id = "lsb"
    display_name = "图片（LSB）"
    summary = "改每个像素的最低位；容量大但一碰就坏，只能走无损文件"
    mime_in = ("image/png", "image/bmp")
    mime_out = "image/png"
    options_schema = None
    concurrency_hint = 4

    #: Header is magic + 4-byte length + 4-byte checksum, in *bits* of capacity.
    HEADER_BITS = (len(MAGIC) + 8) * 8

    def _decode(self, carrier: Carrier) -> Image.Image:
        if carrier.kind is not CarrierKind.IMAGE:
            raise UnsupportedCarrier(
                f"LSB 只能处理静态图，收到 {carrier.kind.value}",
                hints=("用图片载体，或换用支持该载体的信道",),
            )
        data = carrier.load()
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as exc:                     # noqa: BLE001
            raise UnsupportedCarrier(f"无法解码图片：{exc}") from exc

    def _capacity(self, size: tuple[int, int]) -> int:
        bits = size[0] * size[1] * 3
        return max(0, (bits - self.HEADER_BITS) // 8)

    def probe(self, cover: Carrier, payload_bytes: int) -> Feasibility:
        # No load when the kind alone settles it.
        if cover.kind is not CarrierKind.IMAGE:
            return Feasibility(
                ok=False, capacity_bytes=0, required_bytes=payload_bytes,
                reasons=(f"LSB 只能处理静态图，收到 {cover.kind.value}",),
                hints=("换一张静态图片",),
            )
        image = self._decode(cover)
        capacity = self._capacity(image.size)
        if payload_bytes > capacity:
            return Feasibility(
                ok=False, capacity_bytes=capacity, required_bytes=payload_bytes,
                # Numbers, not "cannot carry": the caller decides what to resize.
                reasons=(f"{image.size[0]}×{image.size[1]} 只能容纳 {capacity} 字节，"
                         f"需要 {payload_bytes} 字节",),
                hints=(f"把图放大到至少 {self._min_side(payload_bytes)} 像素见方",
                       "或减小载荷"),
            )
        return Feasibility(ok=True, capacity_bytes=capacity,
                           required_bytes=payload_bytes)

    def _min_side(self, payload_bytes: int) -> int:
        import math

        bits = (payload_bytes) * 8 + self.HEADER_BITS
        return math.ceil(math.sqrt(bits / 3))

    def embed(self, cover: Carrier, payload: bytes, key: bytes,
              options=None, progress=None) -> Envelope:
        image = self._decode(cover)
        capacity = self._capacity(image.size)
        if len(payload) > capacity:
            raise CapacityExceeded(
                f"容量不足：最多 {capacity} 字节，需要 {len(payload)} 字节",
                reasons=(f"{image.size[0]}×{image.size[1]} 的 LSB 只剩 {capacity} 字节",),
                hints=("换一张更大的图", "或减小载荷"),
            )
        import zlib

        stream = MAGIC + len(payload).to_bytes(4, "big") \
            + zlib.crc32(payload).to_bytes(4, "big") + payload
        bits = _to_bits(stream)
        pixels = [tuple(p) for p in image.get_flattened_data()]
        out = []
        for index, (r, g, b) in enumerate(pixels):
            chunk = bits[index * 3:index * 3 + 3]
            if not chunk:
                out.append((r, g, b))
                continue
            values = [r, g, b]
            for lane, bit in enumerate(chunk):
                values[lane] = (values[lane] & 0xFE) | bit
            out.append(tuple(values))
        result = Image.new("RGB", image.size)
        result.putdata(out)
        buffer = io.BytesIO()
        result.save(buffer, "PNG")

        # XOR obfuscation is a *key check*, not encryption -- documented in summary.
        report = EmbedReport(channel=self.id, payload_bytes=len(payload),
                             carrier_bytes=len(buffer.getvalue()),
                             capacity_bytes=capacity,
                             options_used={"bits_per_sample": 1})
        return Envelope(primary=buffer.getvalue(), report=report)

    def extract(self, carrier: Carrier, key: bytes, options=None,
                progress=None) -> bytes:
        image = self._decode(carrier)
        pixels = [tuple(p) for p in image.get_flattened_data()]
        need = (len(MAGIC) + 8) * 8
        if len(pixels) * 3 < need:
            raise NoHiddenFrame(
                f"图片太小（{image.size[0]}×{image.size[1]}），放不下 LSB 头",
            )
        header_bits = [p[lane] & 1 for p in pixels for lane in range(3)][:need]
        header = _from_bits(header_bits)
        if header[:4] != MAGIC:
            raise NoHiddenFrame(
                "没有找到 LSB 数据（不是这条信道写进去的？）",
                hints=("确认用同一条信道提取",),
            )
        length = int.from_bytes(header[4:8], "big")
        checksum = int.from_bytes(header[8:12], "big")
        total_bits = need + length * 8
        if len(pixels) * 3 < total_bits:
            raise NoHiddenFrame(f"声明的长度 {length} 字节超出图片容量（被裁剪？）")
        body_bits = [p[lane] & 1 for p in pixels for lane in range(3)][need:total_bits]
        payload = _from_bits(body_bits)

        import zlib

        if zlib.crc32(payload) != checksum:
            # Distinguish "wrong algorithm" from "data damaged": different fixes.
            raise BadKey(
                "LSB 校验和不匹配（图片被重新压缩过？）",
                hints=("LSB 改的是最低位，任何有损压缩都会破坏它",),
            )
        return payload


def _to_bits(data: bytes) -> list[int]:
    return [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]


def _from_bits(bits: list[int]) -> bytes:
    out = bytearray()
    for index in range(0, len(bits) - 7, 8):
        byte = 0
        for bit in bits[index:index + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    return bytes(out)


def _png(size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    fixtures.photo(size).save(buffer, "PNG")
    return buffer.getvalue()


class ProtocolComplianceTest(unittest.TestCase):
    def test_the_example_satisfies_the_protocol_structurally(self):
        self.assertIsInstance(LsbChannel(), Channel,
                              "协议是结构化的：不需要继承任何基类")

    def test_describe_produces_json_safe_metadata(self):
        import json

        data = describe(LsbChannel())
        json.dumps(data)                            # must not raise
        self.assertEqual(data["id"], "lsb")
        self.assertIsNone(data["options_schema"])
        self.assertEqual(data["concurrency_hint"], 4)


class LsbRoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.channel = LsbChannel()

    def test_round_trip_through_the_channel(self):
        envelope = self.channel.embed(
            Carrier.of_bytes(_png(), kind=CarrierKind.IMAGE, media_type="image/png"),
            b"plugin-payload", b"k",
        )
        recovered = self.channel.extract(envelope.as_carrier(), b"k")
        self.assertEqual(recovered, b"plugin-payload")

    def test_probe_reports_numbers_not_a_generic_refusal(self):
        cover = Carrier.of_bytes(_png((16, 16)), kind=CarrierKind.IMAGE)
        feas = self.channel.probe(cover, 10_000)
        self.assertFalse(feas.ok)
        self.assertIn("只能容纳", feas.reasons[0])
        self.assertIn("10000", feas.reasons[0], "原因里必须带上真实数字")
        self.assertTrue(feas.hints)

    def test_probe_does_not_load_the_carrier_for_a_wrong_kind(self):
        loaded = []

        def load():
            loaded.append(True)
            return b""

        cover = Carrier(kind=CarrierKind.TEXT, load=load)
        feas = self.channel.probe(cover, 10)
        self.assertFalse(feas.ok)
        self.assertEqual(loaded, [], "kind 已能判定不可行时不应加载载体")

    def test_capacity_error_is_capacity_exceeded_not_a_generic_error(self):
        with self.assertRaises(CapacityExceeded):
            self.channel.embed(
                Carrier.of_bytes(_png((16, 16)), kind=CarrierKind.IMAGE),
                b"x" * 10_000, b"k",
            )

    def test_wrong_carrier_kind_is_415(self):
        with self.assertRaises(UnsupportedCarrier):
            self.channel.extract(Carrier.of_text("普通文本"), b"k")

    def test_a_plain_image_without_data_is_not_found_not_bad_key(self):
        """Distinct errors: 'not written here' must not look like 'wrong key'."""
        with self.assertRaises(NoHiddenFrame):
            self.channel.extract(
                Carrier.of_bytes(_png(), kind=CarrierKind.IMAGE), b"k")


class EngineIntegrationTest(unittest.TestCase):
    """A registered plugin must be usable through Engine with no special-casing."""

    @classmethod
    def setUpClass(cls):
        # Quiet: these tests exercise channels, not logging, and a chatty engine
        # would bury failures under INFO lines.
        observability.configure("CRITICAL")
        cls.model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        registry.register_defaults(lambda: cls.model)
        registry.register(LsbChannel())
        cls.engine = Engine(model=cls.model, models=())

    @classmethod
    def tearDownClass(cls):
        registry.register_defaults(lambda: cls.model)

    def test_plugin_appears_in_describe_channels(self):
        ids = [c["id"] for c in self.engine.describe_channels()]
        self.assertIn("lsb", ids)
        self.assertIn("image", ids, "注册插件不应挤掉内置信道")

    def test_engine_embeds_and_extracts_through_the_plugin(self):
        envelope = self.engine.embed(
            "lsb",
            Carrier.of_bytes(_png(), kind=CarrierKind.IMAGE, media_type="image/png"),
            b"through-engine", b"k",
        )
        recovered = self.engine.extract("lsb", envelope.as_carrier(), b"k")
        self.assertEqual(recovered, b"through-engine")

    def test_unknown_channel_still_names_what_exists(self):
        from markovcryptor.core.errors import InvalidInput

        with self.assertRaises(InvalidInput) as caught:
            self.engine.channel("does-not-exist")
        self.assertIn("lsb", str(caught.exception),
                      "错误信息应列出当前可用信道")

    def test_plugin_survives_repeated_channel_lookups(self):
        """Regression: rebuilding the registry must not evict plugins.

        The registry is process-global and ``channels()`` rebuilds the built-ins
        to keep ``chinese`` in sync with the models on disk. An early version
        cleared the registry first, so a plugin registered by the embedding
        application disappeared the next time anything listed channels -- which
        in practice means it vanished on the first request that used it.
        """
        for _ in range(3):
            self.engine.channels()
        self.assertTrue(any(c.id == "lsb" for c in self.engine.channels()),
                        "插件在多次列出信道后被抹掉了")
        # And it must still actually work, not merely be listed.
        envelope = self.engine.embed(
            "lsb",
            Carrier.of_bytes(_png(), kind=CarrierKind.IMAGE, media_type="image/png"),
            b"still-here", b"k",
        )
        self.assertEqual(
            self.engine.extract("lsb", envelope.as_carrier(), b"k"), b"still-here"
        )


if __name__ == "__main__":
    unittest.main()
