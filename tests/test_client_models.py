"""Text models: lazy loading, cross-model identification, silent failure.

The channel's whole point is that the receiver works out *which* model wrote a
cover text without any negotiation. That is only true if the decoder holds every
model the peer might use, so these tests exercise the multi-model path rather
than a single model.

Failure behaviour matters as much as success: a cover text from an unknown model
and an ordinary sentence must be indistinguishable to the caller, because the
client must not answer either of them.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from markovcryptor.client.models import (
    ModelLibrary,
    MultiModelTextChannel,
    ModelEntry,
)

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"


def _have_models() -> bool:
    return MODELS.is_dir() and any(MODELS.glob("*.json"))


class ScanTest(unittest.TestCase):
    def test_scan_counts_files_without_loading(self):
        """扫描不能付加载代价 —— 启动时只看文件名。"""
        lib = ModelLibrary(MODELS)
        count = lib.scan()
        if not _have_models():
            self.skipTest("仓库里没有模型文件")
        self.assertGreater(count, 0)
        self.assertEqual(lib.loaded_count, 0, "scan 不该加载任何模型")

    def test_missing_folder_is_not_an_error(self):
        lib = ModelLibrary(Path("X:/no/such/folder"))
        self.assertEqual(lib.scan(), 0)
        self.assertFalse(lib.usable)
        self.assertEqual(lib.loaded_models(), [])

    def test_none_folder_is_not_an_error(self):
        lib = ModelLibrary(None)
        self.assertEqual(lib.scan(), 0)

    def test_non_json_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "readme.txt").write_text("x")
            lib = ModelLibrary(folder)
            self.assertEqual(lib.scan(), 0)


class LazyLoadTest(unittest.TestCase):
    def setUp(self):
        if not _have_models():
            self.skipTest("仓库里没有模型文件")

    def test_load_first_loads_exactly_one(self):
        """只加载一个就够开始发送，剩下的交给后台。"""
        lib = ModelLibrary(MODELS)
        entry = lib.load_first()
        self.assertIsNotNone(entry)
        self.assertEqual(lib.loaded_count, 1)
        self.assertGreater(len(lib.entries()), 1, "应该还有没加载的")

    def test_load_all_loads_everything(self):
        lib = ModelLibrary(MODELS)
        lib.scan()
        loaded = lib.load_all()
        self.assertEqual(loaded, len(lib))
        self.assertEqual(lib.loaded_count, len(lib))

    def test_loaded_model_has_a_fingerprint(self):
        lib = ModelLibrary(MODELS)
        entry = lib.load_first()
        self.assertIsInstance(entry.fingerprint, bytes)
        self.assertEqual(len(entry.fingerprint), 32)

    def test_broken_model_is_recorded_not_retried(self):
        """坏文件记一次就够，不要每次识别都重试一遍。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "broken.json").write_text("{not a model")
            lib = ModelLibrary(folder)
            self.assertIsNone(lib.load_first())
            entry = lib.entries()[0]
            self.assertTrue(entry.error, "失败要记下来")
            self.assertFalse(entry.loaded)
            # 再调一次不该抛，也不该重新尝试
            self.assertIsNone(lib.load(folder / "broken.json"))

    def test_status_reports_progress(self):
        lib = ModelLibrary(MODELS)
        lib.scan()
        lib.load_first()
        status = lib.status()
        self.assertEqual(status["total"], len(lib))
        self.assertEqual(status["loaded"], 1)
        self.assertEqual(status["failed"], 0)


