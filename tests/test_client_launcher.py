"""Config, browser detection and launcher discovery.

These are the parts that decide whether a double-click on a fresh machine gets
anywhere, so they get tested without touching the real user profile or launching
a browser.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from markovcryptor.client import browser, config, launcher


class ConfigRoundTripTest(unittest.TestCase):
    def test_save_then_load_keeps_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            cfg = config.ClientConfig(
                cover_folders=[Path("D:/Pics/a"), Path("D:/Pics/b")],
                max_queue=5, pick="sequential", psk="secret",
                onebot_url="ws://127.0.0.1:3001/", onebot_token="tok", port=8741)
            self.assertTrue(cfg.save(path))
            back = config.ClientConfig.load(path)
            self.assertEqual(back.cover_folders, [Path("D:/Pics/a"), Path("D:/Pics/b")])
            self.assertEqual(back.max_queue, 5)
            self.assertEqual(back.pick, "sequential")
            self.assertEqual(back.psk, "secret")
            self.assertEqual(back.onebot_url, "ws://127.0.0.1:3001/")
            self.assertEqual(back.port, 8741)

    def test_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.ClientConfig.load(Path(tmp) / "nope.json")
            self.assertEqual(cfg.cover_folders, [])
            self.assertEqual(cfg.max_queue, 8)
            self.assertEqual(cfg.pick, "random")

    def test_malformed_json_does_not_raise(self):
        """用户改坏了 JSON 不该让程序起不来 —— 他根本看不到语法错。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            path.write_text("{ this is not json", encoding="utf-8")
            cfg = config.ClientConfig.load(path)
            self.assertEqual(cfg.max_queue, 8)

    def test_unwritable_location_returns_false(self):
        """只读位置是正常情况（移动盘、锁定的共享盘），不该抛。"""
        cfg = config.ClientConfig()
        with mock.patch.object(Path, "write_text",
                               side_effect=OSError("read-only")):
            self.assertFalse(cfg.save(Path("X:/nowhere/client.json")))

    def test_env_override_changes_default_path(self):
        with mock.patch.dict(os.environ, {config.CONFIG_ENV: "D:/tmp/x.json"}):
            self.assertEqual(config.default_config_path(), Path("D:/tmp/x.json"))

    def test_bad_values_are_sanitised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            path.write_text(json.dumps({
                "queue": {"max_items": 0, "pick": "wat"},
                "port": "not-a-number",
            }), encoding="utf-8")
            cfg = config.ClientConfig.load(path)
            self.assertEqual(cfg.max_queue, 1, "上限至少 1")
            self.assertEqual(cfg.pick, "random")
            self.assertEqual(cfg.port, 0)


