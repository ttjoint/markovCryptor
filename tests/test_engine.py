"""Engine and ModelCache behaviour.

The engine's whole reason to exist is to be the *one* place that knows about
model discovery and channel resolution. So the tests here focus on the two
things that would silently hurt if they regressed:

* a model is loaded **once**, not once per operation (batch performance), but
  a changed file on disk is picked up (correctness);
* ``chinese`` exists exactly when a model does -- never a channel that errors
  on first use.
"""

from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from markovcryptor.channels import Carrier, CarrierKind
from markovcryptor.core.errors import ChannelUnavailable, InvalidInput
from markovcryptor.engine import Config, Engine, ModelCache
from markovcryptor.model import MarkovModel, unique_model_path

import fixtures


def _train() -> MarkovModel:
    return MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)


def _png(image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ModelCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mkx1-engine-"))
        self.path = unique_model_path(self.dir, "cachetest")
        _train().save(self.path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_second_load_returns_the_same_object(self):
        cache = ModelCache()
        first = cache.load(self.path)
        second = cache.load(self.path)
        self.assertIs(first, second, "模型被重复加载，批处理会退化")
        self.assertEqual(len(cache), 1)

    def test_changed_file_is_reloaded(self):
        """Editing the JSON in place must not require a process restart."""
        cache = ModelCache()
        first = cache.load(self.path)
        # Bump mtime explicitly: some filesystems have coarse timestamps and a
        # rewrite within the same tick would otherwise look unchanged.
        later = time.time() + 5
        os.utime(self.path, (later, later))
        second = cache.load(self.path)
        self.assertIsNot(first, second)

    def test_missing_file_names_the_path(self):
        cache = ModelCache()
        with self.assertRaises(InvalidInput) as ctx:
            cache.load(self.dir / "nope.json")
        self.assertIn("nope.json", str(ctx.exception))


class ChannelResolutionTest(unittest.TestCase):
    def test_no_model_means_no_chinese_channel(self):
        """A channel that only fails on first use is worse than an absent one."""
        engine = Engine(Config(model_dir=Path(tempfile.gettempdir()) / "mkx1-no-such-dir"))
        ids = [c.id for c in engine.channels()]
        self.assertEqual(ids, ["image", "gif"])
        with self.assertRaises(InvalidInput):
            engine.channel("chinese")

    def test_injected_model_enables_the_chinese_channel(self):
        engine = Engine(model=_train())
        self.assertIn("chinese", [c.id for c in engine.channels()])

    def test_describe_channels_includes_options_schema(self):
        engine = Engine(model=_train())
        described = {d["id"]: d for d in engine.describe_channels()}
        self.assertEqual(set(described), {"image", "gif", "chinese"})
        for cid, data in described.items():
            self.assertIn("properties", data["options_schema"], cid)

    def test_corrupt_model_file_does_not_break_image_work(self):
        """A bad JSON in the model folder must not take the whole engine down."""
        directory = Path(tempfile.mkdtemp(prefix="mkx1-bad-"))
        try:
            (directory / "broken.json").write_text("{not json", encoding="utf-8")
            engine = Engine(Config(model_dir=directory))
            self.assertIn("image", [c.id for c in engine.channels()])
            carrier = Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE)
            env = engine.embed("image", carrier, b"payload", b"key")
            self.assertEqual(engine.extract("image", env.as_carrier(), b"key"), b"payload")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class EngineOperationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = _train()
        cls.engine = Engine(model=cls.model)

    def test_embed_and_extract_through_the_engine(self):
        carrier = Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE)
        env = self.engine.embed("image", carrier, b"engine-payload", b"k")
        self.assertEqual(env.report.channel, "image")
        self.assertEqual(self.engine.extract("image", env.as_carrier(), b"k"), b"engine-payload")

    def test_options_as_a_dict_are_validated(self):
        carrier = Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE)
        env = self.engine.embed("image", carrier, b"x" * 20, b"k", {"step": 12.0})
        self.assertGreaterEqual(env.report.options_used["step"], 12.0)

    def test_invalid_option_reaches_the_caller_as_validation_error(self):
        carrier = Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE)
        with self.assertRaises(Exception):
            self.engine.embed("image", carrier, b"x", b"k", {"step": 1e9})

    def test_probe_does_not_need_a_key(self):
        carrier = Carrier.of_bytes(_png(fixtures.photo()), kind=CarrierKind.IMAGE)
        self.assertTrue(self.engine.probe("image", carrier, 32).ok)

    def test_decode_text_identifies_the_writing_model(self):
        """Two models in play: the right one must be found from the frame."""
        codec = self.engine.codec()
        cover = codec.encode(b"multi-model payload")
        payload, selected = self.engine.decode_text(cover)
        self.assertEqual(payload, b"multi-model payload")
        self.assertIsNotNone(selected)

    def test_identify_returns_none_for_plain_text(self):
        self.assertIsNone(self.engine.identify("这是一段普通的中文，不含任何隐写。"))

    def test_codec_without_a_model_is_a_channel_problem(self):
        engine = Engine(Config(model_dir=Path(tempfile.gettempdir()) / "mkx1-none"))
        with self.assertRaises(ChannelUnavailable) as ctx:
            engine.codec()
        self.assertTrue(ctx.exception.hints, "缺少依赖时必须给出下一步")
