"""发送/接收任务的调度与限流。

## 为什么需要这一层

加密和隐写都是**同步、CPU 密集**的运算：Markov 文本编码约 0.6–0.75 秒，
1600×1600 图片嵌入约 3.3 秒。原来这些直接跑在 FastAPI 的 ``async def`` 里，
等于**阻塞事件循环**——期间所有轮询、其他好友的消息、握手全部停摆。

更糟的是接收侧：对方连发三张 1600 大图就能让客户端卡十秒。这不是攻击，
正常使用就会发生。

## 两条路径，两种并发手段

``to_thread`` 只对**会释放 GIL** 的代码有效：

* 图片嵌入/解码走 numpy + PIL，会在 C 层释放 GIL → 线程池真并发
* Markov 文本编解码是纯 Python 循环，**几乎不释放 GIL** → 线程再多也串行，
  而且线程间抢 GIL 会让延迟更差

所以文本编码用**单线程串行队列**（就是本模块的 ``text`` 通道），图片用线程池。
实测 800 字 0.7 秒，排 3 条 2.1 秒，配合进度提示是可以接受的。

## 按会话排队

同一好友同时点两次发送没有意义，还会让消息顺序不确定。所以每个 peer 最多
一个在途发送任务；再点就是「这个会话正在发送」，前端禁用按钮。
不同好友之间互不影响。
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..observability import event

#: 图片嵌入/解码并发数。
#:
#: 不是越大越好：1600×1600 一次要 3.3 秒，两路并行已经能吃满一个核附近。
#: 再多只会互相抢占，让每条都变慢。
IMAGE_WORKERS = 2

#: 接收队列上限。超过就丢弃并提示 —— 这是防「对方高强度发大图」的闸门。
#: 20 条按最坏情况（每张 3.3 秒）约 66 秒才能消化完，再多用户等不起。
INBOUND_LIMIT = 20


class TaskState:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """一个发送任务。前端按 id 轮询状态。"""

    id: str
    peer: str
    kind: str                       # "image" | "text" | "plain"
    state: str = TaskState.QUEUED
    progress: float = 0.0           # 0..1，仅用于展示
    note: str = ""                  # 给用户看的当前阶段
    error: str = ""
    #: 失败时对应的 HTTP 状态码。任务跑在后台线程里，没法直接抛 HTTPException；
    #: 让 work 把状态码放进这里，前端/测试才能区分「图太小(413)」和「我们崩了(502)」。
    error_status: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)
    #: 任务持有、尚未真正发出的图片（取消时归还）。
    reserved: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def as_dict(self) -> dict[str, Any]:
        return {
            "error_status": self.error_status,
            "id": self.id,
            "peer": self.peer,
            "kind": self.kind,
            "state": self.state,
            "progress": round(self.progress, 3),
            "note": self.note,
            "error": self.error,
            "result": self.result,
            "age": round(time.monotonic() - self.created_at, 2),
        }


class TaskManager:
    """发送任务的注册表 + 两条执行通道。

    * ``submit_text`` —— 单线程串行，专给 Markov 编解码
    * ``submit_image`` —— 线程池，专给图片隐写

    两个通道各自统计排队数，供底部状态栏显示。
    """

    def __init__(self, *, image_workers: int = IMAGE_WORKERS) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._by_peer: dict[str, str] = {}          # peer -> 在途任务 id
        self._text_thread: threading.Thread | None = None
        self._text_queue: list[str] = []
        self._image_pool = ThreadPoolExecutor(
            max_workers=image_workers, thread_name_prefix="stego-img")
        self._image_pending = 0
        #: 文本任务的 work 暂存（只有那条单线程读它）
        self._text_works: dict[str, Callable[[Task], dict[str, Any]]] = {}
        #: 接收侧：正在解码的图片数和已排队丢弃数
        self._inbound_pending = 0
        self._inbound_dropped = 0
        self._handshakes = 0

    # ------------------------------------------------------------ 提交

    def submit(self, peer: str, kind: str,
               work: Callable[[Task], dict[str, Any]]) -> Task | None:
        """注册一个发送任务。同一 peer 已有在途任务时返回 None。

        ``work`` 在后台线程里执行，可以读写 ``task.progress`` / ``task.note``。
        返回的字典会成为 ``task.result``。
        """
        with self._lock:
            if peer in self._by_peer:
                return None                          # 这个会话正在发
            task = Task(id=uuid.uuid4().hex[:12], peer=peer, kind=kind)
            self._tasks[task.id] = task
            self._by_peer[peer] = task.id
            if kind == "text":
                self._text_works[task.id] = work
                self._text_queue.append(task.id)
                self._ensure_text_thread()
            else:
                self._image_pending += 1
                self._image_pool.submit(self._run, task, work, "image")
        event("tasks", "submitted", task=task.id, peer=peer, kind=kind)
        return task

    def _ensure_text_thread(self) -> None:
        if self._text_thread is not None and self._text_thread.is_alive():
            return
        self._text_thread = threading.Thread(
            target=self._text_loop, name="stego-text", daemon=True)
        self._text_thread.start()

    def _text_loop(self) -> None:
        """单线程串行跑文本编码。

        Markov 编码几乎不释放 GIL，多线程只会互相抢，不如老老实实排队。
        """
        while True:
            with self._lock:
                if not self._text_queue:
                    return                           # 空闲就退出，下次再起
                task_id = self._text_queue.pop(0)
                task = self._tasks.get(task_id)
            if task is None:
                continue
            work = self._text_works.pop(task_id, None)
            if work is not None:
                self._run(task, work, "text")

    def _run(self, task: Task, work: Callable[[Task], dict[str, Any]],
             channel: str) -> None:
        try:
            with self._lock:
                if task.cancelled:
                    task.state = TaskState.CANCELLED
                    task.note = "已取消"
                    return
                task.state = TaskState.RUNNING
                task.note = "正在编码…"
            result = work(task) or {}
            with self._lock:
                if task.cancelled:
                    # 计算完了但还没发出去：结果作废，占用归还。
                    task.state = TaskState.CANCELLED
                    task.note = "已取消"
                else:
                    task.state = TaskState.DONE
                    task.note = "已发送"
                    task.progress = 1.0
                    task.result = result
        except Exception as exc:                     # noqa: BLE001 - 后台线程
            # hints 里写着"怎么办"（例如"换一张更大的图片"）。丢掉它就等于
            # 只告诉用户失败、不告诉他下一步，所以拼进错误文本。
            hints = tuple(getattr(exc, "hints", ()) or ())
            text = str(exc) or type(exc).__name__
            with self._lock:
                task.state = TaskState.FAILED
                task.error = "\n".join((text, *hints)) if hints else text
                task.note = "失败"
            event("tasks", "failed", level=40, task=task.id, error=exc)
        finally:
            with self._lock:
                if channel == "image":
                    self._image_pending = max(0, self._image_pending - 1)
                if self._by_peer.get(task.peer) == task.id:
                    del self._by_peer[task.peer]

    # ------------------------------------------------------------ 查询/取消

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def active_for(self, peer: str) -> Task | None:
        with self._lock:
            task_id = self._by_peer.get(peer)
            return self._tasks.get(task_id) if task_id else None

    def cancel(self, task_id: str) -> Task | None:
        """请求取消。真正生效在下一个取消检查点。

        没有抢占式取消：Python 线程无法安全中断。所以这里只置标志，
        ``work`` 内部在耗时步骤之间检查 ``task.cancelled``。
        """
        task = self.get(task_id)
        if task is None:
            return None
        if task.state in (TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED):
            return task
        task.cancel_event.set()
        with self._lock:
            if task.state == TaskState.QUEUED:
                task.state = TaskState.CANCELLED
                task.note = "已取消"
        event("tasks", "cancel_requested", task=task.id, peer=task.peer)
        return task

    def gc(self, *, keep: float = 120.0) -> int:
        """清掉老任务，避免注册表无限增长。"""
        now = time.monotonic()
        with self._lock:
            dead = [t.id for t in self._tasks.values()
                    if t.state in (TaskState.DONE, TaskState.FAILED,
                                   TaskState.CANCELLED)
                    and now - t.created_at > keep]
            for task_id in dead:
                del self._tasks[task_id]
        return len(dead)

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict[str, Any]:
        """底部状态栏用。全部是当前值，不是累计值。"""
        with self._lock:
            running = sum(1 for t in self._tasks.values()
                          if t.state == TaskState.RUNNING)
            queued_text = sum(1 for t in self._tasks.values()
                              if t.state == TaskState.QUEUED and t.kind == "text")
            return {
                "text_queued": queued_text,
                "image_queued": self._image_pending,
                "text_running": running if queued_text else 0,
                "inbound_pending": self._inbound_pending,
                "inbound_dropped": self._inbound_dropped,
                "handshakes": self._handshakes,
                "sending": len(self._by_peer),
            }

    # ------------------------------------------------------------ 接收侧

    def inbound_admit(self) -> bool:
        """要不要接下这张图。返回 False 表示积压太多，请丢弃。

        这是防「对方高强度发 1600 大图」的闸门：不解码、不排队、直接告诉
        用户跳过了几条，而不是让客户端越积越卡。
        """
        with self._lock:
            if self._inbound_pending >= INBOUND_LIMIT:
                self._inbound_dropped += 1
                return False
            self._inbound_pending += 1
            return True

    def inbound_done(self) -> None:
        with self._lock:
            self._inbound_pending = max(0, self._inbound_pending - 1)

    def inbound_reset_dropped(self) -> int:
        with self._lock:
            count, self._inbound_dropped = self._inbound_dropped, 0
        return count

    def handshake_started(self) -> None:
        with self._lock:
            self._handshakes += 1

    def handshake_done(self) -> None:
        with self._lock:
            self._handshakes = max(0, self._handshakes - 1)

    def shutdown(self) -> None:
        self._image_pool.shutdown(wait=False, cancel_futures=True)
