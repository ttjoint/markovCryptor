"""发送/接收任务调度的测试。

这一层存在的理由：Markov 编码 0.6–0.75 秒、1600² 图片嵌入 3.3 秒，原来直接
跑在 ``async def`` 里阻塞事件循环，对方发几张图就能让客户端卡死几十秒。
这些测试盯的是「卡死」和「串行」两件事，而不是具体耗时。
"""

from __future__ import annotations

import threading
import time
import unittest

from markovcryptor.client.tasks import (
    INBOUND_LIMIT,
    TaskManager,
    TaskState,
)


class _Waiter:
    """让 worker 卡在指定点，便于测试取消与排队。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()


def _wait_for(manager, task, timeout=2.0):
    """轮询到任务结束（不 sleep 固定时间，避免慢机器上假失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = manager.get(task.id)
        if current and current.state in (TaskState.DONE, TaskState.FAILED,
                                         TaskState.CANCELLED):
            return current
        time.sleep(0.01)
    raise AssertionError(f"任务 {task.id} 在 {timeout}s 内没有结束")


class PerPeerSerialTest(unittest.TestCase):
    def test_same_peer_second_send_is_refused(self):
        """同一会话不允许两个在途发送：消息顺序会乱，按钮也该禁用。"""
        manager = TaskManager()
        gate = _Waiter()

        def work(task):
            gate.started.set()
            gate.release.wait(2.0)
            return {"sent": 1}

        first = manager.submit("alice", "image", work)
        self.assertIsNotNone(first)
        gate.started.wait(2.0)

        second = manager.submit("alice", "image", lambda t: {})
        self.assertIsNone(second, "同一 peer 的第二个任务应该被拒绝")

        gate.release.set()
        _wait_for(manager, first)
        manager.shutdown()

    def test_different_peers_run_concurrently(self):
        """不同好友互不影响 —— 一个卡住不该拖住另一个。"""
        manager = TaskManager(image_workers=2)
        gates = {p: _Waiter() for p in ("alice", "bob")}

        def make(peer):
            def work(task):
                gates[peer].started.set()
                gates[peer].release.wait(2.0)
                return {}
            return work

        a = manager.submit("alice", "image", make("alice"))
        b = manager.submit("bob", "image", make("bob"))
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)

        # 两个都真的跑起来了，说明没有互相阻塞
        self.assertTrue(gates["alice"].started.wait(2.0))
        self.assertTrue(gates["bob"].started.wait(2.0))

        for gate in gates.values():
            gate.release.set()
        _wait_for(manager, a)
        _wait_for(manager, b)
        manager.shutdown()

    def test_peer_becomes_free_after_completion(self):
        """发完之后同一会话必须能再发 —— 否则按钮会永久禁用。"""
        manager = TaskManager()
        task = manager.submit("alice", "image", lambda t: {})
        _wait_for(manager, task)
        self.assertIsNone(manager.active_for("alice"))
        again = manager.submit("alice", "image", lambda t: {})
        self.assertIsNotNone(again)
        _wait_for(manager, again)
        manager.shutdown()


class TextChannelTest(unittest.TestCase):
    def test_text_tasks_are_serialised(self):
        """文本编码是纯 Python 循环，不释放 GIL，所以必须串行。

        两个任务若并行，只会互相抢 GIL，总耗时反而更长；串行至少让
        先来的先完成。
        """
        manager = TaskManager()
        order: list[str] = []
        lock = threading.Lock()

        def work(name):
            def run(task):
                with lock:
                    order.append(name)
                time.sleep(0.05)
                return {}
            return run

        a = manager.submit("alice", "text", work("alice"))
        b = manager.submit("bob", "text", work("bob"))
        _wait_for(manager, a)
        _wait_for(manager, b)
        self.assertEqual(order, ["alice", "bob"], "文本任务应当按提交顺序串行")
        manager.shutdown()

    def test_text_queue_is_counted(self):
        """状态栏要看到文本排队数。"""
        manager = TaskManager()
        gate = _Waiter()

        def work(task):
            gate.started.set()
            gate.release.wait(2.0)
            return {}

        first = manager.submit("alice", "text", work)
        gate.started.wait(2.0)
        manager.submit("bob", "text", lambda t: {})

        stats = manager.stats()
        self.assertGreaterEqual(stats["text_queued"], 1,
                                f"应该有排队的文本任务，实际 {stats}")

        gate.release.set()
        _wait_for(manager, first)
        manager.shutdown()


