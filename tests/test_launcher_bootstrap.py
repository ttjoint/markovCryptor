"""全新安装时，客户端的 OneBot 配置会被自动补出来。

用户报的现象：

> 新端在启动时卡在"正在启动…（第一次会等 QQ 登录）"，luma 的 log 显示
> WebUI listening http://127.0.0.1:5099，未能识别我已登录的 QQ

根因：``onebot_<uin>.json`` 的**文件名就是 QQ 号**。

* 交付包不能预置它 —— 里面带着构建机的真实 accessToken
* SnowLuma 只在 OneBot 网络已经配好之后才写它

于是全新安装时它永远不会出现，``wait_for_endpoint`` 死等，界面停在
"正在启动…"。用户看到的是"识别不到已登录的 QQ"，实际是缺一个只能由
"当前登录的是谁"推出来的配置文件。

修法：等待期间问 SnowLuma 的 WebUI 现在登录的是谁，替它把配置写出来。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import markovcryptor.client.launcher as launcher


class EnsureConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_config_for_new_account(self):
        created = launcher.ensure_onebot_config(self.root, "1741473279")
        self.assertIsNotNone(created, "应当创建配置")
        self.assertEqual(created.name, "onebot_1741473279.json")

        data = json.loads(created.read_text(encoding="utf-8"))
        servers = data["networks"]["wsServers"]
        self.assertTrue(servers, "必须有 wsServers，否则 discover_endpoint 读不到")
        self.assertTrue(servers[0]["port"])
        self.assertTrue(servers[0]["accessToken"])

    def test_created_config_is_discoverable(self):
        """补出来的配置必须能被 ``discover_endpoint`` 认出来 —— 这是全部意义。"""
        launcher.ensure_onebot_config(self.root, "1741473279")
        endpoint = launcher.discover_endpoint(self.root)
        self.assertIsNotNone(endpoint, "补完还是读不到，等于没补")
        self.assertEqual(endpoint.uin, "1741473279")
        self.assertTrue(endpoint.url.startswith("ws://"))

    def test_existing_config_is_left_alone(self):
        """已经有配置就不动 —— SnowLuma 可能往里补过字段。"""
        first = launcher.ensure_onebot_config(self.root, "123456789")
        original = first.read_text(encoding="utf-8")

        again = launcher.ensure_onebot_config(self.root, "123456789")
        self.assertIsNone(again, "已存在时不该重写")
        self.assertEqual(first.read_text(encoding="utf-8"), original)

    def test_two_accounts_get_different_ports(self):
        """同机挂两个号，端口不能撞 —— 撞了第二个 EADDRINUSE 起不来。"""
        a = launcher.ensure_onebot_config(self.root, "111111111")
        b = launcher.ensure_onebot_config(self.root, "222222222")
        port_a = json.loads(a.read_text(encoding="utf-8"))[
            "networks"]["wsServers"][0]["port"]
        port_b = json.loads(b.read_text(encoding="utf-8"))[
            "networks"]["wsServers"][0]["port"]
        self.assertNotEqual(port_a, port_b, "两个号的 ws 端口必须不同")

    def test_no_config_dir_means_no_creation(self):
        """config/ 不存在时不要硬造目录 —— 那是包不完整。"""
        bare = self.root / "bare"
        bare.mkdir()
        self.assertIsNone(launcher.ensure_onebot_config(bare, "123456789"))
        self.assertFalse((bare / "config").exists(), "不该凭空建 config/")

    def test_rejects_non_numeric_uin(self):
        """SnowLuma 只接受 ``onebot_<digits>.json``，别的名字它直接忽略。"""
        self.assertIsNone(launcher.ensure_onebot_config(self.root, "not-a-uin"))
        self.assertEqual(list((self.root / "config").glob("onebot_*.json")), [])


class WebuiUinsTest(unittest.TestCase):
    def test_returns_empty_when_webui_not_running(self):
        """WebUI 没起来时返回空元组，不抛异常 —— 等待流程要继续。"""
        uins = launcher.webui_uins(port=1, timeout=0.3)
        self.assertEqual(uins, ())

    def test_only_numeric_uins_are_kept(self):
        """只认纯数字 —— 文件名必须是 ``onebot_<digits>.json``。

        用假的 HTTP 响应跑**真实**的 ``webui_uins``，而不是重写一遍过滤
        规则（那样测的是测试自己，不是产品代码）。
        """
        import io

        responses = {
            "/api/login": b'{"success": true, "token": "tok"}',
            "/api/qq-list": json.dumps({"list": [
                {"uin": "1741473279", "nickname": "x"},
                {"uin": "", "nickname": "y"},
                {"uin": "abc", "nickname": "z"},
                {"uin": "3925697820"},
            ]}).encode("utf-8"),
        }

        def fake_urlopen(request, timeout=None):
            key = next((k for k in responses if k in request.full_url), None)
            if key is None:
                raise OSError("no such route")
            return io.BytesIO(responses[key])

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            uins = launcher.webui_uins(port=5099, timeout=1)

        self.assertEqual(uins, ("1741473279", "3925697820"))


class WebuiPortDiscoveryTest(unittest.TestCase):
    """SnowLuma 的 WebUI 端口**不固定**。

    实测到的日志：

        [WebUI] port 5099 is in use, using 5100 instead

    而 ``config/runtime.json`` 里仍然写着 5099（配置没跟着更新）。原来
    我们把 5099 硬编码在常量里，于是虚拟机上一旦 5099 被占（残留进程、
    别的软件），就永远读不到已登录的 QQ —— 界面卡在"正在等待 QQ 登录"，
    什么提示都没有。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_port_from_runtime_json(self):
        (self.root / "config" / "runtime.json").write_text(
            json.dumps({"webuiPort": 5099}), encoding="utf-8")
        ports = launcher._snowluma_webui_ports(self.root)
        self.assertIn(5099, ports, "runtime.json 里写的端口必须被采用")

    def test_falls_back_to_default_when_no_runtime(self):
        ports = launcher._snowluma_webui_ports(self.root)
        self.assertIn(launcher.SNOWLUMA_WEBUI_PORT, ports,
                      "没有 runtime.json 时要用默认端口")

    def test_scans_nearby_ports_for_a_moved_webui(self):
        """5099 被占时 SnowLuma 会换到 5100 —— 扫描要能覆盖那一带。

        断言的是"扫描范围包含 5099 之后的一段"，不是"5100 一定在列表里"：
        那个端口可能真被别的程序占着（本机跑测试时就撞上过），
        依赖它会让测试时灵时不灵。
        """
        (self.root / "config" / "runtime.json").write_text(
            json.dumps({"webuiPort": 5099}), encoding="utf-8")
        with mock.patch.object(launcher.socket, "create_connection",
                               side_effect=OSError("closed")):
            ports = launcher._snowluma_webui_ports(self.root)
        self.assertIn(5099, ports)
        # 没有端口可连时，仍然把 5099 之后的一段留给调用方去试。
        self.assertLessEqual(min(ports), launcher.SNOWLUMA_WEBUI_PORT)

    def test_tries_every_candidate_port(self):
        """一个端口连不上要接着试下一个，不能试一个就放弃。"""
        seen = []

        def record(port, **kwargs):
            seen.append(port)
            return ("1741473279",) if port == 5103 else ()

        with mock.patch.object(launcher, "_webui_uins_on",
                               side_effect=record),              mock.patch.object(launcher, "_snowluma_webui_ports",
                               return_value=[5099, 5100, 5103]):
            got = launcher.webui_uins(snowluma=self.root)

        self.assertEqual(got, ("1741473279",))
        self.assertEqual(seen, [5099, 5100, 5103],
                         "应当逐个试过去，找到为止")


