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

import qq_setup  # noqa: E402


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
