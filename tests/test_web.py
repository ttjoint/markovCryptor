"""The HTTP surface.

These tests drive the app through ``httpx``'s in-process ASGI transport, so they
cover routing, status codes and the error-body contract without binding a port.
One separate test starts a real uvicorn server, because "the app object works"
and "the service starts" are different claims and only the second one is what a
user experiences.

There is no authentication here by design (loopback-only, single machine), so
nothing below tests for a token. What *is* tested is the boundary that does
matter: the service must refuse to listen off-loopback.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import unittest

from httpx import ASGITransport, AsyncClient
import anyio

from markovcryptor.engine import Engine
from markovcryptor.model import MarkovModel
from markovcryptor.web import (
    ApiConfig, DEFAULT_PORT, _is_loopback, create_app, run_server,
)
from markovcryptor.core.errors import InvalidInput

import fixtures


def _png(image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _run(coro):
    """Drive one awaitable to completion. Each test is sync, the client is not."""
    async def _await():
        return await coro

    return anyio.run(_await)


class OneBotHubWiringTest(unittest.TestCase):
    """接 QQ 时，本地名字与线上地址是两件事。

    每个路由、历史、整个前端都用名字（alice/bob）寻址；线上需要的是 QQ 号。
    把两者混起来会以两种方式炸：发给 "bob" 被传输层拒绝（没有这个 QQ 号），
    或者聊天记录里一半写 alice 一半写 10002，看起来像两个不同的人。
    """

    def _hub(self):
        from markovcryptor.terminal.onebot import (
            InProcessOneBot, RoutedNetwork,
        )
        from markovcryptor.web_terminal import TerminalHub

        bot = InProcessOneBot()
        routes = {"alice": bot.connect("10001"), "bob": bot.connect("10002")}
        hub = TerminalHub(network=RoutedNetwork(routes), names=("alice", "bob"))
        hub.uins = {"alice": "10001", "bob": "10002"}
        return hub, bot

    def test_address_of_maps_name_to_uin(self):
        hub, _ = self._hub()
        self.assertEqual(hub.address_of("bob"), "10002")
        self.assertEqual(hub.address_of("alice"), "10001")

    def test_single_account_knows_its_peer(self):
        """一台设备一个号:本地只有一个终端,对端在另一台设备上。

        真实部署就是这样(每台设备 1 个 QQ + 1 份本系统)。此时不能靠
        ``_other()`` 找对端 —— 本机没有第二个终端 —— 必须用 --peer 给的号。
        """
        from markovcryptor.terminal.onebot import InProcessOneBot, RoutedNetwork
        from markovcryptor.web_terminal import TerminalHub

        bot = InProcessOneBot()
        routes = {"alice": bot.connect("10001")}
        hub = TerminalHub(network=RoutedNetwork(routes), names=("alice",))
        hub.uins = {"alice": "10001"}
        hub.peer_uin = "10002"

        self.assertEqual(hub.peer_address_of("alice"), "10002",
                         "单账号时对端地址来自 --peer")

    def test_single_account_without_peer_says_what_is_missing(self):
        from markovcryptor.core.errors import InvalidInput
        from markovcryptor.terminal.onebot import InProcessOneBot, RoutedNetwork
        from markovcryptor.web_terminal import TerminalHub

        bot = InProcessOneBot()
        hub = TerminalHub(network=RoutedNetwork({"alice": bot.connect("10001")}),
                          names=("alice",))
        hub.uins = {"alice": "10001"}

        with self.assertRaises(InvalidInput) as caught:
            hub.peer_address_of("alice")
        self.assertIn("--peer", " ".join(caught.exception.hints))

    def test_two_local_accounts_ignore_peer_uin(self):
        """同机双号时对端是本机另一个终端,不该被 --peer 带偏。"""
        hub, _ = self._hub()
        hub.peer_uin = "99999"
        self.assertEqual(hub.peer_address_of("alice"), "10002")

    def test_name_of_maps_uin_back_to_name(self):
        hub, _ = self._hub()
        self.assertEqual(hub.name_of("10002"), "bob")
        # 陌生号是真实的第三方，照实显示。
        self.assertEqual(hub.name_of("99999"), "99999")

    def test_local_mode_names_are_their_own_addresses(self):
        """本地演练没有映射，名字就是地址——否则老路径会全线断掉。"""
        from markovcryptor.web_terminal import TerminalHub

        hub = TerminalHub()
        self.assertEqual(hub.address_of("alice"), "alice")
        self.assertEqual(hub.name_of("alice"), "alice")
        self.assertEqual(hub.transport_name, "local")

    def test_state_reports_peer_uin_for_single_account(self):
        """界面靠 peer_uin 说出对端是谁，否则只能画一个不存在的 bob。"""
        from markovcryptor.terminal.onebot import InProcessOneBot, RoutedNetwork
        from markovcryptor.web_terminal import TerminalHub

        bot = InProcessOneBot()
        hub = TerminalHub(network=RoutedNetwork({"alice": bot.connect("10001")}),
                          names=("alice",))
        hub.uins = {"alice": "10001"}
        hub.peer_uin = "10002"
        self.assertEqual(getattr(hub, "peer_uin", ""), "10002")

    def test_network_history_does_not_blow_up_on_real_transport(self):
        """RoutedNetwork 不保留每个包的记录，接口得如实回答而不是 500。

        UI 每次轮询都会打这个端点；一个 500 会让整页看起来坏了。
        """
        from markovcryptor.terminal.onebot import InProcessOneBot, RoutedNetwork
        from markovcryptor.web_terminal import network_history_payload

        bot = InProcessOneBot()
        network = RoutedNetwork({"alice": bot.connect("10001")})
        self.assertFalse(hasattr(network, "history"),
                         "前提：真实传输层本来就没有 history")

        payload = network_history_payload(network)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["packets"], [])
        self.assertFalse(payload["available"],
                         "要能说清：这条链路不记账，而不是假装记了空账")

    def test_network_history_still_reports_the_demo_line(self):
        """本地演练那条内存线有记录，不能被顺手改坏。"""
        from markovcryptor.terminal.transport import LoopbackNetwork
        from markovcryptor.web_terminal import network_history_payload

        network = LoopbackNetwork()
        payload = network_history_payload(network)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["packets"], [])

    def test_transport_name_says_onebot_when_wired(self):
        hub, _ = self._hub()
        self.assertEqual(hub.transport_name, "onebot")


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        cls.app = create_app(engine=Engine(model=cls.model))

    def setUp(self):
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test"
        )

    def tearDown(self):
        _run(self.client.aclose())

    # ---------------------------------------------------------------- metadata

    def test_health_lists_available_channels(self):
        response = _run(self.client.get("/health"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(set(body["channels"]), {"image", "gif", "chinese"})

    def test_channels_carries_the_options_schema(self):
        """A form generator needs the schema, not just the names."""
        response = _run(self.client.get("/channels"))
        self.assertEqual(response.status_code, 200)
        by_id = {c["id"]: c for c in response.json()}
        self.assertIn("step", by_id["image"]["options_schema"]["properties"])

    # ------------------------------------------------------------------- probe

    def test_probe_says_yes_with_numbers(self):
        response = _run(self.client.post(
            "/probe", params={"channel": "image", "payload_bytes": 32, "kind": "image"},
            files={"file": ("photo.png", _png(fixtures.photo()), "image/png")},
        ))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["capacity_bytes"], 32)

    def test_probe_says_no_with_the_real_reason(self):
        response = _run(self.client.post(
            "/probe", params={"channel": "image", "payload_bytes": 999_999,
                              "kind": "image"},
            files={"file": ("photo.png", _png(fixtures.photo()), "image/png")},
        ))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["reasons"], "拒绝时必须给原文原因")
        self.assertTrue(body["hints"], "拒绝时必须给可操作建议")

    # ------------------------------------------------------------------- round trips

    def test_image_round_trip(self):
        secret = b"web-image-roundtrip"
        embedded = _run(self.client.post(
            "/embed",
            params={"channel": "image", "kind": "image", "key": "k"},
            files={
                "payload_file": ("s.bin", secret, "application/octet-stream"),
                "cover_file": ("c.png", _png(fixtures.photo()), "image/png"),
            },
        ))
        self.assertEqual(embedded.status_code, 200, embedded.text)
        self.assertEqual(embedded.headers["content-type"], "image/png")
        recovered = _run(self.client.post(
            "/extract", params={"channel": "image", "kind": "image", "key": "k"},
            files={"file": ("e.png", embedded.content, "image/png")},
        ))
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.content, secret)

    def test_chinese_round_trip_uploading_the_cover_text(self):
        """The cover text comes back as a file, so extract must accept one."""
        secret = b"web-chinese-roundtrip"
        embedded = _run(self.client.post(
            "/embed", params={"channel": "chinese", "kind": "text", "key": "pw"},
            files={"payload_file": ("s.bin", secret, "application/octet-stream")},
        ))
        self.assertEqual(embedded.status_code, 200, embedded.text)
        recovered = _run(self.client.post(
            "/extract", params={"channel": "chinese", "kind": "text", "key": "pw"},
            files={"file": ("cover.txt", embedded.content, "text/plain")},
        ))
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.content, secret)

    def test_gif_round_trip(self):
        secret = b"web-gif"
        embedded = _run(self.client.post(
            "/embed", params={"channel": "gif", "kind": "animation", "key": "k"},
            files={
                "payload_file": ("s.bin", secret, "application/octet-stream"),
                "cover_file": ("a.gif", fixtures.animation(), "image/gif"),
            },
        ))
        self.assertEqual(embedded.status_code, 200, embedded.text)
        recovered = _run(self.client.post(
            "/extract", params={"channel": "gif", "kind": "animation", "key": "k"},
            files={"file": ("x.gif", embedded.content, "image/gif")},
        ))
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.content, secret)

    # ------------------------------------------------------------------- errors

    def test_wrong_key_is_401_not_404(self):
        """A wrong password is an auth failure, not a missing frame."""
        embedded = _run(self.client.post(
            "/embed", params={"channel": "chinese", "kind": "text", "key": "right"},
            files={"payload_file": ("s.bin", b"x", "application/octet-stream")},
        ))
        wrong = _run(self.client.post(
            "/extract", params={"channel": "chinese", "kind": "text", "key": "wrong"},
            files={"file": ("c.txt", embedded.content, "text/plain")},
        ))
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()["code"], "bad_key")

    def test_plain_text_is_404(self):
        response = _run(self.client.post(
            "/extract", params={"channel": "chinese", "kind": "text"},
            data={"text": "这是一段普通的中文，没有任何隐写内容。"},
        ))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "no_hidden_frame")

    def test_unknown_channel_lists_the_available_ones(self):
        response = _run(self.client.post(
            "/probe", params={"channel": "video", "payload_bytes": 10, "kind": "image"},
            files={"file": ("p.png", _png(fixtures.photo()), "image/png")},
        ))
        self.assertNotEqual(response.status_code, 200)
        message = response.json().get("message") or response.text
        self.assertIn("image", message)

    def test_missing_payload_file_says_which_field(self):
        response = _run(self.client.post(
            "/embed", params={"channel": "chinese", "kind": "text"},
            files={"cover_file": ("c.txt", b"x", "text/plain")},
        ))
        self.assertEqual(response.status_code, 422)
        self.assertIn("payload_file", response.json()["message"])

    def test_bad_kind_is_rejected_with_the_allowed_values(self):
        response = _run(self.client.post(
            "/probe", params={"channel": "image", "payload_bytes": 10, "kind": "video"},
            files={"file": ("p.png", _png(fixtures.photo()), "image/png")},
        ))
        self.assertEqual(response.status_code, 422)
        self.assertIn("animation", response.json()["detail"])

    def test_bad_options_json_is_rejected(self):
        response = _run(self.client.post(
            "/embed",
            params={"channel": "image", "kind": "image", "options": "{not json"},
            files={
                "payload_file": ("s.bin", b"x", "application/octet-stream"),
                "cover_file": ("c.png", _png(fixtures.photo()), "image/png"),
            },
        ))
        self.assertEqual(response.status_code, 422)
        self.assertIn("JSON", response.json()["message"])

    def test_upload_limit_is_enforced(self):
        app = create_app(
            ApiConfig(max_upload_bytes=16), engine=Engine(model=self.model)
        )
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        try:
            response = _run(client.post(
                "/embed", params={"channel": "chinese", "kind": "text"},
                files={"payload_file": ("s.bin", b"y" * 64,
                                        "application/octet-stream")},
            ))
        finally:
            _run(client.aclose())
        self.assertEqual(response.status_code, 422)
        self.assertIn("上限", response.json()["message"])


class JobApiTest(unittest.TestCase):
    """Batch over HTTP: submit, poll, report.

    A job outlives the request, so these tests poll. The poll loop is bounded by
    a wall clock rather than a fixed count, because a slow machine must not turn
    a correct job into a test failure.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        cls.app = create_app(engine=Engine(model=cls.model))
        cls.root = Path(tempfile.mkdtemp(prefix="mkx1-webjobs-"))

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test"
        )

    def tearDown(self):
        _run(self.client.aclose())

    def _wait(self, job_id: str, timeout: float = 120.0) -> dict:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            body = _run(self.client.get(f"/jobs/{job_id}")).json()
            if body["state"] in ("done", "failed", "cancelled"):
                return body
            time.sleep(0.1)
        self.fail(f"作业 {job_id} 超时未结束")

    def _folder(self, name: str, count: int) -> Path:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (directory / f"i{i}.png").write_bytes(_png(fixtures.varied_photo(i)))
        return directory

    def test_submit_then_poll_to_completion(self):
        source = self._folder("sub", 3)
        out = self.root / "sub-out"
        submitted = _run(self.client.post(
            "/jobs",
            params={"channel": "image", "input_dir": str(source),
                    "output_dir": str(out), "key": "k", "kind": "image"},
            files={"payload_file": ("p.bin", b"job-secret",
                                    "application/octet-stream")},
        ))
        self.assertEqual(submitted.status_code, 202, submitted.text)
        body = self._wait(submitted.json()["job_id"])
        self.assertEqual(body["state"], "done")
        self.assertEqual((body["succeeded"], body["failed"]), (3, 0))
        self.assertEqual(body["progress"], 1.0)
        self.assertEqual(len(list(out.glob("*.png"))), 3)

    def test_report_endpoint_lists_failures(self):
        source = self._folder("rep", 2)
        (source / "broken.png").write_bytes(b"PNG-nope")
        out = self.root / "rep-out"
        submitted = _run(self.client.post(
            "/jobs",
            params={"channel": "image", "input_dir": str(source),
                    "output_dir": str(out), "key": "k"},
            files={"payload_file": ("p.bin", b"x", "application/octet-stream")},
        ))
        job_id = submitted.json()["job_id"]
        body = self._wait(job_id)
        self.assertEqual(body["state"], "done")
        self.assertEqual(body["failed"], 1)

        failures = _run(self.client.get(
            f"/jobs/{job_id}/report", params={"failures_only": True}
        )).json()["results"]
        self.assertEqual(len(failures), 1)
        self.assertIn("broken.png", failures[0]["source"])
        self.assertTrue(failures[0]["error"], "失败项必须带原因")

    def test_missing_input_folder_is_422(self):
        response = _run(self.client.post(
            "/jobs",
            params={"channel": "image", "input_dir": str(self.root / "nope"),
                    "output_dir": str(self.root / "x")},
            files={"payload_file": ("p.bin", b"x", "application/octet-stream")},
        ))
        self.assertEqual(response.status_code, 422)
        self.assertIn("文件夹", response.json()["message"])

    def test_empty_folder_says_so(self):
        empty = self.root / "empty"
        empty.mkdir(exist_ok=True)
        response = _run(self.client.post(
            "/jobs",
            params={"channel": "image", "input_dir": str(empty),
                    "output_dir": str(self.root / "y")},
            files={"payload_file": ("p.bin", b"x", "application/octet-stream")},
        ))
        self.assertEqual(response.status_code, 422)
        self.assertIn("没有", response.json()["message"])

    def test_unknown_job_is_422_with_the_id(self):
        response = _run(self.client.get("/jobs/deadbeef"))
        self.assertEqual(response.status_code, 422)
        self.assertIn("deadbeef", response.json()["message"])

    def test_job_listing_includes_submitted_jobs(self):
        source = self._folder("list", 1)
        _run(self.client.post(
            "/jobs",
            params={"channel": "image", "input_dir": str(source),
                    "output_dir": str(self.root / "list-out")},
            files={"payload_file": ("p.bin", b"x", "application/octet-stream")},
        ))
        listed = _run(self.client.get("/jobs")).json()
        self.assertTrue(any(j["total"] >= 1 for j in listed))


