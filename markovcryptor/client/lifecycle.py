"""把前端和后端的生命周期绑在一起。

关掉窗口后 SnowLuma 还在后台跑，是用户实际遇到的问题：它继续占着端口、
继续挂在 QQ 进程上，下次启动还可能因为端口冲突失败。用户看到的是"我关了
程序"，实际没关。

难点在于**浏览器窗口是独立进程，关掉它不会给后端任何信号**。能用的办法：

1. 页面关闭时发一个 beacon —— 简单，但浏览器崩溃、断电、强杀进程时不会发，
   于是后端永远不知道，仍然泄漏
2. 前端定时心跳，后端看门狗发现心跳停了就退出 —— 覆盖上面所有情况

这里用的是**心跳**。它把"前端还活着吗"变成一个可观察的事实，而不是依赖
一个可能永远不来的通知。

看门狗必须宽容：浏览器标签页在后台时定时器会被节流（Chrome 对后台标签可
低到每分钟一次），所以超时按**分钟**算，而不是几秒。
"""

from __future__ import annotations

import logging
import threading
import time

from ..observability import event

#: 多久没收到心跳就认定前端没了。
#:
#: 后台标签页的定时器可能被节流到 1 次/分钟，所以这个值必须显著大于 60 秒，
#: 否则用户切到别的窗口一会儿，程序就自己退了。
DEFAULT_IDLE_TIMEOUT = 150.0

#: 心跳间隔（前端用）。远小于超时，留足重试余地。
DEFAULT_BEAT_INTERVAL = 15.0


