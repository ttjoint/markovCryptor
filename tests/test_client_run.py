"""The no-argument entry point: finding things, and failing readably.

``run()`` itself starts servers, so it is not exercised end to end here -- that
belongs to the packaged smoke test on a clean machine. What is tested is the
part that decides *whether* startup can work at all: locating SnowLuma, and
reporting a missing login as an actionable sentence instead of a traceback.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from markovcryptor.client import launcher, run as run_mod


def _fake_snowluma(root: Path, uin="1741473279", port=3001) -> Path:
    """A directory that looks like a SnowLuma install."""
    snow = root / "snowluma"
    (snow / "config").mkdir(parents=True)
    (snow / "index.mjs").write_text("// stub")
    (snow / "config" / f"onebot_{uin}.json").write_text(json.dumps({
        "networks": {"wsServers": [
            {"host": "127.0.0.1", "port": port, "accessToken": "tok"}]}
    }), encoding="utf-8")
    return snow


class FindSnowlumaTest(unittest.TestCase):
    def test_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            snow = _fake_snowluma(Path(tmp))
            self.assertEqual(run_mod.find_snowluma(snow), snow)

    def test_missing_snowluma_names_a_real_path(self):
        """报错要指向一个具体路径，否则用户不知道该去看哪。"""
        got = run_mod.find_snowluma()
        self.assertIsInstance(got, Path)


class RunFailureTest(unittest.TestCase):
    """启动失败必须是「一句话 + 怎么办」，不是 traceback。"""

    def test_no_qq_login_returns_2_with_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snow = root / "snowluma"
            snow.mkdir()
            (snow / "index.mjs").write_text("// stub")
            printed: list[str] = []
            with mock.patch.object(run_mod, "_log", printed.append), \
                 mock.patch.object(launcher, "start_snowluma", return_value=None):
                code = run_mod.run(snowluma=snow, wait=0.3, open_browser=False)
            self.assertEqual(code, 2)
            text = "\n".join(printed)
            self.assertIn("QQ", text, "要说清是 QQ 没登录")
            self.assertIn("怎么办", text, "要给出下一步动作")

    def test_missing_snowluma_returns_2_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            printed: list[str] = []
            with mock.patch.object(run_mod, "_log", printed.append):
                code = run_mod.run(snowluma=Path(tmp) / "nope", wait=0.2,
                                   open_browser=False)
            self.assertEqual(code, 2)
            self.assertTrue(any("启动失败" in line for line in printed))

    def test_config_is_not_written_when_startup_fails(self):
        """失败时不能把半截状态写进配置文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snow = root / "snowluma"
            snow.mkdir()
            (snow / "index.mjs").write_text("// stub")
            cfg_path = root / "client.json"
            with mock.patch.object(run_mod, "_log", lambda *_: None), \
                 mock.patch.object(launcher, "start_snowluma", return_value=None):
                run_mod.run(snowluma=snow, config_path=cfg_path, wait=0.2,
                            open_browser=False)
            self.assertFalse(cfg_path.exists(), "失败不该留下配置文件")


class ConsoleTest(unittest.TestCase):
    def test_log_survives_missing_stdout(self):
        """打包后用 pythonw 跑，没有 stdout —— 往关闭的流写会抛。"""
        with mock.patch.object(run_mod.sys, "stdout", None):
            run_mod._log("应该不抛异常")


if __name__ == "__main__":
    unittest.main()
