"""QQ 信道工具的离线测试。

真连 QQ 的验证不在这里（CI 无网、也没有 QQ 号），这里只锁住不需要网络的
那部分：配置写对没有、端口会不会撞、参数解析、以及 ``--help`` 能跑通。

实网结论与手工验证见 ``docs/QQ_TESTBED.md``。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT))

import qq_setup  # noqa: E402
from markovcryptor import onebot_config  # noqa: E402


def _fake_snowluma(root: Path) -> Path:
    """最小可用的 SnowLuma 目录骨架：有个 index.mjs 和 config/ 就够。"""
    (root / "config").mkdir(parents=True)
    (root / "index.mjs").write_text("// stub\n", encoding="utf-8")
    return root


class SetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _fake_snowluma(Path(self._tmp.name) / "snowluma")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, *args: str) -> int:
        with redirect_stdout(io.StringIO()):     # 工具会打印提示，测试里不需要
            return qq_setup.main(["init", "--snowluma", str(self.root), *args])

    def _config(self, uin: str) -> dict:
        path = self.root / "config" / f"onebot_{uin}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_each_account_gets_its_own_ports(self) -> None:
        """两个号端口必须不同 —— 相同会让第二个号 EADDRINUSE 起不来。"""
        self.assertEqual(self._write("--a-uin", "111", "--b-uin", "222"), 0)
        left = self._config("111")["networks"]["wsServers"][0]
        right = self._config("222")["networks"]["wsServers"][0]
        self.assertNotEqual(left["port"], right["port"])
        self.assertNotEqual(
            self._config("111")["networks"]["httpServers"][0]["port"],
            self._config("222")["networks"]["httpServers"][0]["port"])

    def test_self_messages_are_not_reported(self) -> None:
        """自己发的消息不回显，否则终端会自己跟自己握手。"""
        self._write("--a-uin", "111")
        for network in ("wsServers", "httpServers"):
            self.assertFalse(self._config("111")["networks"][network][0]["reportSelfMessage"])

    def test_message_format_is_array(self) -> None:
        """图片段要带结构，必须是 array 而不是 string。"""
        self._write("--a-uin", "111")
        self.assertEqual(
            self._config("111")["networks"]["wsServers"][0]["messageFormat"], "array")

    def test_token_is_per_account(self) -> None:
        self._write("--a-uin", "111", "--b-uin", "222")
        left = self._config("111")["networks"]["wsServers"][0]["accessToken"]
        right = self._config("222")["networks"]["wsServers"][0]["accessToken"]
        self.assertTrue(left and right)
        self.assertNotEqual(left, right)

    def test_existing_config_is_preserved_unless_forced(self) -> None:
        """SnowLuma 可能在配置里补字段，重跑 init 不该抹掉。"""
        self._write("--a-uin", "111")
        path = self.root / "config" / "onebot_111.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["marker"] = "keep-me"
        path.write_text(json.dumps(data), encoding="utf-8")

        self._write("--a-uin", "111")
        self.assertEqual(self._config("111")["marker"], "keep-me")

        self._write("--a-uin", "111", "--force")
        self.assertNotIn("marker", self._config("111"))

    def test_refuses_directory_without_config(self) -> None:
        """指错目录时要说清是"没解压"，而不是抛个无关异常。"""
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                qq_setup.main(["init", "--snowluma", str(empty), "--a-uin", "111"])
        message = str(caught.exception.code)
        self.assertTrue("index.mjs" in message or "config" in message, message)

    def test_refuses_when_config_dir_is_missing(self) -> None:
        """有 index.mjs 但没有 config/ 时要报 config，不能默默写失败。"""
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        (bare / "index.mjs").write_text("// stub\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                qq_setup.main(["init", "--snowluma", str(bare), "--a-uin", "111"])
        self.assertIn("config", str(caught.exception.code))


class CommandLineTest(unittest.TestCase):
    """工具必须能当脚本跑，且不因为缺依赖在 import 期就炸。"""

    def test_help_works(self) -> None:
        for name in ("qq_setup.py", "qq_probe.py", "fetch_snowluma.py"):
            with self.subTest(tool=name):
                result = subprocess.run(
                    [sys.executable, str(TOOLS / name), "--help"],
                    capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_needs_two_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = _fake_snowluma(Path(raw) / "snowluma")
            with self.assertRaises(SystemExit) as caught:
                qq_setup.main(["probe", "--snowluma", str(root)])
            self.assertIn("两个账号", str(caught.exception.code))


class OneBotConfigTest(unittest.TestCase):
    """``markovcryptor.onebot_config`` 是 CLI 与 tools 的共同真源。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _fake_snowluma(Path(self._tmp.name) / "sl")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_token_is_derived_from_uin(self) -> None:
        settings = onebot_config.OneBotSettings(uin="123")
        self.assertEqual(settings.token, "mkx-123")
        self.assertEqual(settings.ws_url, "ws://127.0.0.1:9711/")

    def test_round_trip_through_file(self) -> None:
        """写进去再读回来要一致 —— tools 靠这个推 probe 的连接参数。"""
        settings = onebot_config.OneBotSettings(uin="123", ws_port=9721,
                                                http_port=9722, host="0.0.0.0")
        path, wrote = onebot_config.write_config(self.root, settings)
        self.assertTrue(wrote)
        self.assertEqual(onebot_config.read_settings(path), settings)

    def test_discover_sorts_by_uin(self) -> None:
        for uin in ("222", "111"):
            onebot_config.write_config(
                self.root, onebot_config.OneBotSettings(uin=uin))
        self.assertEqual(list(onebot_config.discover(self.root)), ["111", "222"])

    def test_read_settings_tolerates_garbage(self) -> None:
        path = self.root / "config" / "onebot_bad.json"
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(onebot_config.read_settings(path))

    def test_cli_queue_init_writes_config(self) -> None:
        """装机后没有 tools/ 目录，所以 qq-init 必须在 CLI 里。"""
        result = subprocess.run(
            [sys.executable, "-m", "markovcryptor.cli", "qq-init",
             "--snowluma", str(self.root), "--uin", "777",
             "--ws-port", "9731", "--http-port", "9732"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = onebot_config.read_settings(
            self.root / "config" / "onebot_777.json")
        self.assertEqual(settings.ws_port, 9731)
        self.assertEqual(settings.http_port, 9732)

    def test_cli_queue_init_reports_bad_directory(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "markovcryptor.cli", "qq-init",
             "--snowluma", str(self.root / "nope"), "--uin", "1"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())


class PackagingTest(unittest.TestCase):
    """打包时踩过的坑，锁住免得重新出现。"""

    def test_web_ui_ships_with_the_package(self) -> None:
        """``webui/index.html`` 必须进 wheel，否则装完打开 ``/`` 是 404。

        这条曾真实发生过：没配 ``package-data`` 时 wheel 里没有这个文件。
        """
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.setuptools.package-data]", pyproject)
        self.assertIn("webui/*.html", pyproject)

    def test_web_extra_includes_websockets(self) -> None:
        """OneBot 客户端要 websockets，它必须跟 [web] 一起装。"""
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        # 不能按 "[" 切：uvicorn[standard] 自己就带一层方括号。
        section = pyproject.split("\nweb = [", 1)[1]
        web_extra = section.split("\n]", 1)[0]
        self.assertIn("websockets", web_extra)

    def test_bundle_covers_multiple_python_versions(self) -> None:
        """只打 cp311 的 wheel 装到 3.12 上会 No matching distribution。"""
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        self.assertIn("PYTHON_TARGETS", source)
        for version in ("3.11", "3.12", "3.13"):
            self.assertIn(f'"{version}"', source)

    def test_bundle_installs_web_extra(self) -> None:
        """install.ps1 若只装 markovcryptor，guest 上会没有 fastapi。"""
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        self.assertIn('markovcryptor[web]', source)


class OneBotWebSocketTest(unittest.TestCase):
    """``websockets`` 14 起删了 legacy 实现，参数名也改了。

    项目原来直接用 ``extra_headers``，装上新版 websockets 就会
    ``TypeError: create_connection() got an unexpected keyword argument``。
    """

    def test_picks_the_parameter_name_the_installed_version_accepts(self) -> None:
        import inspect

        import websockets

        from markovcryptor.onebot_ws import _header_kwarg

        name = _header_kwarg()
        params = inspect.signature(websockets.connect).parameters
        self.assertIn(name, params)
        self.assertIn(name, ("extra_headers", "additional_headers"))

    def test_auth_header_omits_empty_token(self) -> None:
        from markovcryptor.onebot_ws import auth_header

        self.assertEqual(auth_header(""), {})
        self.assertEqual(auth_header("mkx-1"), {"Authorization": "Bearer mkx-1"})

    def test_connect_without_token_passes_no_header(self) -> None:
        """没 token 时不应把空的 Authorization 发出去。"""
        from markovcryptor import onebot_ws

        captured = {}

        class Fake:
            @staticmethod
            def connect(url, **kwargs):
                captured.update(kwargs)
                return object()

        real = sys.modules.get("websockets")
        sys.modules["websockets"] = Fake
        try:
            onebot_ws.connect("ws://127.0.0.1:1/", "")
            self.assertEqual(captured, {})
            onebot_ws.connect("ws://127.0.0.1:1/", "mkx-7")
            self.assertIn("Authorization", str(captured))
        finally:
            if real is None:
                sys.modules.pop("websockets", None)
            else:
                sys.modules["websockets"] = real

    def test_no_module_uses_raw_extra_headers(self) -> None:
        """再有人手写 extra_headers，就会在新版 websockets 上崩掉。"""
        for path in (ROOT / "tools").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "websockets" not in text:
                continue
            self.assertNotIn(
                "extra_headers=", text,
                f"{path.name} 应该用 markovcryptor.onebot_ws.connect")


class DocsTest(unittest.TestCase):
    def test_testbed_doc_exists_and_is_indexed(self) -> None:
        doc = ROOT / "docs" / "QQ_TESTBED.md"
        self.assertTrue(doc.is_file())
        self.assertIn("QQ_TESTBED.md", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_vendor_is_ignored(self) -> None:
        """SnowLuma 不进版本库 —— 换平台要换包，且它有自己的 EULA。"""
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("vendor/", [line.strip() for line in ignored])


if __name__ == "__main__":
    unittest.main()
