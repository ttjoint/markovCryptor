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


class OpenUiWhenReadyTest(unittest.TestCase):
    """浏览器要等端口真的在听之后再开。

    **实锤**：原来是"先 open_ui() 再 server.run()"，靠谁快谁慢赌。用户
    本地启动时抢在 uvicorn 前面，看到一个 ERR_CONNECTION_REFUSED 的窗口，
    关掉之后整个程序以退出码 1 结束 —— 现象看着像"启动失败"，其实只是
    打开得太早。冷启动 / 虚拟机 / 杀毒扫描时必然踩到，快机器上看不出来。
    """

    def setUp(self):
        self.calls = []
        self._orig_open = run_mod.browser_mod.open_ui
        self._orig_port = run_mod.launcher._port_open
        self._orig_log = run_mod._log

    def tearDown(self):
        run_mod.browser_mod.open_ui = self._orig_open
        run_mod.launcher._port_open = self._orig_port
        run_mod._log = self._orig_log

    def test_does_not_open_before_port_is_listening(self):
        """端口没起来时绝不能开浏览器。"""
        run_mod.launcher._port_open = lambda host, port, timeout=0.4: False
        run_mod.browser_mod.open_ui = lambda url, **k: self.calls.append(url) or ("x", 0)

        run_mod._open_when_ready("http://127.0.0.1:8741/", 8741, object(),
                                 timeout=0.4)

        self.assertEqual(self.calls, [], "端口没起来就开了浏览器")

    def test_opens_once_port_is_listening(self):
        run_mod.launcher._port_open = lambda host, port, timeout=0.4: True
        run_mod.browser_mod.open_ui = lambda url, **k: self.calls.append(url) or ("x", 0)
        run_mod._log = lambda *a, **k: None

        run_mod._open_when_ready("http://127.0.0.1:8741/", 8741, object())

        self.assertEqual(self.calls, ["http://127.0.0.1:8741/"])

    def test_waits_for_a_port_that_opens_late(self):
        """端口晚一点起来也要等 —— 慢机器就靠这个。"""
        state = {"n": 0}

        def late_port(host, port, timeout=0.4):
            state["n"] += 1
            return state["n"] >= 3                # 第三次才通

        run_mod.launcher._port_open = late_port
        run_mod.browser_mod.open_ui = lambda url, **k: self.calls.append(url) or ("x", 0)
        run_mod._log = lambda *a, **k: None

        run_mod._open_when_ready("http://127.0.0.1:8741/", 8741, object(),
                                 timeout=5.0)

        self.assertEqual(self.calls, ["http://127.0.0.1:8741/"])
        self.assertGreaterEqual(state["n"], 3)

    def test_browser_failure_does_not_kill_the_backend(self):
        """开浏览器失败不该带崩后端 —— 用户还能手动打开地址。"""
        run_mod.launcher._port_open = lambda host, port, timeout=0.4: True

        def boom(url, **k):
            raise OSError("no chrome")

        run_mod.browser_mod.open_ui = boom
        # 不该抛异常
        run_mod._open_when_ready("http://127.0.0.1:8741/", 8741, object())