class LivenessWatchdog:
    """Tracks whether a browser is still polling us.

    Starts *disarmed*: the server can be up for a while before a browser
    connects, and killing it during that window would look like a crash. It arms
    itself on the first beat.
    """

    def __init__(self, *, timeout: float = DEFAULT_IDLE_TIMEOUT,
                 on_expire=None) -> None:
        self.timeout = timeout
        self._on_expire = on_expire
        self._last_beat = 0.0
        self._armed = False
        self._fired = False
        self._stopped = False
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 外部接口

    def beat(self) -> None:
        """前端说"我还在"。第一次心跳同时把看门狗武装起来。"""
        with self._lock:
            self._last_beat = time.monotonic()
            if not self._armed:
                self._armed = True
                event("liveness", "armed", timeout=self.timeout)

    def disarm(self) -> None:
        """Don't expire on idle. Used while a long operation owns the process."""
        with self._lock:
            self._armed = False

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    def idle_for(self) -> float:
        with self._lock:
            if not self._armed:
                return 0.0
            return time.monotonic() - self._last_beat

    # ------------------------------------------------------------------ 线程

    def start(self) -> "LivenessWatchdog":
        with self._lock:
            if self._thread is not None:
                return self
            self._thread = threading.Thread(target=self._loop,
                                            name="liveness-watchdog",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    def _loop(self) -> None:
        # 每秒看一次就够：超时是分钟级的，没必要更密。
        while True:
            with self._lock:
                if self._stopped:
                    return
                should_fire = (self._armed and not self._fired
                               and self.idle_for() >= self.timeout)
                if should_fire:
                    self._fired = True
            if should_fire:
                event("liveness", "expired",
                      idle=round(self.idle_for(), 1))
                if self._on_expire is not None:
                    try:
                        self._on_expire()
                    except Exception as exc:        # noqa: BLE001 - shutdown path
                        event("liveness", "expire_handler_failed",
                              level=logging.WARNING, error=exc)
                return
            time.sleep(1.0)


class OwnedProcesses:
    """Only the processes *we* started, so shutdown never kills a user's own.

    SnowLuma may already be running because the user launched it (or because a
    previous instance survived). Killing that on exit would be wrong: it is not
    ours, and it may be serving a QQ account we were merely borrowing.

    Also kills the whole process *tree*: ``node index.mjs`` spawns children, and
    killing only the parent leaves orphans holding the port -- which is exactly
    the symptom that made the user report this in the first place.
    """

    def __init__(self) -> None:
        self._items: list[tuple[str, object]] = []
        self._lock = threading.RLock()

    def add(self, label: str, process) -> None:
        if process is None:
            return
        with self._lock:
            self._items.append((label, process))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def labels(self) -> list[str]:
        with self._lock:
            return [label for label, _ in self._items]

    def terminate_all(self, *, grace: float = 8.0) -> list[str]:
        """Ask each owned process to stop, then insist. Returns what was killed."""
        with self._lock:
            items = list(self._items)
            self._items.clear()

        killed: list[str] = []
        for label, process in items:
            if _terminate(process, grace=grace):
                killed.append(label)
                event("lifecycle", "terminated", what=label)
        return killed


def _terminate(process, *, grace: float) -> bool:
    """Stop one process and its children. Returns True if it is gone."""
    pid = getattr(process, "pid", None)
    if pid is None:
        return False

    try:
        if process.poll() is not None:
            return False                        # 已经退出了，没什么可做
    except Exception:                           # noqa: BLE001
        pass

    # 直接强杀，不做"先温柔后强硬"。
    # 实测：Windows 上不带 /F 的 taskkill 对 node 会**返回 0 但进程还活着**，
    # 于是这里白白等满 grace 秒才升级，用户看到的是"关了很久才关掉"。
    # SnowLuma 是挂在 QQ 上的桥，没有需要优雅落盘的状态，强杀是安全的。
    _kill_tree(pid, signal_name="kill")
    try:
        process.wait(timeout=5.0)
        return True
    except Exception:                           # noqa: BLE001
        event("lifecycle", "kill_failed", pid=pid)
        return False


def _kill_tree(pid: int, *, signal_name: str) -> None:
    """Kill ``pid`` and everything it spawned.

    Windows has no process groups in the POSIX sense; ``taskkill /T`` is the
    supported way to take a tree down. On other platforms we fall back to the
    process object itself, which is enough for development.
    """
    import os
    import signal
    import subprocess
    import sys

    if sys.platform == "win32":
        flag = "/F" if signal_name == "kill" else ""
        cmd = ["taskkill", "/T", "/PID", str(pid)]
        if flag:
            cmd.insert(1, flag)
        try:
            subprocess.run(cmd, capture_output=True, timeout=15,
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
        except Exception as exc:                # noqa: BLE001
            event("lifecycle", "taskkill_failed", pid=pid, error=exc)
        return

    sig = signal.SIGKILL if signal_name == "kill" else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except OSError:
        pass


# --------------------------------------------------------------------- 端口

def find_free_port(preferred: int, *, tries: int = 30) -> int:
    """Pick a port we can actually bind.

    The client's UI port used to be a hardcoded 8741: if anything else held it,
    startup failed with a bare OSError and the user had no way forward. Now we
    start at the preferred port and walk upward until one binds.

    ``preferred=0`` means "let the OS choose", which is the right default when
    the user has not pinned anything -- but it makes the URL unpredictable, so
    callers usually pass an explicit starting point.

    We bind with SO_EXCLUSIVEADDRUSE semantics (the default on Windows for a
    fresh socket) and close immediately. That is racy in theory -- another
    process could take the port between the probe and uvicorn's bind -- but the
    window is microseconds and uvicorn still reports a clear error if it loses.
    """
    import socket

    if preferred <= 0:
        preferred = 1

    for offset in range(tries):
        candidate = preferred + offset
        if candidate > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate

    raise OSError(f"从 {preferred} 开始找了 {tries} 个端口都被占用")


def port_owner_hint(port: int) -> str:
    """Best-effort "who is holding this port" for the error message."""
    import subprocess
    import sys

    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("gbk", "replace")
    except Exception:                               # noqa: BLE001
        return ""

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, pid = parts[1], parts[-1]
        if local.endswith(f":{port}") and parts[3] == "LISTENING":
            name = _process_name(pid)
            return f"{name} (PID {pid})" if name else f"PID {pid}"
    return ""


def _process_name(pid: str) -> str:
    import subprocess
    import sys

    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("gbk", "replace")
    except Exception:                               # noqa: BLE001
        return ""
    first = out.splitlines()[0] if out.splitlines() else ""
    if first.startswith('"'):
        return first.split('","')[0].strip('"')
    return ""