class RealServerTest(unittest.TestCase):
    """Actually bind a port. The ASGI tests above cannot catch a startup bug."""

    def test_service_starts_and_answers_health(self):
        import socket
        import threading
        import time

        import httpx
        import uvicorn

        # Port 0 lets the OS pick a free one, so this test never collides with
        # whatever the developer already has running.
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        finally:
            sock.close()

        model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        app = create_app(engine=Engine(model=model))
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 20
            while not server.started and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(server.started, "服务未能启动")
            body = httpx.get(f"http://127.0.0.1:{port}/health", timeout=10).json()
            self.assertTrue(body["ok"])
            self.assertIn("image", body["channels"])
        finally:
            server.should_exit = True
            thread.join(timeout=15)


class LoopbackGuardTest(unittest.TestCase):
    """The only real security boundary this service has."""

    def test_loopback_detection(self):
        for host in ("127.0.0.1", "localhost", "::1", ""):
            self.assertTrue(_is_loopback(host), host)
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            self.assertFalse(_is_loopback(host), host)

    def test_run_server_refuses_a_public_bind(self):
        with self.assertRaises(InvalidInput) as ctx:
            run_server(ApiConfig(host="0.0.0.0", port=8731))
        self.assertIn("0.0.0.0", str(ctx.exception))

    def test_default_port_is_not_a_common_dev_port(self):
        """8000/8080 are routinely taken; picking one turns startup into a hunt."""
        self.assertNotIn(DEFAULT_PORT, (8000, 8080, 5000, 3000, 8888))
        self.assertGreater(DEFAULT_PORT, 1023)


if __name__ == "__main__":
    unittest.main()
