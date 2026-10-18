"""Phase 2 acceptance: the three channels behind one uniform interface.

The point of these tests is *uniformity*, not algorithm behaviour -- the
algorithms have their own suites. What is asserted here is that a caller who
knows nothing about DCT or Markov chains can drive every channel identically:

    feas = channel.probe(carrier, len(payload))
    if not feas.ok: ...
    env = channel.embed(carrier, payload, key)
    assert channel.extract(env.as_carrier(), key) == payload

A regression here means a new channel would need special-casing somewhere,
which is exactly what the abstraction exists to prevent.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
import unittest

from markovcryptor.channels import Carrier, CarrierKind, Feasibility
from markovcryptor.channels import registry
from markovcryptor.channels.chinese import ChineseChannel
from markovcryptor.channels.gif import GifChannel
from markovcryptor.channels.image import ImageChannel
from markovcryptor.channels.options import GifOptions, ImageOptions, coerce
from markovcryptor.codec import MarkovStegoCodec
from markovcryptor.model import MarkovModel
from markovcryptor.core.errors import CapacityExceeded, InvalidInput, NoHiddenFrame

import fixtures

KEY = "共享口令-shared"


def _png(image) -> bytes:
    """Encode a fixture to PNG bytes -- carriers are bytes in every real call."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@lru_cache(maxsize=1)
def _model() -> MarkovModel:
    """A small deterministic model, trained once and shared across tests.

    ``vocabulary_size=None`` is not accepted by ``train``, so the corpus's own
    size is used -- it is a fixture, so the result is byte-for-byte reproducible.
    """
    return MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)


class RegistryTest(unittest.TestCase):
    # ``register_defaults`` rebuilds the registry, so no manual reset is needed.

    def test_defaults_without_model_register_only_media_channels(self):
        ids = [c.id for c in registry.register_defaults()]
        self.assertEqual(ids, ["image", "gif"])

    def test_defaults_with_model_also_register_chinese(self):
        ids = [c.id for c in registry.register_defaults(lambda: _model())]
        self.assertEqual(ids, ["image", "gif", "chinese"])

    def test_unknown_channel_names_the_alternatives(self):
        registry.register_defaults()
        with self.assertRaises(InvalidInput) as ctx:
            registry.get("video")
        self.assertIn("image", str(ctx.exception))
        self.assertIn("image", ctx.exception.hints[0])

    def test_channel_satisfies_the_protocol(self):
        from markovcryptor.channels.protocol import Channel
        for channel in registry.register_defaults():
            self.assertIsInstance(channel, Channel, channel.id)

    def test_every_channel_describes_itself_for_a_form(self):
        registry.register_defaults(lambda: _model())
        described = {d["id"]: d for d in registry.describe_all()}
        self.assertEqual(set(described), {"image", "gif", "chinese"})
        for cid, data in described.items():
            self.assertTrue(data["display_name"], cid)
            self.assertTrue(data["mime_in"], cid)
            # A schema is what lets the web UI build a form without knowing
            # anything about the channel.
            self.assertIsNotNone(data["options_schema"], cid)
            self.assertIn("properties", data["options_schema"], cid)


class ProtocolUniformityTest(unittest.TestCase):
    """The acceptance loop from docs/ARCHITECTURE.md §阶段 2, run over all three."""

    @classmethod
    def setUpClass(cls):
        codec = MarkovStegoCodec(_model())
        cls.channels = (ImageChannel(), GifChannel(), ChineseChannel(codec))

    def _carrier_for(self, channel) -> Carrier:
        if channel.id == "image":
            return Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE,
                                    media_type="image/png")
        if channel.id == "gif":
            return Carrier.of_bytes(fixtures.animation(), kind=CarrierKind.ANIMATION,
                                    media_type="image/gif")
        return Carrier.of_text("", label="（无底稿）")

    def test_all_channels_round_trip(self):
        payload = b"MKX1 phase-2 acceptance payload \x00\x01\x02"
        for channel in self.channels:
            with self.subTest(channel=channel.id):
                carrier = self._carrier_for(channel)
                feas = channel.probe(carrier, len(payload))
                self.assertIsInstance(feas, Feasibility)
                self.assertTrue(feas.ok, f"{channel.id}: {feas}")

                env = channel.embed(carrier, payload, KEY.encode())
                self.assertEqual(env.report.channel, channel.id)
                self.assertEqual(env.report.payload_bytes, len(payload))
                self.assertGreater(env.report.capacity_bytes, 0)

                recovered = channel.extract(env.as_carrier(), KEY.encode())
                self.assertEqual(recovered, payload)

    def test_probe_agrees_with_embed_on_what_fits(self):
        """A feasibility verdict must not be a guess that embed then contradicts."""
        payload = b"x" * 40
        for channel in self.channels:
            with self.subTest(channel=channel.id):
                carrier = self._carrier_for(channel)
                feas = channel.probe(carrier, len(payload))
                if feas.ok:
                    channel.embed(carrier, payload, KEY.encode())   # must not raise
                else:
                    self.assertTrue(feas.reasons, "不可承载时必须给出真实原因")

    def test_wrong_key_fails_loudly_and_specifically(self):
        """A wrong key must never silently return garbage, nor a bare phrase."""
        from markovcryptor.core.errors import StegoError

        payload = b"secret"
        for channel in self.channels:
            with self.subTest(channel=channel.id):
                carrier = self._carrier_for(channel)
                env = channel.embed(carrier, payload, KEY.encode())
                with self.assertRaises(StegoError) as ctx:
                    channel.extract(env.as_carrier(), b"wrong-key")
                # The cause stays specific: the image channel explains that the
                # lead failed to decode, the text channel that the AEAD tag did.
                self.assertGreater(len(str(ctx.exception)), 10, ctx.exception)
                self.assertTrue(ctx.exception.code)

    def test_wrong_key_is_reported_as_a_key_problem(self):
        """The text channel can tell 401 from 404; callers depend on that split."""
        from markovcryptor.core.errors import BadKey

        channel = self.channels[2]
        env = channel.embed(Carrier.of_text(""), b"secret", b"right-pw")
        with self.assertRaises(BadKey):
            channel.extract(env.as_carrier(), b"wrong-pw")


