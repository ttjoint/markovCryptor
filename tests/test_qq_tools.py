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

import pytest
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


class ChatWindowTest(unittest.TestCase):
    """终端界面必须让人看见"收到了什么"。

    联调时踩过：对端发来一张普通照片，隐写层按设计不出声（图里没有密文是
    最常见的情况），结果界面上什么都没有，看起来和"根本没收到"一模一样，
    于是白排查了一轮。
    """

    def _side(self):
        import qq_chat
        from markovcryptor.terminal.terminal import Inbound

        side = qq_chat.Side.__new__(qq_chat.Side)
        side.label = "me"
        side.lines = []
        side.say = staticmethod(lambda line: side.lines.append(line))
        return qq_chat, Inbound, side

    def test_plain_photo_is_shown(self) -> None:
        from PIL import Image

        qq_chat, Inbound, side = self._side()
        inbound = Inbound(
            sender="10002",
            chat_images=(Image.new("RGB", (512, 512)), Image.new("RGB", (300, 200))),
        )
        qq_chat.Side._on_inbound(side, inbound)
        shown = chr(10).join(side.lines)
        self.assertIn("图", shown, "普通照片必须显示出来")
        self.assertIn("512×512", shown)

    def test_empty_packet_still_says_something(self) -> None:
        """收到但没内容，也要说一句；否则两种情形无法区分。"""
        qq_chat, Inbound, side = self._side()
        qq_chat.Side._on_inbound(side, Inbound(sender="10002"))
        self.assertTrue(side.lines, "空包也必须留下一行输出")

    def test_covert_and_handshake_still_shown(self) -> None:
        qq_chat, Inbound, side = self._side()
        qq_chat.Side._on_inbound(side, Inbound(
            sender="10002", covert=("密文内容",), handshake="peer-key"))
        shown = chr(10).join(side.lines)
        self.assertIn("密文内容", shown)
        self.assertIn("peer-key", shown)