class UsableFoldersTest(unittest.TestCase):
    def test_folder_with_images_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            Image.new("RGB", (200, 200)).save(folder / "a.png", "PNG")
            cfg = config.ClientConfig(cover_folders=[folder])
            self.assertEqual(cfg.usable_cover_folders(), [folder])

    def test_empty_folder_is_not_usable(self):
        """「没配」和「配了但里面没图」要能区分，给用户的提示不一样。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "readme.txt").write_text("x")
            cfg = config.ClientConfig(cover_folders=[folder])
            self.assertEqual(cfg.usable_cover_folders(), [])

    def test_missing_folder_is_skipped_not_fatal(self):
        cfg = config.ClientConfig(cover_folders=[Path("X:/does/not/exist")])
        self.assertEqual(cfg.usable_cover_folders(), [])


class ExeParsingTest(unittest.TestCase):
    def test_quoted_command(self):
        got = browser._exe_from_command(
            r'"C:\Program Files\Google\Chrome\Application\chrome.exe" -- "%1"')
        self.assertEqual(got, r"C:\Program Files\Google\Chrome\Application\chrome.exe")

    def test_unquoted_command(self):
        got = browser._exe_from_command(r"C:\x\msedge.exe --single-argument %1")
        self.assertEqual(got, r"C:\x\msedge.exe")

    def test_empty_command_gives_none(self):
        """猜错会启动错的程序，所以拿不准就返回 None。"""
        self.assertIsNone(browser._exe_from_command(""))

    def test_chromium_detection(self):
        self.assertTrue(browser._is_chromium(r"C:\a\chrome.exe"))
        self.assertTrue(browser._is_chromium(r"C:\a\msedge.exe"))
        self.assertFalse(browser._is_chromium(r"C:\a\firefox.exe"))


class OpenUiTest(unittest.TestCase):
    def test_chromium_uses_app_mode(self):
        with mock.patch.object(browser.subprocess, "Popen") as popen:
            label = browser.open_ui("http://127.0.0.1:8741/",
                                    browser=r"C:\x\chrome.exe")
        self.assertIn("--app", label)
        args = popen.call_args[0][0]
        self.assertTrue(any("--app=http://127.0.0.1:8741/" in a for a in args),
                        f"要用 --app 开无地址栏窗口，实际：{args}")

    def test_firefox_falls_back_to_a_tab(self):
        """Firefox 没有 --app，降级为普通窗口是可接受的（已写进文档）。"""
        with mock.patch.object(browser.subprocess, "Popen") as popen, \
             mock.patch.object(browser.os, "startfile", create=True) as startfile:
            label = browser.open_ui("http://127.0.0.1:8741/",
                                    browser=r"C:\x\firefox.exe")
        self.assertIn("标签页", label)
        startfile.assert_called_once()

    def test_no_browser_reports_none(self):
        with mock.patch.object(browser, "find_default_browser", return_value=None):
            self.assertEqual(browser.open_ui("http://x/"), "none")

    def test_app_mode_failure_falls_back_to_tab(self):
        """--app 失败不能什么都不显示。"""
        with mock.patch.object(browser.subprocess, "Popen",
                               side_effect=OSError("nope")), \
             mock.patch.object(browser.os, "startfile", create=True) as startfile:
            label = browser.open_ui("http://x/", browser=r"C:\x\chrome.exe")
        self.assertIn("标签页", label)
        startfile.assert_called_once()


def _write_snowluma_config(root: Path, uin="1741473279", port=3001,
                           token="tok", host="127.0.0.1"):
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / f"onebot_{uin}.json").write_text(json.dumps({
        "networks": {"wsServers": [
            {"host": host, "port": port, "accessToken": token}]}
    }), encoding="utf-8")
    return cfg


class DiscoveryTest(unittest.TestCase):
    def test_account_comes_from_the_filename(self):
        """「不用输 QQ 号」的全部依据：文件名就是账号。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snowluma_config(root, uin="1741473279", port=3001)
            endpoint = launcher.discover_endpoint(root)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.uin, "1741473279")
            self.assertEqual(endpoint.url, "ws://127.0.0.1:3001/")
            self.assertEqual(endpoint.token, "tok")

    def test_no_config_returns_none(self):
        """登录前没有配置是正常状态，不是错误。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(launcher.discover_endpoint(Path(tmp)))

    def test_wildcard_host_is_rewritten_to_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snowluma_config(root, host="0.0.0.0")
            self.assertEqual(launcher.discover_endpoint(root).url,
                             "ws://127.0.0.1:3001/")

    def test_broken_config_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config"
            cfg.mkdir()
            (cfg / "onebot_111.json").write_text("{broken", encoding="utf-8")
            _write_snowluma_config(root, uin="222", port=3010)
            endpoint = launcher.discover_endpoint(root)
            self.assertEqual(endpoint.uin, "222", "坏的那份跳过，好的照用")

    def test_spec_round_trip(self):
        endpoint = launcher.Endpoint(uin="1", url="ws://h:1/", token="t")
        self.assertEqual(endpoint.spec, "1=ws://h:1/;t")
        endpoint_no_token = launcher.Endpoint(uin="1", url="ws://h:1/")
        self.assertEqual(endpoint_no_token.spec, "1=ws://h:1/")


class WaitForEndpointTest(unittest.TestCase):
    def test_times_out_with_actionable_hint(self):
        """超时提示必须说"去登录 QQ"，不能只说"超时"。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(launcher.LauncherError) as ctx:
                launcher.wait_for_endpoint(Path(tmp), timeout=0.3)
            self.assertIn("QQ", ctx.exception.hint)

    def test_returns_as_soon_as_config_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snowluma_config(root)
            endpoint = launcher.wait_for_endpoint(root, timeout=2.0)
            self.assertEqual(endpoint.uin, "1741473279")


