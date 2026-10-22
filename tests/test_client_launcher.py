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
