from __future__ import annotations

from bisect import bisect_right
from itertools import accumulate
import json
from pathlib import Path
import random
import re
import tempfile
import unittest

from markovcryptor import (
    MarkovModel,
    MarkovStegoCodec,
    decode_with_models,
    default_model_dir,
    extract_hanzi,
    identify_model,
    unique_model_path,
)
from markovcryptor.arith import ArithmeticDecoder, ArithmeticEncoder, BitReader
from markovcryptor.compression import METHOD_NAMES, TEXT, TextModel, compress, decompress
from markovcryptor.progress import Cancelled
from markovcryptor.punctuation import CLOSE_TO_OPEN, OPEN_TO_CLOSE, TERMINAL_MARKS, is_hanzi

CORPUS = (Path(__file__).resolve().parent / "sample_corpus.txt").read_text(encoding="utf-8")


def _assert_balanced(test: unittest.TestCase, text: str) -> None:
    """Quotes/brackets pair up, never nest inside themselves, and book titles hold only hanzi."""
    stack: list[str] = []
    for char in text:
        if char in OPEN_TO_CLOSE:
            test.assertNotIn(char, stack, msg="bracket nested inside itself")
            stack.append(char)
        elif char in CLOSE_TO_OPEN:
            test.assertTrue(stack, msg=f"unmatched {char!r}")
            test.assertEqual(stack.pop(), CLOSE_TO_OPEN[char])
        elif char == "\n":
            test.assertEqual(stack, [], msg="line break inside an open quote/bracket")
        elif stack and stack[-1] in "《〈":
            test.assertTrue(is_hanzi(char), msg=f"{char!r} inside a book title")
    test.assertEqual(stack, [])


class MarkovStegoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = MarkovModel.train(CORPUS, vocabulary_size=128, smoothing=1)
        cls.codec = MarkovStegoCodec(cls.model)

    def test_binary_round_trip(self) -> None:
        rng = random.Random(7)
        payloads = [b"", b"hello", bytes(range(256)), bytes(rng.getrandbits(8) for _ in range(64))]
        for payload in payloads:
            with self.subTest(length=len(payload)):
                cover = self.codec.encode(payload)
                self.assertEqual(self.codec.decode(cover), payload)
                self.assertGreater(len(extract_hanzi(cover)), 0)

    def test_model_is_hanzi_only(self) -> None:
        self.assertTrue(all(is_hanzi(char) for char in self.model.vocabulary))
        self.assertIsNotNone(self.model.punctuation)

    def test_data_lives_only_in_hanzi(self) -> None:
        payload = "改标点、重新排版都不影响解码。".encode()
        hanzi = self.codec.encode_hanzi(payload)
        self.assertTrue(all(is_hanzi(char) for char in hanzi))
        self.assertEqual(self.codec.decode(hanzi), payload)
        # Different punctuation renderings of the same hanzi all decode.
        for seed in range(5):
            rendered = self.model.render(hanzi, random.Random(seed))
            self.assertEqual(extract_hanzi(rendered), hanzi)
            self.assertEqual(self.codec.decode(rendered), payload)
        # Stripping, replacing and re-wrapping punctuation is harmless.
        cover = self.codec.encode(payload)
        edited = re.sub(r"[，。！？；：、]", "…", cover).replace("\n", "\r\n\r\n")
        edited = "  ".join(edited[i : i + 7] for i in range(0, len(edited), 7))
        self.assertEqual(self.codec.decode(edited), payload)
        self.assertEqual(self.codec.decode(extract_hanzi(cover)), payload)

    def test_no_punctuation_option(self) -> None:
        cover = self.codec.encode(b"bare", punctuate=False)
        self.assertEqual(cover, extract_hanzi(cover))
        self.assertEqual(self.codec.decode(cover), b"bare")

    def test_password_round_trip(self) -> None:
        cover = self.codec.encode(b"secret", "right")
        self.assertEqual(self.codec.decode(cover, "right"), b"secret")
        with self.assertRaises(ValueError):
            self.codec.decode(cover)

    def test_model_save_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            self.model.save(path)
            loaded = MarkovModel.load(path)
            self.assertEqual(loaded.to_dict(), self.model.to_dict())
            self.assertEqual(loaded.fingerprint, self.model.fingerprint)
            payload = "模型文件可以独立保存和加载。".encode()
            self.assertEqual(MarkovStegoCodec(loaded).decode(self.codec.encode(payload)), payload)

    def test_old_model_format_is_rejected(self) -> None:
        payload = self.model.to_dict()
        payload["format"] = "markovcryptor-model-v2"
        with self.assertRaisesRegex(ValueError, "unsupported model format"):
            MarkovModel.from_dict(payload)

    def test_default_model_dir(self) -> None:
        self.assertEqual(default_model_dir("x"), Path("x") / "models")

    def test_tampering_is_rejected(self) -> None:
        # Rewrite one payload character.  A single substitution can land on
        # something the arithmetic coder still reconciles, so we require that
        # the checksum catches *some* substitution rather than one specific one.
        hanzi = self.codec.encode_hanzi(b"tamper detection")
        detections = 0
        for index in range(len(hanzi) - 1, max(len(hanzi) - 9, 0), -1):
            replacement = next(
                char for char in self.model.vocabulary if char != hanzi[index])
            tampered = hanzi[:index] + replacement + hanzi[index + 1 :]
            try:
                self.codec.decode(tampered)
            except ValueError:
                detections += 1
        self.assertGreater(detections, 0, "篡改完全未被检测到")

    def test_random_salt_changes_output(self) -> None:
        payload = b"same input should not produce the same cover text" * 4
        first, second = self.codec.encode(payload), self.codec.encode(payload)
        self.assertNotEqual(extract_hanzi(first), extract_hanzi(second))
        self.assertEqual(self.codec.decode(first), self.codec.decode(second))

    def test_rendered_punctuation_is_well_formed(self) -> None:
        rng = random.Random(11)
        saw_break = False
        for _ in range(10):
            payload = bytes(rng.getrandbits(8) for _ in range(400))
            cover = self.codec.encode(payload, rng=random.Random(rng.random()))
            self.assertEqual(self.codec.decode(cover), payload)
            _assert_balanced(self, cover)
            stripped = cover.rstrip("".join(CLOSE_TO_OPEN))
            self.assertIn(stripped[-1], TERMINAL_MARKS)
            lines = cover.split("\n")
            saw_break |= len(lines) > 1
            for line in lines[:-1]:
                if line:
                    self.assertIn(line.rstrip("".join(CLOSE_TO_OPEN))[-1], TERMINAL_MARKS, msg=line)
        self.assertTrue(saw_break)

    def test_punctuation_summary(self) -> None:
        summary = self.model.punctuation.summary()
        self.assertGreater(summary["token_kinds"], 0)
        self.assertGreater(summary["mean_clause_length"], 0)
        counts = self.model.punctuation.token_counts()
        self.assertEqual(counts, sorted(counts, key=lambda item: -item[1]))

    def test_frame_identifies_writing_model(self) -> None:
        half = len(CORPUS) // 2
        others = [
            MarkovModel.train(CORPUS, vocabulary_size=100, smoothing=1),
            MarkovModel.train(CORPUS, vocabulary_size=128, smoothing=2),
            MarkovModel.train(CORPUS[half:] + CORPUS[:half], vocabulary_size=128, smoothing=1),
        ]
        models = [*others, self.model]
        self.assertEqual(len({model.fingerprint for model in models}), len(models))
        payload = b"which model wrote me?"
        for writer in models:
            with self.subTest(writer=writer.model_id):
                try:
                    cover = MarkovStegoCodec(writer).encode(payload)
                except ValueError:
                    continue  # a tiny vocabulary may not carry the fixed frame
                self.assertIs(identify_model(cover, models), writer)
                recovered, selected = decode_with_models(cover, models)
                self.assertEqual(recovered, payload)
                self.assertIs(selected, writer)

    def test_identified_model_reports_wrong_password(self) -> None:
        cover = self.codec.encode(b"secret", "right")
        with self.assertRaisesRegex(ValueError, "identified model"):
            decode_with_models(cover, [self.model], "wrong")

    def test_metadata_does_not_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "新闻.txt"
            corpus.write_text(CORPUS, encoding="utf-8")
            model = MarkovModel.train_from_files([corpus], vocabulary_size=128)
            self.assertEqual(model.name, "新闻")
            self.assertEqual(model.metadata["sources"][0]["name"], "新闻.txt")
            self.assertEqual(model.metadata["corpus_characters"], len(CORPUS))
            self.assertIn("created_at", model.metadata)
            self.assertEqual(model.fingerprint, self.model.fingerprint)
            path = unique_model_path(directory, model.name)
            model.save(path)
            self.assertNotEqual(unique_model_path(directory, model.name), path)
            loaded = MarkovModel.load(path)
            self.assertEqual(loaded.metadata, model.metadata)
            cover = MarkovStegoCodec(model).encode(b"renamed")
            loaded.metadata["name"] = "renamed"
            self.assertEqual(decode_with_models(cover, [loaded])[0], b"renamed")

    def test_multiple_corpus_files_and_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first.txt").write_text(CORPUS, encoding="utf-8")
            (root / "second.txt").write_text(CORPUS, encoding="utf-8")
            merged = MarkovModel.train_from_files([root / "first.txt", root / "second.txt"], vocabulary_size=128)
            trained = MarkovModel.train_many(root, vocabulary_size=128)
            self.assertEqual(len(trained), 2)
            payload = b"multi-model decoding"
            cover = MarkovStegoCodec(merged).encode(payload)
            recovered, selected = decode_with_models(cover, [trained[0][1], merged])
            self.assertEqual(recovered, payload)
            self.assertIn(selected, (trained[0][1], merged))

    def test_non_utf8_corpus_encodings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "utf8.txt").write_text(CORPUS, encoding="utf-8")
            (root / "utf8bom.txt").write_text(CORPUS, encoding="utf-8-sig")
            (root / "gbk.txt").write_text(CORPUS, encoding="gb18030")
            (root / "utf16.txt").write_text(CORPUS, encoding="utf-16")
            reference = MarkovModel.train_from_files(root / "utf8.txt", vocabulary_size=128)
            for name in ("utf8bom.txt", "gbk.txt", "utf16.txt"):
                with self.subTest(name=name):
                    model = MarkovModel.train_from_files(root / name, vocabulary_size=128)
                    self.assertEqual(model.vocabulary, reference.vocabulary)
                    self.assertEqual(model.training_tokens, reference.training_tokens)

    def test_text_is_compressed_before_hiding(self) -> None:
        text = CORPUS[5000:8000]
        cover, stats = self.codec.encode_with_stats(text.encode())
        self.assertEqual(stats.method, METHOD_NAMES[TEXT])
        self.assertLess(stats.compressed_bytes, len(text.encode()) / 2.5)
        self.assertEqual(self.codec.decode(cover).decode(), text)

    def test_compressed_payload_is_encrypted(self) -> None:
        text = CORPUS[9000:9500].encode()
        cover, stats = self.codec.encode_with_stats(text, "pw")
        self.assertEqual(stats.method, METHOD_NAMES[TEXT])
        self.assertEqual(self.codec.decode(cover, "pw"), text)

    def test_progress_reports_and_cancels(self) -> None:
        seen: list[float] = []
        payload = CORPUS[:20000].encode()
        cover = self.codec.encode(payload, progress=lambda fraction, _: seen.append(fraction))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(self.codec.decode(cover, progress=lambda *_: None), payload)

        def cancel(fraction: float, _: str) -> None:
            if fraction > 0.1:
                raise Cancelled()

        with self.assertRaises(Cancelled):
            self.codec.encode(payload, progress=cancel)

    def test_training_reports_progress(self) -> None:
        seen: list[float] = []
        MarkovModel.train(CORPUS, vocabulary_size=128, progress=lambda fraction, _: seen.append(fraction))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)


class CompressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text_model = TextModel.train(CORPUS)

    def test_methods_round_trip(self) -> None:
        samples = [
            b"",
            b"a",
            "纯中文的一句话。".encode(),
            "Mixed 中英文 text, 数字 12345 and emoji 😀\r\n第二行\r\n".encode(),
            "lone\rCR and\nLF".encode(),
            CORPUS[:5000].encode(),
            bytes(range(256)) * 8,
            random.Random(3).randbytes(4000),
        ]
        for payload in samples:
            method, data = compress(payload, self.text_model)
            with self.subTest(method=METHOD_NAMES[method], length=len(payload)):
                self.assertEqual(decompress(method, data, self.text_model), payload)
                self.assertLessEqual(len(data), len(payload))

    def test_text_model_beats_general_compressors_on_short_text(self) -> None:
        method, data = compress(CORPUS[30000:30100].encode(), self.text_model)
        self.assertEqual(method, TEXT)
        self.assertLess(len(data), 110)

    def test_text_model_serialises(self) -> None:
        loaded = TextModel.from_dict(json.loads(json.dumps(self.text_model.to_dict())))
        sample = CORPUS[100:900]
        self.assertEqual(loaded.compress(sample), self.text_model.compress(sample))

    def test_arithmetic_coder_round_trip(self) -> None:
        rng = random.Random(5)
        for _ in range(50):
            frequencies = [rng.randint(1, 5000) for _ in range(rng.randint(2, 400))]
            cumulative = [0, *accumulate(frequencies)]
            total = cumulative[-1]
            symbols = [rng.randrange(len(frequencies)) for _ in range(rng.randint(0, 300))]
            encoder = ArithmeticEncoder()
            for symbol in symbols:
                encoder.encode(cumulative[symbol], cumulative[symbol + 1], total)
            decoder = ArithmeticDecoder(BitReader(encoder.finish()))
            decoded = []
            for _ in symbols:
                symbol = bisect_right(cumulative, decoder.target(total)) - 1
                decoder.consume(cumulative[symbol], cumulative[symbol + 1], total)
                decoded.append(symbol)
            self.assertEqual(decoded, symbols)


if __name__ == "__main__":
    unittest.main()
