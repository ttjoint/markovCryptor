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


class StartupLogTest(unittest.TestCase):
    """启动失败必须留下能查的痕迹。

    打包版用 pythonw 跑（没有控制台）。原来 _log 只写 stdout，失败时
    双击之后**什么都没发生** —— 用户只会认为包是坏的。日志文件是唯一
    能在事后查到原因的地方。
    """

    def test_log_writes_to_a_file_even_without_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "client.log"
            with mock.patch.object(run_mod, "_log_path", return_value=log), \
                 mock.patch.object(run_mod.sys, "stdout", None):
                run_mod._log("启动失败：QQ 没登录")
            self.assertTrue(log.exists(), "没有控制台时也必须写日志")
            self.assertIn("QQ 没登录", log.read_text(encoding="utf-8"))

    def test_log_failure_never_raises(self):
        """日志写不进去（只读目录）也不能让程序崩。"""
        with mock.patch.object(run_mod, "_log_path",
                               return_value=Path("X:/nope/client.log")), \
             mock.patch.object(run_mod.sys, "stdout", None):
            run_mod._log("随便")      # 不抛即通过

    def test_failure_is_recorded_in_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snow = root / "snowluma"
            snow.mkdir()
            (snow / "index.mjs").write_text("// stub")
            log = root / "client.log"
            with mock.patch.object(run_mod, "_log_path", return_value=log), \
                 mock.patch.object(launcher, "start_snowluma", return_value=None), \
                 mock.patch.object(run_mod.sys, "stdout", None):
                code = run_mod.run(snowluma=snow, wait=0.2, open_browser=False)
            self.assertEqual(code, 2)
            text = log.read_text(encoding="utf-8")
            self.assertIn("启动失败", text)
            self.assertIn("QQ", text)


class PortRediscoveryTest(unittest.TestCase):
    """自己启动了 SnowLuma，就必须重新读端口。

    实际故障（虚拟机重启后）：上次运行留下的 config/onebot_<QQ>.json 里写着
    3001，这次 SnowLuma 重新分配成 3000。旧代码拿启动前的 3001 去等端口，
    死等到超时，报"OneBot 端口没起来：ws://127.0.0.1:3001/"。
    SnowLuma 其实起来了，只是端口不是那个。
    """

    def test_stale_port_is_not_used_after_starting_snowluma(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snow = _fake_snowluma(root, port=3001)   # 旧配置写 3001

            started = []
            waited = []

            def fake_start(path, **kwargs):
                started.append(path)
                # 启动后 SnowLuma 把端口改成了 3000 —— 这才是真实情况。
                (snow / "config" / "onebot_1741473279.json").write_text(
                    json.dumps({"networks": {"wsServers": [
                        {"host": "127.0.0.1", "port": 3000,
                         "accessToken": "tok"}]}}), encoding="utf-8")
                return None

            def fake_wait_onebot(url, timeout=None):
                waited.append(url)
                # 只有 3000 是通的；3001 永远不通。
                return url.endswith(":3000/")

            # run() 后半段会真的连 QQ 并起 HTTP 服务；这里只关心端口选择，
            # 所以把"连上之后"的部分整体打桩。
            with mock.patch.object(run_mod.launcher, "start_snowluma", fake_start), \
                 mock.patch.object(run_mod.launcher, "wait_for_onebot", fake_wait_onebot), \
                 mock.patch.object(run_mod, "_serve", return_value=0), \
                 mock.patch.object(run_mod, "_log"):
                code = run_mod.run(snowluma=snow, open_browser=False)

            self.assertTrue(started, "应当启动了 SnowLuma")
            self.assertIn("ws://127.0.0.1:3000/", waited,
                          "启动后必须用新发现的端口，不能用旧配置的 3001")
            self.assertNotEqual(code, 2,
                                "端口变了不该导致启动失败")

    def test_reuses_running_instance_without_starting_a_second(self):
        """已经在听就复用，不要起第二份（会 EADDRINUSE）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snow = _fake_snowluma(root, port=3001)

            started = []
            with mock.patch.object(run_mod.launcher, "start_snowluma",
                                   lambda *a, **k: started.append(1)), \
                 mock.patch.object(run_mod.launcher, "wait_for_onebot",
                                   lambda url, timeout=None: True), \
                 mock.patch.object(run_mod, "_serve", return_value=0), \
                 mock.patch.object(run_mod, "_log"):
                run_mod.run(snowluma=snow, open_browser=False)

            self.assertEqual(started, [], "已在运行时不该再启动一份")