class CancelTest(unittest.TestCase):
    def test_cancel_queued_task_never_runs(self):
        """排队中取消：连 work 都不该执行。"""
        manager = TaskManager()
        gate = _Waiter()
        ran = threading.Event()

        def blocker(task):
            gate.started.set()
            gate.release.wait(2.0)
            return {}

        first = manager.submit("alice", "text", blocker)
        gate.started.wait(2.0)

        def victim(task):
            ran.set()
            return {}

        second = manager.submit("bob", "text", victim)
        self.assertIsNotNone(second)
        manager.cancel(second.id)
        self.assertEqual(manager.get(second.id).state, TaskState.CANCELLED)

        gate.release.set()
        _wait_for(manager, first)
        time.sleep(0.15)
        self.assertFalse(ran.is_set(), "已取消的排队任务不该被执行")
        manager.shutdown()

    def test_cancel_running_task_marks_cancelled(self):
        """运行中取消：work 返回后结果作废，状态是 cancelled。"""
        manager = TaskManager()
        gate = _Waiter()

        def work(task):
            gate.started.set()
            gate.release.wait(2.0)
            return {"sent": 1}

        task = manager.submit("alice", "image", work)
        gate.started.wait(2.0)
        manager.cancel(task.id)
        gate.release.set()

        final = _wait_for(manager, task)
        self.assertEqual(final.state, TaskState.CANCELLED)
        self.assertEqual(final.result, {}, "取消的任务不能留下发送结果")
        self.assertIsNone(manager.active_for("alice"), "取消后会话要能被重新使用")
        manager.shutdown()

    def test_cancel_unknown_task_is_harmless(self):
        manager = TaskManager()
        self.assertIsNone(manager.cancel("nope"))
        manager.shutdown()


class InboundGateTest(unittest.TestCase):
    def test_admits_up_to_limit_then_drops(self):
        """对方高强度发大图时要有闸门，否则客户端越积越卡。"""
        manager = TaskManager()
        admitted = [manager.inbound_admit() for _ in range(INBOUND_LIMIT)]
        self.assertTrue(all(admitted), "上限之内都应该接纳")
        self.assertFalse(manager.inbound_admit(), "超过上限必须丢弃")
        self.assertFalse(manager.inbound_admit())

        stats = manager.stats()
        self.assertEqual(stats["inbound_dropped"], 2)
        self.assertEqual(stats["inbound_pending"], INBOUND_LIMIT)
        manager.shutdown()

    def test_finishing_frees_a_slot(self):
        """消费掉一条之后，新消息要能进来 —— 否则一次洪峰就永久卡死。"""
        manager = TaskManager()
        for _ in range(INBOUND_LIMIT):
            manager.inbound_admit()
        self.assertFalse(manager.inbound_admit())
        manager.inbound_done()
        self.assertTrue(manager.inbound_admit())
        manager.shutdown()

    def test_dropped_counter_can_be_read_and_cleared(self):
        """界面提示「已跳过 N 条」后要能清零，不然数字一直涨。"""
        manager = TaskManager()
        for _ in range(INBOUND_LIMIT):
            manager.inbound_admit()
        manager.inbound_admit()
        manager.inbound_admit()
        self.assertEqual(manager.inbound_reset_dropped(), 2)
        self.assertEqual(manager.inbound_reset_dropped(), 0)
        manager.shutdown()


class StatsTest(unittest.TestCase):
    def test_stats_reports_sending_count(self):
        manager = TaskManager()
        gate = _Waiter()

        def work(task):
            gate.started.set()
            gate.release.wait(2.0)
            return {}

        task = manager.submit("alice", "image", work)
        gate.started.wait(2.0)
        self.assertEqual(manager.stats()["sending"], 1)
        gate.release.set()
        _wait_for(manager, task)
        self.assertEqual(manager.stats()["sending"], 0)
        manager.shutdown()

    def test_handshake_counter_never_goes_negative(self):
        manager = TaskManager()
        manager.handshake_done()
        self.assertEqual(manager.stats()["handshakes"], 0)
        manager.handshake_started()
        self.assertEqual(manager.stats()["handshakes"], 1)
        manager.shutdown()


class FailureTest(unittest.TestCase):
    def test_exception_marks_task_failed_and_frees_peer(self):
        """抛异常不能让会话永久锁死 —— 否则用户再也发不出消息。"""
        manager = TaskManager()

        def boom(task):
            raise RuntimeError("故意失败")

        task = manager.submit("alice", "image", boom)
        final = _wait_for(manager, task)
        self.assertEqual(final.state, TaskState.FAILED)
        self.assertIn("故意失败", final.error)
        self.assertIsNone(manager.active_for("alice"))
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