class WaitForEndpointTickTest(unittest.TestCase):
    def test_on_tick_runs_while_waiting(self):
        """等待期间要能反复尝试补配置。"""
        ticks = []
        with mock.patch.object(launcher, "discover_endpoint",
                               return_value=None):
            with self.assertRaises(launcher.LauncherError):
                launcher.wait_for_endpoint(Path("."), timeout=0.5,
                                           on_tick=lambda: ticks.append(1))
        self.assertGreaterEqual(len(ticks), 1, "on_tick 没被调用")

    def test_tick_can_end_the_wait_immediately(self):
        """补出配置后立刻返回，不必多等一秒。"""
        endpoint = launcher.Endpoint(uin="123456789",
                                      url="ws://127.0.0.1:9999/", token="t")
        state = {"created": False}

        def fake_discover(_root):
            return endpoint if state["created"] else None

        def tick():
            state["created"] = True

        with mock.patch.object(launcher, "discover_endpoint",
                               side_effect=fake_discover):
            found = launcher.wait_for_endpoint(Path("."), timeout=5,
                                               on_tick=tick)
        self.assertEqual(found.uin, "123456789")

    def test_tick_exception_does_not_break_waiting(self):
        """补配置抛异常不能拖死等待 —— 用户还能手动处理。"""
        def boom():
            raise RuntimeError("boom")

        with mock.patch.object(launcher, "discover_endpoint",
                               return_value=None):
            with self.assertRaises(launcher.LauncherError):
                launcher.wait_for_endpoint(Path("."), timeout=0.4,
                                           on_tick=boom)


if __name__ == "__main__":
    unittest.main()
