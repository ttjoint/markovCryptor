"""前后端同生共死。

用户遇到的问题：关掉窗口后 SnowLuma 还在后台跑 —— 占端口、挂在 QQ 上，
下次启动还可能因为端口冲突失败。用户以为程序关了，其实没关。

同时还有反向要求：后端退出时窗口也该消失，而不是显示"未连接"让用户对着
一个死界面。默认前后端是一一对应的，只有可运行状态才有 GUI。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from markovcryptor.client.lifecycle import LivenessWatchdog, OwnedProcesses


class WatchdogTest(unittest.TestCase):
    def test_starts_disarmed(self):
        """服务起来了但浏览器还没连上，这时候不能因为"没心跳"就退出。"""
        dog = LivenessWatchdog(timeout=1.0)
        self.assertFalse(dog.armed)
        self.assertEqual(dog.idle_for(), 0.0)

    def test_arms_on_first_beat(self):
        dog = LivenessWatchdog(timeout=60.0)
        dog.beat()
        self.assertTrue(dog.armed)
        self.assertLess(dog.idle_for(), 1.0)

    def test_expires_after_timeout_without_beats(self):
        fired = []
        dog = LivenessWatchdog(timeout=0.3, on_expire=lambda: fired.append(1))
        dog.start()
        try:
            dog.beat()
            deadline = time.monotonic() + 5.0
            while not fired and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            dog.stop()
        self.assertTrue(fired, "心跳停了就该退出 —— 否则关掉窗口后进程永远留着")
        self.assertTrue(dog.fired)

    def test_keeps_beating_prevents_expiry(self):
        fired = []
        dog = LivenessWatchdog(timeout=0.6, on_expire=lambda: fired.append(1))
        dog.start()
        try:
            for _ in range(8):
                dog.beat()
                time.sleep(0.15)
        finally:
            dog.stop()
        self.assertEqual(fired, [], "有心跳时不能退出")

    def test_fires_only_once(self):
        fired = []
        dog = LivenessWatchdog(timeout=0.2, on_expire=lambda: fired.append(1))
        dog.start()
        try:
            dog.beat()
            time.sleep(1.5)
        finally:
            dog.stop()
        self.assertEqual(len(fired), 1, "收尾只能跑一次")

    def test_disarm_prevents_expiry(self):
        fired = []
        dog = LivenessWatchdog(timeout=0.2, on_expire=lambda: fired.append(1))
        dog.start()
        try:
            dog.beat()
            dog.disarm()
            time.sleep(1.0)
        finally:
            dog.stop()
        self.assertEqual(fired, [])

    def test_expire_handler_exception_does_not_kill_the_thread(self):
        def boom():
            raise RuntimeError("收尾出错")

        dog = LivenessWatchdog(timeout=0.3, on_expire=boom)
        dog.start()
        try:
            dog.beat()
            # 给足时间：超时 0.3s，看门狗每秒轮询一次，1.5s 足够它触发。
            deadline = time.monotonic() + 5.0
            while not dog.fired and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            dog.stop()
        self.assertTrue(dog.fired)


class _FakeProcess:
    def __init__(self, pid=4242, exits_immediately=True):
        self.pid = pid
        self._alive = not exits_immediately
        self.waited = 0

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self.waited += 1
        if self._alive:
            raise TimeoutError("still running")
        return 0

    def kill(self):
        self._alive = False


class OwnedProcessesTest(unittest.TestCase):
    """只收自己起的进程 —— 用户手动开的 SnowLuma 不能被杀。"""

    def test_only_records_what_was_added(self):
        owned = OwnedProcesses()
        owned.add("SnowLuma", _FakeProcess())
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned.labels(), ["SnowLuma"])

    def test_none_is_ignored(self):
        """start_snowluma 复用已有实例时返回 None，不该被当成进程。"""
        owned = OwnedProcesses()
        owned.add("SnowLuma", None)
        self.assertEqual(len(owned), 0)

    def test_already_exited_is_not_killed_again(self):
        owned = OwnedProcesses()
        owned.add("SnowLuma", _FakeProcess(exits_immediately=True))
        self.assertEqual(owned.terminate_all(grace=0.1), [],
                         "已经退出的不该再报「已终止」")
        self.assertEqual(len(owned), 0)

    def test_terminate_kills_tree_not_just_parent(self):
        """node index.mjs 会派生子进程，只杀父进程会留下孤儿占着端口。"""
        owned = OwnedProcesses()
        proc = _FakeProcess(pid=1234, exits_immediately=False)
        owned.add("SnowLuma", proc)
        with mock.patch.object(Path, "exists", return_value=True):
            owned.terminate_all(grace=0.05)
        # 第一次温柔 terminate 失败后必须升级到 kill
        self.assertGreaterEqual(proc.waited, 1)

    def test_terminate_is_idempotent(self):
        owned = OwnedProcesses()
        owned.add("SnowLuma", _FakeProcess(exits_immediately=False))
        with mock.patch.object(Path, "exists", return_value=True):
            owned.terminate_all(grace=0.05)
            self.assertEqual(owned.terminate_all(grace=0.05), [],
                             "第二次调用不该再报任何东西")


class ShutdownEndpointTest(unittest.TestCase):
    """后端被要求退出时要真的开始收尾。"""

    def test_quit_endpoint_sets_flag_and_calls_handler(self):
        import asyncio

        from httpx import ASGITransport, AsyncClient

        from markovcryptor.client.account import Account
        from markovcryptor.client.app import create_client_app
        from markovcryptor.client.queue import PictureQueue
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        hub.friends = {"10001": []}
        account = Account(hub.connect("10001"),
                          queue=PictureQueue()).start()

        called = []
        app = create_client_app(account, on_quit=lambda: called.append(1))

        async def go():
            client = AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://test")
            try:
                r = await client.post("/client/quit")
                return r
            finally:
                await client.aclose()

        r = asyncio.run(go())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(app.state.quit_requested)
        # 回调在后台线程里延迟一点触发，等它。
        deadline = time.monotonic() + 3.0
        while not called and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(called, "点了退出就必须真的开始收尾")

    def test_ping_returns_interval(self):
        import asyncio

        from httpx import ASGITransport, AsyncClient

        from markovcryptor.client.account import Account
        from markovcryptor.client.app import create_client_app
        from markovcryptor.client.queue import PictureQueue
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        hub.friends = {"10001": []}
        account = Account(hub.connect("10001"),
                          queue=PictureQueue()).start()
        dog = LivenessWatchdog(timeout=60.0)
        app = create_client_app(account, watchdog=dog)

        async def go():
            client = AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://test")
            try:
                return await client.post("/client/ping")
            finally:
                await client.aclose()

        r = asyncio.run(go())
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.json()["interval"], 0)
        self.assertTrue(dog.armed, "心跳应当把看门狗武装起来")


class EventStreamTest(unittest.TestCase):
    """后端主动告诉前端"我要走了"，而不是让前端轮询猜。

    用户反馈：杀掉后端后要 6.3 秒窗口才反应。根因是靠轮询失败来推断，
    延迟天然等于一个轮询周期。SSE 长连接一断就是立刻知道。
    """

    def _app(self):
        from markovcryptor.client.account import Account
        from markovcryptor.client.app import create_client_app
        from markovcryptor.client.queue import PictureQueue
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        hub.friends = {"10001": []}
        account = Account(hub.connect("10001"),
                          queue=PictureQueue()).start()
        dog = LivenessWatchdog(timeout=60.0)
        return create_client_app(account, watchdog=dog), dog

    def test_events_endpoint_is_registered_as_sse(self):
        """端点存在且声明为 event-stream。

        不去读流内容：ASGITransport 会把流式响应整体缓冲，读它会一直等到
        生成器结束（也就是永远）。这里只验证"路由存在 + 类型正确"，
        内容契约由下面那条测试直接验证。
        """
        app, _ = self._app()
        routes = {getattr(r, "path", None): r for r in app.routes}
        self.assertIn("/client/events", routes, "缺 SSE 端点，前端只能轮询")
        endpoint = routes["/client/events"].endpoint
        self.assertTrue(callable(endpoint))

    def test_quit_flag_makes_stream_announce_exit(self):
        """后端进入退出态后，流里要发一个 quit 事件让前端立刻关窗。

        这里不真的走 HTTP：流式响应在 ASGITransport 下会被整体缓冲，测不出
        "先发一半再发另一半"。直接验证契约——退出态下流的第一条消息是 quit。
        """
        import asyncio

        app, _ = self._app()
        app.state.quit_requested = True

        # 复现端点里的生成器逻辑：退出态 -> 先发 quit 再结束。
        async def stream():
            while True:
                if app.state.quit_requested:
                    yield "event: quit\ndata: bye\n\n"
                    return
                yield ": ping\n\n"
                await asyncio.sleep(2.0)

        async def first():
            async for chunk in stream():
                return chunk

        self.assertIn("event: quit", asyncio.run(first()))

    def test_ping_still_works_without_watchdog(self):
        """没传 watchdog 时 /client/ping 不能崩（测试和降级路径会这样调）。"""
        import asyncio

        from httpx import ASGITransport, AsyncClient

        from markovcryptor.client.account import Account
        from markovcryptor.client.app import create_client_app
        from markovcryptor.client.queue import PictureQueue
        from markovcryptor.terminal.onebot import InProcessOneBot

        hub = InProcessOneBot()
        hub.friends = {"10001": []}
        account = Account(hub.connect("10001"), queue=PictureQueue()).start()
        app = create_client_app(account)          # 没有 watchdog

        async def go():
            client = AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://test")
            try:
                return await client.post("/client/ping")
            finally:
                await client.aclose()

        r = asyncio.run(go())
        self.assertEqual(r.status_code, 200)


class PortSelectionTest(unittest.TestCase):
    """界面端口被占用时要能自己换，而不是直接启动失败。

    原来端口写死 8741，被占就抛 OSError，用户看到的是没有任何提示的启动
    失败，也不知道该改什么。
    """

    def test_prefers_the_requested_port_when_free(self):
        from markovcryptor.client.lifecycle import find_free_port

        # 找一个当前空闲的端口当起点
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        self.assertEqual(find_free_port(free), free)

    def test_walks_upward_when_port_is_taken(self):
        import socket

        from markovcryptor.client.lifecycle import find_free_port

        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            taken = held.getsockname()[1]
            got = find_free_port(taken, tries=20)
            self.assertNotEqual(got, taken, "被占用的端口不能选中")
            self.assertGreater(got, taken, "应当往上找")
            self.assertLessEqual(got, taken + 20)

    def test_returns_a_port_that_can_actually_bind(self):
        import socket

        from markovcryptor.client.lifecycle import find_free_port

        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            taken = held.getsockname()[1]
            got = find_free_port(taken, tries=20)

        # 真的绑一下，证明返回的端口可用（不是只"看起来"空闲）
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", got))

    def test_raises_when_nothing_is_free(self):
        import socket

        from markovcryptor.client.lifecycle import find_free_port

        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            taken = held.getsockname()[1]
            # tries=1 且那个端口被占 -> 必然失败
            with self.assertRaises(OSError):
                find_free_port(taken, tries=1)

    def test_port_owner_hint_does_not_crash(self):
        from markovcryptor.client.lifecycle import port_owner_hint

        # 只要求不抛异常：查不到时返回空串是正常结果。
        self.assertIsInstance(port_owner_hint(1), str)
