"""QQ 信道工具的离线测试。

真连 QQ 的验证不在这里（CI 无网、也没有 QQ 号），这里只锁住不需要网络的
那部分：配置写对没有、端口会不会撞、参数解析、以及 ``--help`` 能跑通。

实网结论与手工验证见 ``docs/QQ_TESTBED.md``。
"""

from __future__ import annotations

import io
import json
import re
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

    def test_bundle_covers_32bit(self) -> None:
        """32 位 Python 在 64 位 Windows 上很常见，它的 wheel 叫 win32。

        只打 win_amd64 的话，32 位解释器报 numpy 找不到 —— 实际踩过。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        self.assertIn("PLATFORMS", source)
        self.assertIn('"win32"', source)
        self.assertIn('"win_amd64"', source)

    def test_install_falls_back_without_uvicorn_standard(self) -> None:
        """uvicorn[standard] 的 C 扩展在很新的 Python 上常常还没编好。

        它一旦解析失败会带崩整条依赖链，所以要用纯 Python 的 uvicorn。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        self.assertIn("plain", source)
        self.assertIn('"uvicorn>=0.27"', source)

    def test_scripts_do_not_assume_python_on_path(self) -> None:
        """``python`` 不一定在 PATH（只装 py 启动器、或没勾 Add to PATH）。

        实测踩过：install.ps1 用 py 能跑，probe.ps1 硬编码 python 就
        ``CommandNotFoundException``。所有脚本都要自己找。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        for name, body in re.findall(r'(\w+_PS1) = r"""(.*?)"""', source, re.S):
            hard = [ln for ln in body.splitlines()
                    if re.search(r"&\s*python", ln)]
            self.assertFalse(hard, f"{name} 直接调用了 python：{hard}")
            if "markovcryptor" in body or "pip install" in body:
                self.assertIn("Get-Command py", body,
                              f"{name} 没做 py 降级查找")

    def test_node_is_started_detached(self) -> None:
        """node 必须脱离控制台。

        实测踩过：``Start-Process -NoNewWindow`` 让 node 继承调用方的控制台，
        调用链 start.ps1 -> run-snowluma.ps1 会一直等 node 退出，表现就是
        "脚本卡住"——服务其实已经起来了。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        m = re.search(r'RUN_SNOWLUMA_PS1 = r"""(.*?)"""', source, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        # 只看可执行行：注释里提到 -NoNewWindow 是在解释为什么不用它。
        # 只看可执行行：注释里提到 -NoNewWindow 是在解释为什么不用它。
        code = [ln for ln in body.splitlines()
                if not ln.lstrip().startswith("#")]
        joined = chr(10).join(code)
        self.assertNotIn("-NoNewWindow", joined,
                         "node 不能继承控制台，否则调用方会一直等")
        self.assertIn("-WindowStyle Hidden", body)
        self.assertIn("Start-Process", body)

    def test_one_click_entry_passes_autonomous_env(self) -> None:
        """免开网页的三件套必须由 run-snowluma 设好。

        对应 SnowLuma 源码里的 SNOWLUMA_ACCEPT_EULA / _PRIVACY /
        HOOK_AUTOLOAD / WEBUI_BOOTSTRAP_PASSWORD。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        m = re.search(r'RUN_SNOWLUMA_PS1 = r"""(.*?)"""', source, re.S)
        body = m.group(1)
        for name in ("SNOWLUMA_HOOK_AUTOLOAD",
                     "SNOWLUMA_ACCEPT_EULA",
                     "SNOWLUMA_ACCEPT_PRIVACY",
                     "SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD"):
            self.assertIn(name, body, f"缺 {name}，首次启动要开网页手点")
        # 协议同意必须两个都设，只设一个会被 SnowLuma 当 partial 忽略。
        self.assertIn('SNOWLUMA_ACCEPT_EULA = "1"', body)
        self.assertIn('SNOWLUMA_ACCEPT_PRIVACY = "1"', body)

    def test_bundle_ships_one_click_entry(self) -> None:
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        self.assertIn("START_PS1", source)
        self.assertIn("START_BAT", source)
        self.assertIn('("start.bat", START_BAT)', source)
        self.assertIn('("start.ps1", START_PS1)', source)

    def test_probe_reads_port_and_token_from_config(self) -> None:
        """SnowLuma 首次启动会自己生成随机端口和 token，手抄必错。"""
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        m = re.search(r'PROBE_PS1 = r"""(.*?)"""', source, re.S)
        body = m.group(1)
        self.assertIn("onebot_$Uin.json", body)
        self.assertIn("accessToken", body)
        self.assertIn("wsServers", body)

    def test_param_block_starts_the_script(self) -> None:
        """``param()`` 前只能是注释。

        实测踩过：在它前面插一条 ``[Console]::OutputEncoding = ...``，
        PowerShell 会报
        ``The assignment expression is not valid``
        并指到 param 里面的每个参数 —— 看着像参数写错了，其实整块没被
        当 param 解析。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        scripts = re.findall(r'(\w+_PS1) = r"""(.*?)"""', source, re.S)
        self.assertTrue(scripts, "没找到 ps1 模板")
        checked = 0
        for name, body in scripts:
            lines = [ln.strip() for ln in body.splitlines()]
            # 只认顶格的 param( —— 缩进或写在同一行都说明模板结构不对。
            if "param(" not in lines:
                continue
            self.assertEqual(body.splitlines()[lines.index("param(")].strip(),
                             "param(", f"{name}: param( 必须独占一行且顶格")
            checked += 1
            head = lines[:lines.index("param(")]
            for line in head:
                self.assertTrue(
                    line == "" or line.startswith("#"),
                    f"{name}: param() 前有可执行语句 {line!r}；"
                    "PowerShell 要求 param 之前只能是注释")
        self.assertGreaterEqual(checked, 2, "应至少检查到 probe 和 run-snowluma")

    def test_generated_wheels_cover_declared_targets(self) -> None:
        """真打一个包（不含 SnowLuma），检查每个组合都有 numpy 和 pillow。"""
        import re

        targets = re.findall(r'"3\.\d+"',
                            (TOOLS / "make_bundle.py").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            subprocess.run(
                [sys.executable, str(TOOLS / "make_bundle.py"), "--out", str(out)],
                cwd=str(ROOT), check=True, capture_output=True)
            wheels = [p.name for p in (out.glob("qqtest-*") if False else out.rglob("*.whl"))]
            self.assertTrue(wheels, "没下到任何 wheel")
            for version in set(targets):
                tag = "cp" + version.strip('"').replace(".", "")
                for arch in ("win_amd64", "win32"):
                    for pkg in ("numpy", "pillow"):
                        hit = [w for w in wheels
                               if w.startswith(pkg) and f"-{tag}-{tag}-{arch}" in w]
                        self.assertTrue(hit, f"{pkg} 缺 {tag}-{arch}")

    def test_ps1_need_bom_and_crlf(self) -> None:
        """Win10 自带的 PowerShell 5.1 按 ANSI 代码页读无 BOM 的 .ps1。

        UTF-8 中文在 GBK 下解码时，`号` 的尾字节会吞掉行尾 CRLF，把
        `param(` 拼进上一行注释 —— 脚本报「缺少实际参数」。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        self.assertIn("UTF-8 BOM", source)

    def test_bat_entry_points_exist(self) -> None:
        """Win10 默认 ExecutionPolicy=Restricted，.ps1 双击跑不了。

        给 .bat 入口，里面自己带 -ExecutionPolicy Bypass。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        for name in ("install.bat", "run-snowluma.bat", "probe.bat"):
            self.assertIn(name, source)
        self.assertIn("-ExecutionPolicy Bypass", source)

    def test_generated_scripts_have_right_encoding(self) -> None:
        """实际打一个包，逐项检查编码。

        - .ps1：UTF-8 BOM + CRLF，否则 PS 5.1 按 GBK 读会把行尾吃掉
        - .bat：纯 ASCII + CRLF，cmd 走 OEM 代码页
        """
        bom = bytes([0xEF, 0xBB, 0xBF])
        crlf = bytes([0x0D, 0x0A])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            subprocess.run(
                [sys.executable, str(TOOLS / "make_bundle.py"), "--out", str(out)],
                cwd=str(ROOT), check=True, capture_output=True)
            ps1 = list(out.rglob("*.ps1"))
            bat = list(out.rglob("*.bat"))
            self.assertGreaterEqual(len(ps1), 4, "应有 install/run/probe/start 四个 .ps1")
            self.assertGreaterEqual(len(bat), 4, "应有 install/run/probe/start 四个 .bat")
            for script in ps1:
                raw = script.read_bytes()
                self.assertTrue(raw.startswith(bom), f"{script.name} 缺 UTF-8 BOM")
                self.assertIn(crlf, raw, f"{script.name} 不是 CRLF")
            for script in bat:
                raw = script.read_bytes()
                raw.decode("ascii")          # cmd 走 OEM 代码页，只能放 ASCII
                self.assertIn(crlf, raw, f"{script.name} 不是 CRLF")

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