class UpgradeTest(unittest.TestCase):
    """新包必须真的换掉旧代码。

    踩过：wheel 版本号一直是 0.3.0，`pip install --upgrade` 看到"已装
    0.3.0"就跳过，于是 guest 上跑的还是第一次装的那份。症状是
    `chat.bat` 报 "unrecognized arguments: --onebot"，看着像脚本 bug。
    """

    def test_install_ps1_forces_the_bundled_wheel(self) -> None:
        from make_bundle import INSTALL_PS1

        self.assertIn("--force-reinstall", INSTALL_PS1,
                      "版本号不变，不加 --force-reinstall 就永远装不上新代码")
        self.assertIn("markovcryptor", INSTALL_PS1)

    def test_install_ps1_installs_markovcryptor_after_deps(self) -> None:
        """依赖先装、本包后装：本包的元数据依赖别让离线解析卡住。"""
        from make_bundle import INSTALL_PS1

        deps_at = INSTALL_PS1.index("fastapi uvicorn python-multipart websockets")
        api_at = INSTALL_PS1.index("--force-reinstall --no-deps markovcryptor")
        self.assertGreater(api_at, deps_at)

    def test_chat_refuses_to_run_an_old_library(self) -> None:
        """旧库里没有 --onebot；先查一句，别让 argparse 把话说得像参数写错了。"""
        from make_bundle import CHAT_PS1

        self.assertIn("--onebot", CHAT_PS1)
        self.assertIn("install.bat", CHAT_PS1,
                      "必须明确告诉用户去重跑 install.bat")
        self.assertIn("旧版", CHAT_PS1)


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

    def test_console_encoding_failure_is_ignored(self) -> None:
        """``[Console]::OutputEncoding`` 失败必须被吞掉。

        踩过的坑：cmd 里 ``chcp 65001`` 之后再设 OutputEncoding，PS 5.1
        会抛「句柄无效」。若此时 ``$ErrorActionPreference`` 已经是
        ``"Stop"``，脚本会在打印第一行之前静默退出 —— 屏幕上只剩 cmd 的
        ``Press any key to continue``，看着像 probe 什么都没做。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        scripts = re.findall(r'(\w+_PS1) = r"""(.*?)"""', source, re.S)
        self.assertTrue(scripts)
        for name, body in scripts:
            if "OutputEncoding" not in body:
                continue
            for line in body.splitlines():
                stripped = line.strip()
                # 注释里也会提到 OutputEncoding，只看真正执行的语句
                if stripped.startswith("#") or "OutputEncoding" not in stripped:
                    continue
                self.assertIn("try {", line,
                              f"{name}: OutputEncoding 必须包在 try/catch 里")
                self.assertIn("catch { }", line,
                              f"{name}: OutputEncoding 失败必须被忽略")
            # 不能在 OutputEncoding 之前就把 ErrorActionPreference 设成 Stop
            lines = [ln.strip() for ln in body.splitlines()]
            enc_at = next(i for i, ln in enumerate(lines) if "OutputEncoding" in ln)
            for earlier in lines[:enc_at]:
                if earlier.startswith("#"):
                    continue          # 注释里提到不算
                self.assertNotIn('$ErrorActionPreference = "Stop"', earlier,
                                 f"{name}: ErrorActionPreference=Stop 出现在 "
                                 "OutputEncoding 之前，设编码一失败就静默退出")

    def test_probe_reports_failures_in_plain_words(self) -> None:
        """probe 失败不能甩 traceback。

        用户看不懂 traceback，只会说「probe 无效」。每条失败路径都要
        翻成一句人话 + 一句怎么办。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        m = re.search(r'PROBE_PS1 = r"""(.*?)"""', source, re.S)
        body = m.group(1)
        # 三类最可能的失败都要有自己的判断
        self.assertIn("ConnectionRefusedError", body, "没区分「端口不通」")
        self.assertIn("401", body, "没区分「token 被拒」")
        self.assertIn("retcode", body, "没看 OneBot 的 retcode")
        # 失败出口必须带「怎么办」
        self.assertIn("怎么办", body)
        # 先说话再干活：任何情况下都该看到 probe 启动了
        head = body.split("$py = Get-Command py")[0]
        self.assertIn("probe 启动", head,
                      "probe 必须在做任何可能失败的事之前先打印一行，"
                      "否则静默退出时用户什么都看不到")

    def test_bat_templates_stay_ascii(self) -> None:
        """.bat 必须纯 ASCII —— cmd 按 OEM 代码页（中文机 936）解码。

        往 .bat 里写中文，cmd 下会变成乱码；打包时 ``encode("ascii")``
        也会直接抛 UnicodeEncodeError，整个打包挂掉。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        bats = re.findall(r'(\w+_BAT) = r"""(.*?)"""', source, re.S)
        self.assertTrue(bats, "没找到 .bat 模板")
        for name, body in bats:
            try:
                body.encode("ascii")
            except UnicodeEncodeError as error:
                bad = body[error.start:error.end]
                self.fail(f"{name} 含非 ASCII 字符 {bad!r}；"
                          ".bat 只能写英文，cmd 按 OEM 代码页解码")

    def test_bat_blocks_have_no_stray_parens(self) -> None:
        """``if (...)`` 块内的行不能出现未转义的括号。

        踩过的坑（probe.bat 一个字都不打印，只剩 cmd 的
        ``Press any key``）：用法提示写在块里，

            if "%~1"=="" (
              echo Optional: probe.bat PEER_QQ UIN  (pick config explicitly)
              exit /b 1
            )

        那个 ``)`` 会提前闭合整块 —— cmd 在解析阶段就把块切坏，
        后面的 ``powershell ...`` 根本不会执行。传了参数时更隐蔽：
        块被跳过，但解析已经出错，脚本直接退出，屏幕上什么都没有。
        所以：echo 里的括号一律写成 ``^)`` ``^(``。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        bats = re.findall(r'(\w+_BAT) = r"""(.*?)"""', source, re.S)
        self.assertTrue(bats)
        checked = 0
        for name, body in bats:
            in_block = False
            for number, line in enumerate(body.splitlines(), 1):
                stripped = line.strip()
                if stripped.upper().startswith("REM"):
                    continue
                code = stripped
                # 去掉转义对，剩下的括号才参与块结构
                while True:
                    new_code = code.replace("^(", "").replace("^)", "")
                    if new_code == code:
                        break
                    code = new_code
                if in_block:
                    # 块内只允许作为整个块边界出现的括号
                    inner = code
                    if inner.startswith(")"):
                        inner = inner[1:]
                    self.assertNotIn(")", inner,
                                     f"{name}:{number} if 块内出现未转义的 ')'："
                                     f"{stripped!r}；cmd 会提前结束这个块")
                    self.assertNotIn("(", inner,
                                     f"{name}:{number} if 块内出现未转义的 '('："
                                     f"{stripped!r}")
                    if code.endswith(")"):
                        in_block = False
                elif code.endswith("("):
                    in_block = True
                    checked += 1
        self.assertGreater(checked, 0, "没扫到任何多行 if 块，测试没意义")

    def test_probe_bat_says_something_on_failure(self) -> None:
        """``probe.bat`` 失败时不能只留一个 ``pause``。"""
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        m = re.search(r'PROBE_BAT = r"""(.*?)"""', source, re.S)
        body = m.group(1)
        self.assertIn("if errorlevel 1", body,
                      "probe.bat 失败后直接 pause，屏幕上看不出出过错")

    @pytest.mark.slow
    def test_generated_wheels_cover_declared_targets(self) -> None:
        """真打一个包（不含 SnowLuma），检查每个组合都有 numpy 和 pillow。

        会联网下载 cp311/cp314/cp315 的 numpy 和 pillow，单条约 200 秒。
        默认不跑（见 pyproject.toml 的 slow marker）；发版前 `-m slow` 验证。
        """
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

    @pytest.mark.slow
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


class ZeroInputStartupTest(unittest.TestCase):
    """新设备上不该要求输入本机 QQ 号。

    SnowLuma hook 到已登录 QQ 之后会自己写 ``config/onebot_<QQ号>.json``，
    文件名就是那个号。所以"识别本机 QQ"不需要问用户 —— 问反而更糟：
    用户报了号，我们还要拿它去匹配，匹配不上就卡住，而错误信息会写成
    "配置不存在"，看起来像包装坏了。
    """

    def _start(self) -> str:
        """start.ps1 与它调用的 run-snowluma.ps1 一起看。

        端口发现其实发生在 run-snowluma 里，只看 start 会漏掉真正的那段。
        """
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        begin = source.index("START_PS1 = r'''")
        end = source.index('CHAT_PS1 = r"""')
        return source[begin:end]

    def test_start_does_not_demand_a_qq_number(self):
        start = self._start()
        self.assertNotIn("先登录一次 QQ，再跑：start.bat", start,
                         "不能在全新设备上要求先报号码")

    def test_start_has_no_hardcoded_fallback_port(self):
        """端口写死 9721 会去等一个永远不会来的端口。

        SnowLuma 起来之前根本没有配置文件，进程起来后才写，而且常用
        3000/3001。回退到一个猜的端口，症状是卡满超时。
        """
        start = self._start()
        self.assertNotIn("$WsPort = 9721", start,
                         "不能在发现失败时回退到写死的端口")

    def test_start_reads_the_account_from_the_generated_config(self):
        """本机号必须来自 SnowLuma 写出的文件名，不是我们猜的。"""
        start = self._start()
        self.assertIn("onebot_", start)
        self.assertIn("发现本机 QQ", start,
                      "要明确报出识别到的账号，用户才知道挂对了没有")

    def test_chat_remembers_the_peer_so_it_is_asked_once(self):
        source = (TOOLS / "make_bundle.py").read_text(encoding="utf-8")
        chat = source[source.index('CHAT_PS1 = r"""'):
                      source.index("CHAT_BAT = r")]
        self.assertIn("peer.txt", chat, "对端号要记住，不能每次都问")
        self.assertIn("上次记住的", chat, "复用了要说明来源，别让人以为没生效")