class StartSnowlumaTest(unittest.TestCase):
    def test_missing_index_mjs_says_the_package_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(launcher.LauncherError) as ctx:
                launcher.start_snowluma(Path(tmp))
            self.assertIn("SnowLuma", str(ctx.exception))

    def test_missing_node_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.mjs").write_text("// stub")
            with mock.patch.object(launcher, "_find_node", return_value=None):
                with self.assertRaises(launcher.LauncherError) as ctx:
                    launcher.start_snowluma(root)
                self.assertIn("node", str(ctx.exception))

    def test_bundled_node_is_preferred_over_path(self):
        """机器上的老 node 不能用 —— 包自带的那份优先。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "snowluma"
            root.mkdir()
            bundled = Path(tmp) / "node" / "node.exe"
            bundled.parent.mkdir()
            bundled.write_text("")
            with mock.patch.object(launcher.shutil, "which",
                                   return_value=r"C:\old\node.exe"):
                self.assertEqual(launcher._find_node(root), str(bundled))


class PortTest(unittest.TestCase):
    def test_port_extracted_from_url(self):
        self.assertEqual(launcher._port_of("ws://127.0.0.1:3001/"), 3001)

    def test_garbage_url_gives_zero(self):
        self.assertEqual(launcher._port_of("nonsense"), 0)

    def test_wait_for_onebot_false_on_closed_port(self):
        self.assertFalse(launcher.wait_for_onebot("ws://127.0.0.1:1/", timeout=0.4))


if __name__ == "__main__":
    unittest.main()


class PackageDataTest(unittest.TestCase):
    """界面文件必须真的进 wheel。

    实际撞上过：pyproject 的 package-data 只列了 `webui/*.html`，漏了
    `client/webui/*.html`。装出来的包界面一片空白 —— 看起来像 JS 写错了，
    查半天才发现是打包漏了文件。这种错误只能靠断言 wheel 内容来防。
    """

    def test_pyproject_ships_both_uis(self):
        root = Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("webui/*.html", text)
        self.assertIn("client/webui/*.html", text,
                      "漏了客户端界面就会白屏")

    def test_client_webui_file_exists_in_source(self):
        root = Path(__file__).resolve().parent.parent
        ui = root / "markovcryptor" / "client" / "webui" / "index.html"
        self.assertTrue(ui.exists(), "客户端界面文件不见了")
        body = ui.read_text(encoding="utf-8")
        self.assertIn("display: none !important", body,
                      "hidden 必须配 !important，否则自带 display 会压掉它")


class WebuiSyntaxTest(unittest.TestCase):
    """界面 JS 必须语法正确。

    实际踩过：用脚本改 index.html 时反斜杠被多重转义，写出
    `join("` + 真换行，整个 <script> 直接 SyntaxError —— 页面一片空白，
    没有任何错误提示（浏览器只在控制台里说）。这类错误只能靠真正解析
    一次 JS 来防。
    """

    def _script(self) -> str:
        import re
        root = Path(__file__).resolve().parent.parent
        html = (root / "markovcryptor" / "client" / "webui" / "index.html")
        body = html.read_text(encoding="utf-8")
        match = re.search(r"<script>(.*?)</script>", body, re.S)
        self.assertIsNotNone(match, "找不到 <script> 块")
        return match.group(1)

    def test_no_raw_newline_inside_string_literal(self):
        """字符串字面量里不能出现未转义的换行。"""
        script = self._script()
        for lineno, line in enumerate(script.splitlines(), 1):
            stripped = line.rstrip()
            # 以 .join(" 或 = " 结尾且引号没闭合的行，就是被换行劈开的字符串。
            if stripped.endswith('.join("') or stripped.endswith('= "'):
                self.fail(f"第 {lineno} 行字符串没闭合：{line!r}")

    def test_no_quadruple_backslash(self):
        """四个连续反斜杠在 JS 里是无效转义，多半是脚本写入时多加了一层。"""
        script = self._script()
        self.assertNotIn(chr(92) * 4, script,
                         "出现四个连续反斜杠，检查是不是转义时多套了一层")

    def test_script_parses_as_javascript(self):
        """真的用 node 解析一次 —— 这是唯一可靠的验证。"""
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node，跳过 JS 语法检查")
        script = self._script()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ui.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)],
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0,
                         f"JS 语法错误：{result.stderr[-600:]}")


class BundleDependencyTest(unittest.TestCase):
    """打包自检必须在隔离模式下跑，且要真的检查依赖文件。

    实际事故：打出来的包在虚拟机上启动报
    ``ModuleNotFoundError: No module named 'cryptography'``。
    原因是自检时包内 Python 从**打包机的用户级 site-packages** 借到了
    cryptography —— 打包机上当然有，虚拟机上没有。

    这类 bug 最恶劣的地方：自检"通过"了，包看起来是好的，直到换一台
    干净机器才炸。
    """

    def _source(self) -> str:
        root = Path(__file__).resolve().parent.parent
        return (root / "tools" / "make_client_bundle.py").read_text(encoding="utf-8")

    def test_verify_runs_isolated(self):
        """自检必须用 -I，否则会读到宿主机的包。"""
        source = self._source()
        self.assertIn('"-I"', source,
                      "自检没有用 -I 隔离，会借用宿主机的依赖")

    def test_verify_scrubs_python_env(self):
        """PYTHONPATH / PYTHONHOME 之类的泄漏要清掉。"""
        source = self._source()
        self.assertIn("_clean_env", source)
        self.assertIn("PYTHONPATH", source)
        self.assertIn("PYTHONNOUSERSITE", source)

    def test_verify_imports_the_real_dependencies(self):
        """自检要真的 import 依赖，不能只 import markovcryptor。"""
        source = self._source()
        for name in ("cryptography", "numpy", "reedsolo", "fastapi",
                     "uvicorn", "websockets"):
            self.assertIn(name, source, f"自检没验证 {name}")

    def test_verify_checks_files_not_just_imports(self):
        """还要查文件真的在包内 —— import 成功可能来自宿主机。"""
        source = self._source()
        self.assertIn("_verify_site_packages", source)
        self.assertIn("Lib\" / \"site-packages\"", source)

    def test_single_module_packages_are_recognised(self):
        """reedsolo 是单文件模块（reedsolo.py），只查目录会误报缺失。"""
        source = self._source()
        self.assertIn('.py', source.split("def present")[1][:400],
                      "present() 要考虑单文件模块")


class BundlePrivacyTest(unittest.TestCase):
    """交付包里不能有账号相关的任何东西。

    实际踩到：发给用户的包里有
      snowluma/config/onebot_1741473279.json  -> 账号 + OneBot 明文访问令牌
      snowluma/config/webui.json              -> WebUI 密码哈希和盐
      snowluma/data/1741473279/*.db           -> 聊天记录、身份库
      snowluma/logs/*.log                     -> 说过的话

    这些是打包机上跑过真 QQ 留下的。包是给别人的，带出去就是泄漏，
    而且对方拿到也没用（不是他的账号）。
    """

    def _module(self):
        import importlib

        return importlib.import_module("tools.make_client_bundle")

    def _make_tree(self, root: Path) -> Path:
        sl = root / "snowluma"
        (sl / "config").mkdir(parents=True)
        (sl / "data" / "1741473279").mkdir(parents=True)
        (sl / "logs").mkdir(parents=True)
        (sl / "index.mjs").write_text("// code", encoding="utf-8")
        (sl / "config" / "onebot_1741473279.json").write_text(
            '{"accessToken": "secret"}', encoding="utf-8")
        (sl / "config" / "webui.json").write_text(
            '{"passwordHash": "deadbeef"}', encoding="utf-8")
        (sl / "config" / "runtime.json").write_text("{}", encoding="utf-8")
        (sl / "data" / "1741473279" / "messages.db").write_bytes(b"SQLite")
        (sl / "logs" / "snowluma-2026-10-23.log").write_text("hi", encoding="utf-8")
        return sl

    def test_scrub_removes_account_config_and_data(self):
        import tempfile

        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            sl = self._make_tree(Path(tmp))
            mod._scrub_personal_state(sl)

            self.assertEqual(list((sl / "config").iterdir()), [],
                             "config 目录要清空（但要保留目录本身）")
            self.assertTrue((sl / "config").is_dir(),
                            "SnowLuma 期望 config/ 存在")
            self.assertFalse((sl / "data").exists(), "聊天数据库必须删掉")
            self.assertFalse((sl / "logs").exists(), "日志必须删掉")
            self.assertTrue((sl / "index.mjs").exists(), "程序文件要保留")

    def test_verify_rejects_leftover_account_files(self):
        import tempfile

        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_tree(root)
            with self.assertRaises(SystemExit) as ctx:
                mod._verify_no_personal_state(root)
            message = str(ctx.exception)
            self.assertIn("账号", message)
            self.assertIn("onebot_", message)

    def test_verify_passes_on_a_clean_tree(self):
        import tempfile

        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sl = self._make_tree(root)
            mod._scrub_personal_state(sl)
            mod._verify_no_personal_state(root)     # 不该抛

    def test_verify_ignores_bundled_packages(self):
        """site-packages 里可能有叫 config.json 的，那不是我们的账号信息。"""
        import tempfile

        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "snowluma" / "config").mkdir(parents=True)
            lib = root / "python" / "Lib" / "site-packages" / "somepkg"
            lib.mkdir(parents=True)
            (lib / "config.json").write_text("{}", encoding="utf-8")
            (lib / "data").mkdir()
            (lib / "data" / "x.db").write_bytes(b"x")
            mod._verify_no_personal_state(root)     # 不该抛

    def test_verify_catches_a_stray_db_under_snowluma(self):
        import tempfile

        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "snowluma").mkdir()
            (root / "snowluma" / "snowluma_identity.db").write_bytes(b"x")
            with self.assertRaises(SystemExit):
                mod._verify_no_personal_state(root)

    def test_build_pipeline_actually_scrubs_and_verifies(self):
        """光有函数不够 —— 构建流程必须真的调用它们。

        实际教训：先写了 _scrub_personal_state，但没挂进 build()，
        包里的账号文件照样发了出去。
        """
        import inspect

        mod = self._module()
        # 清理挂在 _copy_snowluma 里（它由 build 调用），最终校验在 build 尾部。
        chain = (inspect.getsource(mod.build)
                 + inspect.getsource(mod._copy_snowluma))
        self.assertIn("_scrub_personal_state", chain,
                      "构建流程没调用清理函数")
        self.assertIn("_verify_no_personal_state", inspect.getsource(mod.build),
                      "构建流程没做最终校验")

    def test_manifest_does_not_list_a_qq_number(self):
        """manifest 是纯包信息，不该出现具体账号。"""
        import re

        root = Path(__file__).resolve().parent.parent
        source = (root / "tools" / "make_client_bundle.py").read_text(
            encoding="utf-8")
        # 1741473279 是打包机的账号，绝不该出现在脚本里
        self.assertNotIn("1741473279", source)
        self.assertNotIn("3925697820", source)


class SplitterRecoveryTest(unittest.TestCase):
    """一侧被收起后必须还能拉回来。

    实际反馈：拖到一侧消失后，分隔条被 `hidden = true` 一起藏了，
    没有任何办法把那一侧拉回来 —— 只能重启程序（而且 ratio 存在
    localStorage 里，重启还是收起的）。
    """

    def _html(self) -> str:
        path = (Path(__file__).resolve().parent.parent
                / "markovcryptor" / "client" / "webui" / "index.html")
        return path.read_text(encoding="utf-8")

    def test_splitter_is_never_hidden(self):
        html = self._html()
        # 找 applySplit 里对 splitter 的处理
        start = html.index("function applySplit()")
        end = html.index("function initSplitter()")
        body = html[start:end]
        self.assertNotIn("splitter.hidden = true", body,
                         "收起一侧时不能把分隔条一起藏掉，否则拉不回来")
        self.assertNotIn("$(\"splitter\").hidden = true", body)

    def test_collapsed_splitter_gets_a_visible_handle(self):
        html = self._html()
        self.assertIn("splitter.classList.add(\"collapsed\")", html,
                      "收起态要有个明显的抓手")
        self.assertIn(".splitter.collapsed", html,
                      "收起态需要独立样式，否则和边框没区别")

    def test_ratio_range_allows_full_collapse(self):
        """ratio 允许到 0/1（代表收起），拖回来时离开极值即可恢复。"""
        html = self._html()
        self.assertIn("Math.min(1, Math.max(0, ratio))", html)
        self.assertNotIn("Math.min(0.95, Math.max(0.05, ratio))", html)

    def test_saved_ratio_accepts_extremes(self):
        html = self._html()
        self.assertIn("saved >= 0 && saved <= 1", html,
                      "存下的收起状态要能恢复，不能因为越界被丢弃")


class ScrollbarStyleTest(unittest.TestCase):
    """默认滚动条在深色界面里是一块浅灰方条，还占 17px 让布局跳动。"""

    def _html(self) -> str:
        path = (Path(__file__).resolve().parent.parent
                / "markovcryptor" / "client" / "webui" / "index.html")
        return path.read_text(encoding="utf-8")

    def test_webkit_scrollbar_is_styled(self):
        html = self._html()
        self.assertIn("::-webkit-scrollbar", html,
                      "Chromium --app 模式用的就是它")
        self.assertIn("::-webkit-scrollbar-thumb", html)

    def test_firefox_scrollbar_is_styled(self):
        html = self._html()
        self.assertIn("scrollbar-width: thin", html,
                      "降级到 Firefox 普通窗口时也要生效")
        self.assertIn("scrollbar-color", html)

    def test_textarea_has_a_max_height(self):
        html = self._html()
        start = html.index(".composer textarea {")
        end = html.index("}", start)
        body = html[start:end]
        self.assertIn("max-height", body,
                      "输入框内容多了要自己滚，不能把布局顶高")
        self.assertIn("overflow-y: auto", body)


class TextareaAutosizeTest(unittest.TestCase):
    """输入框要跟着内容长高。

    实际反馈：写死 height:46px，只够一行半，写两句话就要靠滚动。
    """

    def _html(self) -> str:
        path = (Path(__file__).resolve().parent.parent
                / "markovcryptor" / "client" / "webui" / "index.html")
        return path.read_text(encoding="utf-8")

    def test_no_fixed_height(self):
        html = self._html()
        start = html.index(".composer textarea {")
        end = html.index("}", start)
        body = html[start:end]
        # 只禁"固定 height"（会覆盖 autosize）；min-height: 46px 是合法的下限。
        self.assertNotRegex(body, r"(?<!min-)(?<!max-)height: 46px",
                            "固定高度只够一行半，必须改成 min-height + autosize")
        self.assertIn("min-height", body)
        self.assertIn("max-height", body)

    def test_autosize_function_exists_and_is_wired(self):
        html = self._html()
        self.assertIn("function autosize(", html)
        self.assertIn("initAutosize()", html,
                      "定义了函数还必须真的调用，否则等于没写")
        # 两个输入框都要接上
        self.assertIn('["input", "covertInput"]', html)

    def test_autosize_resets_before_measuring(self):
        """不归零就量 scrollHeight，元素会卡在历史最大值只增不减。"""
        html = self._html()
        start = html.index("function autosize(")
        end = html.index("function initAutosize(", start)
        body = html[start:end]
        self.assertIn("el.style.height = AUTOSIZE_MIN", body)

    def test_textarea_cleared_after_send(self):
        """发送后要收回原高度，否则留一大块空白。"""
        html = self._html()
        self.assertIn("function clearInput(", html,
                      "清空+收回要统一成一个入口，避免每处都记得调 autosize")
        self.assertIn('clearInput("input")', html)
        self.assertIn('clearInput("covertInput")', html)
        # 清空必须真的把值置空
        start = html.index("function clearInput(")
        end = html.index("}", start)
        self.assertIn('el.value = ""', html[start:end])
