"""End-to-end tests for the command line interface.

These exercise the real entry points (``python -m markovcryptor.cli``) rather than
calling library functions, so argument parsing, exit codes and progress output
are covered too.  Every test builds its own temporary model directory from the
versioned fixture corpus — nothing here depends on ``data/``, ``corpus/`` or
``models/``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fixtures as F

ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a subprocess, exactly as a user would."""
    return subprocess.run(
        [sys.executable, "-m", "markovcryptor.cli", *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.model_dir = cls.tmp / "models"
        cls.model_dir.mkdir()
        result = _run(
            "train",
            "--corpus", str(F.require(F.CORPUS)),
            "--model-dir", str(cls.model_dir),
            "--name", "fixture",
        )
        if result.returncode != 0:
            raise AssertionError(
                f"训练失败，后续测试无意义\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        cls.models = sorted(cls.model_dir.glob("*.json"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.assertTrue(self.models, "训练应产出至少一个模型文件")

    def test_help_lists_subcommands(self):
        result = _run("--help")
        self.assertEqual(result.returncode, 0)
        for command in ("train", "encode", "decode", "models", "identify"):
            self.assertIn(command, result.stdout)

    def test_output_survives_a_gbk_console(self):
        """中文控制台（cp936）下也不能把中文输出变成乱码。

        踩过的坑：Win10 的 cmd 是 GBK，``print("可承载")`` 会抛
        ``UnicodeEncodeError``；即便加了 ``errors="replace"``，输出也
        会变成一串 ``�``，测试里断言中文就永远失败。
        CLI 入口现在自己把 stdout 切成 UTF-8，这条测试守住它。
        """
        result = _run("channels")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("�", result.stdout, "输出里有替换字符，中文被吞了")
        # 任意一段中文能读出来，就说明编码对了
        self.assertTrue(any("一" <= ch <= "鿿" for ch in result.stdout),
                        f"stdout 里一个汉字都没有：{result.stdout!r}")

    def test_models_lists_trained_model(self):
        result = _run("models", "--model", str(self.model_dir))
        self.assertEqual(result.returncode, 0)
        self.assertIn("fixture", result.stdout)

    def test_encode_decode_roundtrip(self):
        payload = self.tmp / "payload.bin"
        payload.write_bytes(bytes(range(48)))
        hidden = self.tmp / "hidden.txt"
        recovered = self.tmp / "recovered.bin"

        encoded = _run(
            "encode", "--model", str(self.models[0]),
            "--input", str(payload), "--output", str(hidden),
        )
        self.assertEqual(encoded.returncode, 0, encoded.stderr)
        self.assertTrue(hidden.exists())

        # The carrier must look like prose: no raw payload bytes leaking through.
        text = hidden.read_text(encoding="utf-8")
        self.assertNotIn("MAGIC", text)

        decoded = _run(
            "decode", "--model", str(self.model_dir),
            "--input", str(hidden), "--output", str(recovered),
        )
        self.assertEqual(decoded.returncode, 0, decoded.stderr)
        self.assertEqual(recovered.read_bytes(), bytes(range(48)))

    def test_identify_reports_model(self):
        payload = self.tmp / "payload.bin"
        payload.write_bytes(b"identify me")
        hidden = self.tmp / "hidden.txt"
        _run("encode", "--model", str(self.models[0]),
             "--input", str(payload), "--output", str(hidden))

        result = _run("identify", "--model", str(self.model_dir),
                      "--input", str(hidden))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture", result.stdout)

    def test_decode_wrong_model_fails_loudly(self):
        """A mismatched model must error, not silently emit garbage."""
        # Not created here: `train` refuses an existing directory it does not
        # own, so let it create the folder itself.
        other_dir = self.tmp / "models_other"
        # Train a second model on deliberately different statistics.  Needs
        # >= 64 distinct hanzi or training refuses (a real constraint of the
        # trainer, not a test artefact), so build a wide alphabet rather than
        # repeating a short one.
        alt_corpus = self.tmp / "alt.txt"
        alphabet = "".join(chr(0x4E00 + i) for i in range(120))
        alt_corpus.write_text(
            "\n".join(alphabet[i:i + 60] for i in range(0, len(alphabet), 60)) * 20,
            encoding="utf-8",
        )
        trained = _run("train", "--corpus", str(alt_corpus),
                       "--model", str(other_dir), "--name", "alt")
        self.assertEqual(trained.returncode, 0, trained.stderr)

        payload = self.tmp / "payload.bin"
        payload.write_bytes(bytes(range(24)))
        hidden = self.tmp / "hidden.txt"
        _run("encode", "--model", str(self.models[0]),
             "--input", str(payload), "--output", str(hidden))

        result = _run("decode", "--model", str(other_dir),
                      "--input", str(hidden), "--output", str(self.tmp / "out.bin"))
        # Either it refuses (correct) or it decodes to the right answer by
        # luck — what it must never do is claim a wrong result silently.
        if result.returncode == 0:
            self.assertEqual((self.tmp / "out.bin").read_bytes(), bytes(range(24)))
        else:
            # The message names the real reason ("no supplied model matches
            # this cover text") — assert on that, not on a vague phrase.
            self.assertIn("no supplied model matches", result.stderr + result.stdout)

    def test_demo_runs(self):
        result = _run("demo", "--model", str(self.models[0]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("round-trip ok", result.stdout + result.stderr)

    def test_channels_lists_every_channel_with_its_options(self):
        result = _run("channels")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        for cid in ("image", "gif"):
            self.assertIn(cid, out)
        # An option list is what lets a human (or a form generator) discover
        # the knobs without reading source.
        self.assertIn("step", out)

    def test_probe_reports_capacity_without_embedding(self):
        result = _run("probe", "--channel", "image",
                      "--input", str(F.PHOTO), "--bytes", "32")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("可承载", result.stdout)

    def test_probe_fails_with_the_real_cause_when_too_big(self):
        result = _run("probe", "--channel", "image",
                      "--input", str(F.PHOTO), "--bytes", "100000")
        self.assertEqual(result.returncode, 1)
        # The rule: never a generic "cannot carry", always the numbers.
        self.assertIn("最多", result.stdout)
        self.assertIn("需要", result.stdout)

    def test_probe_rejects_an_unknown_channel_and_says_what_exists(self):
        result = _run("probe", "--channel", "video",
                      "--input", str(F.PHOTO), "--bytes", "32")
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("image", combined)

    def test_batch_embeds_a_folder_and_reports(self):
        from io import BytesIO

        images = self.tmp / "batch-in"
        images.mkdir()
        buffer = BytesIO()
        F.photo().save(buffer, format="PNG")
        for i in range(3):
            (images / f"p{i}.png").write_bytes(buffer.getvalue())
        out = self.tmp / "batch-out"
        payload = self.tmp / "batch-payload.bin"
        payload.write_bytes(b"batch-cli-secret")
        report = self.tmp / "batch-report.csv"

        result = _run("batch", "--channel", "image", "--input", str(images),
                      "--output", str(out), "--payload", str(payload),
                      "--key", "k", "--report", str(report))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list(out.glob("*.png"))), 3)
        self.assertTrue(report.exists())
        self.assertIn("3 成功", result.stdout)

    def test_batch_resume_skips_finished_items(self):
        from io import BytesIO

        images = self.tmp / "resume-in"
        images.mkdir()
        buffer = BytesIO()
        F.photo().save(buffer, format="PNG")
        (images / "a.png").write_bytes(buffer.getvalue())
        out = self.tmp / "resume-out"
        payload = self.tmp / "resume-payload.bin"
        payload.write_bytes(b"resume")
        checkpoint = self.tmp / "resume.jsonl"

        args = ("batch", "--channel", "image", "--input", str(images),
                "--output", str(out), "--payload", str(payload),
                "--key", "k", "--resume", str(checkpoint))
        first = _run(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        import shutil
        shutil.rmtree(out)

        second = _run(*args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(out.exists(), "续跑不应重新生成输出")

    def test_batch_without_payload_is_rejected(self):
        from io import BytesIO

        images = self.tmp / "nopay"
        images.mkdir()
        buffer = BytesIO()
        F.photo().save(buffer, format="PNG")
        (images / "a.png").write_bytes(buffer.getvalue())
        result = _run("batch", "--channel", "image", "--input", str(images),
                      "--output", str(self.tmp / "nopay-out"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--payload", result.stderr)

    def test_serve_refuses_a_public_bind(self):
        """No auth on this service -- leaving loopback must fail loudly."""
        result = _run("serve", "--host", "0.0.0.0", "--port", "8731")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0.0.0.0", result.stderr)

    def test_serve_help_shows_the_configurable_port(self):
        result = _run("serve", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--port", result.stdout)
        self.assertIn("8731", result.stdout)

    def test_missing_corpus_exits_nonzero(self):
        result = _run("train", "--corpus", str(self.tmp / "nope.txt"),
                      "--model", str(self.tmp / "x"))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip() or result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