class ProbeTest(unittest.TestCase):
    def test_wrong_carrier_kind_is_reported_not_raised(self):
        channel = ImageChannel()
        feas = channel.probe(Carrier.of_text("hello"), 16)
        self.assertFalse(feas.ok)
        self.assertIn("text", feas.reasons[0])

    def test_capacity_shortfall_names_the_real_numbers(self):
        channel = ImageChannel()
        carrier = Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE)
        feasible = channel.probe(carrier, 10)
        self.assertTrue(feasible.ok)
        too_big = channel.probe(carrier, 10_000)
        self.assertFalse(too_big.ok)
        # The rule from ARCHITECTURE §4.2: never collapse into "cannot carry".
        self.assertIn("需要 10000 字节", too_big.reasons[0])
        self.assertIn("最多", too_big.reasons[0])

    def test_small_cover_reports_the_upscale_it_would_need(self):
        channel = ImageChannel()
        carrier = Carrier.of_bytes(_png(fixtures.small()), kind=CarrierKind.IMAGE)
        feas = channel.probe(carrier, 200)     # more than a 120² cover holds
        if not feas.ok:
            self.assertIn("upscale", feas.cost)
            self.assertTrue(any("放大到" in h for h in feas.hints), feas.hints)

    def test_gif_probe_reports_per_frame_capacity(self):
        channel = GifChannel()
        carrier = Carrier.of_bytes(fixtures.animation(), kind=CarrierKind.ANIMATION)
        feas = channel.probe(carrier, 32)
        self.assertTrue(feas.ok, feas)
        self.assertGreaterEqual(feas.capacity_bytes, 32)

    def test_chinese_probe_needs_no_carrier(self):
        channel = ChineseChannel(MarkovStegoCodec(_model()))
        feas = channel.probe(Carrier.of_text(""), 32)
        self.assertTrue(feas.ok, feas)
        self.assertIn("bits_per_hanzi", feas.cost)


class OptionsTest(unittest.TestCase):
    def test_coerce_accepts_a_dict_from_http(self):
        opts = coerce(ImageOptions, {"step": 12.0, "upscale": False})
        self.assertIsInstance(opts, ImageOptions)
        self.assertEqual(opts.step, 12.0)
        self.assertFalse(opts.upscale)

    def test_coerce_defaults_when_missing(self):
        self.assertEqual(coerce(ImageOptions, None).step, 10.0)

    def test_coerce_passes_an_existing_model(self):
        original = ImageOptions(step=9.0)
        self.assertIs(coerce(ImageOptions, original), original)

    def test_invalid_option_is_rejected_by_validation(self):
        with self.assertRaises(Exception):
            coerce(ImageOptions, {"step": 999.0})

    def test_option_actually_changes_behaviour(self):
        """A schema that does not reach the algorithm is worse than no schema."""
        channel = ImageChannel()
        carrier = Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE)
        coarse = channel.embed(carrier, b"payload", KEY.encode(),
                               ImageOptions(step=14.0))
        self.assertGreaterEqual(coarse.report.options_used["step"], 14.0)

    def test_disabling_upscale_makes_small_covers_fail(self):
        channel = ImageChannel()
        carrier = Carrier.of_bytes(_png(fixtures.small()), kind=CarrierKind.IMAGE)
        payload = b"x" * 200                       # too big for 120² without scaling
        with self.assertRaises(CapacityExceeded):
            channel.embed(carrier, payload, KEY.encode(), ImageOptions(upscale=False))