class OrphanCleanupTest(unittest.TestCase):
    """上次没退干净的 SnowLuma 必须被收掉。

    **实锤**：反复启动调试下来后台堆了十几个 node.exe，每个都占着
    ``snowluma/data`` 里的数据库 —— 下次启动直接"文件被占用"，虚拟机里
    甚至要手动去任务管理器收拾。

    ``shutdown()`` 只在正常退出这条路上跑；异常、Ctrl+C、关控制台窗口、
    pythonw 崩溃都不经过它。所以要有磁盘记账 + 下次启动清理。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = run_mod._orphan_path
        run_mod._orphan_path = lambda: self.dir / "child.json"

    def tearDown(self):
        run_mod._orphan_path = self._orig
        self._tmp.cleanup()

    def test_records_and_forgets(self):
        run_mod._remember_child(12345, root=Path("D:/x/snowluma"))
        self.assertTrue((self.dir / "child.json").exists())

        run_mod._forget_child()
        self.assertFalse((self.dir / "child.json").exists())

    def test_reap_ignores_dead_pid(self):
        """pid 早就没了 —— 不该调 taskkill，但记录要清掉。"""
        run_mod._remember_child(999999, root=Path("D:/x/snowluma"))
        with mock.patch.object(run_mod, "_pid_is_snowluma", return_value=False),              mock.patch.object(run_mod.subprocess, "run") as fake:
            run_mod._reap_previous_orphan()
        fake.assert_not_called()
        self.assertFalse((self.dir / "child.json").exists(),
                         "记录应当被清掉")

    def test_reap_kills_our_orphan(self):
        run_mod._remember_child(4242, root=Path("D:/x/snowluma"))
        with mock.patch.object(run_mod, "_pid_is_snowluma", return_value=True),              mock.patch.object(run_mod.subprocess, "run") as fake:
            run_mod._reap_previous_orphan()
        fake.assert_called_once()
        argv = fake.call_args[0][0]
        self.assertIn("4242", argv)
        self.assertIn("/T", argv, "必须连子进程一起收，node 会 fork")
        self.assertIn("/F", argv)

    def test_pid_check_rejects_unrelated_process(self):
        """pid 被别的程序复用了 —— 绝不能杀。"""
        result = mock.Mock(returncode=0, stdout="C:\Windows\notepad.exe")
        with mock.patch.object(run_mod.subprocess, "run", return_value=result):
            self.assertFalse(run_mod._pid_is_snowluma(4242, "D:/x/snowluma"))

    def test_pid_check_rejects_different_snowluma(self):
        """是 SnowLuma，但不是**我们上次起的那个**（不同路径）。"""
        result = mock.Mock(
            returncode=0,
            stdout='node.exe "D:/other/snowluma/index.mjs"')
        with mock.patch.object(run_mod.subprocess, "run", return_value=result):
            self.assertFalse(
                run_mod._pid_is_snowluma(4242, "D:/x/snowluma"),
                "路径不同就不能认作我们的")

    def test_pid_check_accepts_our_own(self):
        result = mock.Mock(
            returncode=0,
            stdout='node.exe "D:/x/snowluma/index.mjs"')
        with mock.patch.object(run_mod.subprocess, "run", return_value=result):
            self.assertTrue(run_mod._pid_is_snowluma(4242, "D:/x/snowluma"))


class RestartKeepsOwnershipTest(unittest.TestCase):
    def test_restart_registers_new_process_even_if_it_throws(self):
        """重启失败不能把进程所有权弄丢。

        原来是先摘掉旧的、再 start；start 抛异常就等于"旧的杀了、新的没起、
        也没人管"。顺序改成"先起新的、再登记"。
        """
        import markovcryptor.client.launcher as launch_mod
        from markovcryptor.client.lifecycle import OwnedProcesses

        owned = OwnedProcesses()
        old_process = mock.Mock()
        owned.add("SnowLuma", old_process)

        with mock.patch.object(launch_mod, "start_snowluma",
                               side_effect=OSError("no node")):
            with self.assertRaises(OSError):
                launch_mod.restart_snowluma(Path("."), owned)

        # 旧的一定要已经尝试停掉
        old_process.terminate.assert_called_once()

    def test_restart_registers_the_new_one(self):
        import markovcryptor.client.launcher as launch_mod
        from markovcryptor.client.lifecycle import OwnedProcesses

        owned = OwnedProcesses()
        new_process = mock.Mock()
        with mock.patch.object(launch_mod, "start_snowluma",
                               return_value=new_process):
            got = launch_mod.restart_snowluma(Path("."), owned)
        self.assertIs(got, new_process)
        self.assertIn("SnowLuma", owned.labels(),
                      "新进程必须登记进 owned，否则退出时不会收")


class BrowserProfileTest(unittest.TestCase):
    """每个实例一个 Chromium profile。

    ``--user-data-dir`` 是独占的：第二个进程拿同一个目录时会把 URL 交给
    已经在跑的那个实例，然后自己退出。后果是用户再开一个窗口根本不出现，
    看起来像"127.0.0.1 拒绝连接"。
    """

    def test_different_ports_get_different_profiles(self):
        from markovcryptor.client import browser as browser_mod

        with mock.patch.dict("os.environ", {"APPDATA": tempfile.gettempdir()}):
            a = browser_mod._profile_dir(8741)
            b = browser_mod._profile_dir(8742)
        self.assertNotEqual(a, b, "两个实例不能共用 profile")
        self.assertIn("8741", a)
        self.assertIn("8742", b)

    def test_same_port_is_stable_across_runs(self):
        """同一个端口要复用同一个目录 —— 否则每次启动都丢窗口尺寸。"""
        from markovcryptor.client import browser as browser_mod

        with mock.patch.dict("os.environ", {"APPDATA": tempfile.gettempdir()}):
            first = browser_mod._profile_dir(8741)
            second = browser_mod._profile_dir(8741)
        self.assertEqual(first, second)

    def test_open_ui_failure_is_visible_in_the_log(self):
        """打不开浏览器必须说出来，不能静默退出。

        **实锤**：日志停在"客户端已启动"然后是退出码 1 —— 后端好好的，
        只是窗口没开，但用户只看到"启动失败"。
        """
        logged = []
        with mock.patch.object(run_mod, "_log", lambda msg: logged.append(msg)),              mock.patch.object(run_mod.launcher, "_port_open",
                               return_value=True),              mock.patch.object(run_mod.browser_mod, "open_ui",
                               side_effect=OSError("no chrome")):
            run_mod._open_when_ready("http://127.0.0.1:8741/", 8741, object())

        self.assertTrue(any("打不开浏览器" in m for m in logged),
                        f"没有告诉用户原因，只有 {logged}")