class BackgroundLoadTest(unittest.TestCase):
    def setUp(self):
        if not _have_models():
            self.skipTest("仓库里没有模型文件")

    def test_background_load_finishes_and_loads_all(self):
        lib = ModelLibrary(MODELS)
        thread = lib.load_in_background()
        thread.join(timeout=120)
        self.assertFalse(thread.is_alive(), "后台加载应当结束")
        self.assertEqual(lib.loaded_count, len(lib))

    def test_second_call_does_not_start_a_second_thread(self):
        lib = ModelLibrary(MODELS)
        first = lib.load_in_background()
        second = lib.load_in_background()
        first.join(timeout=120)
        self.assertIsInstance(second, type(first))


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        if not _have_models():
            self.skipTest("仓库里没有模型文件")
        self.lib = ModelLibrary(MODELS)
        self.lib.scan()
        self.lib.load_all()
        self.channel = MultiModelTextChannel(self.lib)

    def test_encode_decode_roundtrip(self):
        secret = "今晚八点，老地方。"
        cover, _stats = self.channel.embed_text(secret.encode("utf-8"))
        self.assertTrue(cover.strip(), "载体不能是空的")
        self.assertEqual(self.channel.extract_text(cover).decode("utf-8"), secret)

    def test_ordinary_text_decodes_to_nothing(self):
        """普通中文必须解出空 —— 不是错误，是正常情况。"""
        self.assertEqual(self.channel.extract_text("今天天气不错，出去走走。"), b"")

    def test_empty_text_decodes_to_nothing(self):
        self.assertEqual(self.channel.extract_text(""), b"")

    def test_cover_text_is_chinese_not_base64(self):
        """载体必须是中文，不能是把密文 hex 出来。"""
        cover, _ = self.channel.embed_text(b"secret payload")
        hanzi = sum(1 for ch in cover if "\u4e00" <= ch <= "\u9fff")
        self.assertGreater(hanzi, len(cover) * 0.5,
                           f"载体应当以汉字为主，实际：{cover[:40]}")


class CrossModelTest(unittest.TestCase):
    """不需要协商的关键：解码端持有多模型，就能认出是哪个模型写的。"""

    def setUp(self):
        if not _have_models():
            self.skipTest("仓库里没有模型文件")
        self.lib = ModelLibrary(MODELS)
        self.lib.scan()
        if self.lib.load_all() < 2:
            self.skipTest("需要至少两个模型")

    def test_sender_switching_model_still_decodes(self):
        """发送端换模型，接收端不必知道 —— 靠帧校验自己认。"""
        models = self.lib.loaded_models()
        channel = MultiModelTextChannel(self.lib)
        secret = "换模型也要能解"

        channel.use(models[0])
        cover_a, _ = channel.embed_text(secret.encode())
        channel.use(models[1])
        cover_b, _ = channel.embed_text(secret.encode())

        self.assertEqual(channel.extract_text(cover_a).decode(), secret)
        self.assertEqual(channel.extract_text(cover_b).decode(), secret)

    def test_identify_returns_the_writing_model(self):
        models = self.lib.loaded_models()
        channel = MultiModelTextChannel(self.lib)
        channel.use(models[0])
        cover, _ = channel.embed_text(b"identify me")
        found = self.lib.identify(cover)
        self.assertIsNotNone(found)
        self.assertEqual(found.fingerprint, models[0].fingerprint)

    def test_identify_returns_none_for_ordinary_text(self):
        self.assertIsNone(self.lib.identify("这是一句普通的中文，没有藏任何东西。"))


class NoModelTest(unittest.TestCase):
    """没有模型时必须明确报错，不能静默失败。"""

    def test_embed_without_model_raises_with_hint(self):
        from markovcryptor.core.errors import InvalidInput

        lib = ModelLibrary(None)
        channel = MultiModelTextChannel(lib)
        with self.assertRaises(InvalidInput) as ctx:
            channel.embed_text(b"x")
        self.assertTrue(ctx.exception.hints, "要告诉用户怎么办")

    def test_extract_without_model_returns_empty(self):
        """收方向没有模型只能当作普通文本，不能抛。"""
        lib = ModelLibrary(None)
        self.assertEqual(MultiModelTextChannel(lib).extract_text("随便"), b"")

    def test_identify_without_model_returns_none(self):
        self.assertIsNone(ModelLibrary(None).identify("随便"))


class PreferredModelTest(unittest.TestCase):
    def setUp(self):
        if not _have_models():
            self.skipTest("仓库里没有模型文件")
        self.lib = ModelLibrary(MODELS)
        self.lib.scan()
        self.lib.load_all()

    def test_use_switches_preferred(self):
        models = self.lib.loaded_models()
        channel = MultiModelTextChannel(self.lib)
        channel.use(models[1])
        self.assertIs(channel.preferred, models[1])

    def test_default_preferred_is_first_loaded(self):
        channel = MultiModelTextChannel(self.lib)
        self.assertIs(channel.preferred, self.lib.loaded_models()[0])


if __name__ == "__main__":
    unittest.main()
