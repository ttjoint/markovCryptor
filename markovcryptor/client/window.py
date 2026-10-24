"""Turning the Chromium app window into a real application window.

``--app=URL`` removes the tab strip and address bar but keeps the **OS title
bar**. Users asked for that gone too: the top row of the app is our own identity
bar, and a Windows caption above it is pure noise.

There is no web API to do this. A page cannot restyle its own window, and it
certainly cannot minimise it. So we do what any native app does: find our window
by the process id we just spawned, and call the Windows API on it.

Two safety rules, both learned the hard way in other projects:

1. **Only touch a window we started.** The pid comes from the exact ``Popen`` we
   launched in ``--app`` mode. A normal tab in the user's existing browser must
   never be restyled -- it would look like the browser broke.
2. **Never assume it worked.** The window may not exist yet (Chromium takes a
   moment), the flags may differ between versions, and this whole module is a
   no-op off Windows. Every entry point here is best-effort and returns a bool.
"""

from __future__ import annotations

import logging
import sys
import time

from ..observability import event

#: Win32 style bits we remove to kill the caption and the resizable border.
_WS_CAPTION = 0x00C00000        # WS_BORDER | WS_DLGFRAME
_WS_THICKFRAME = 0x00040000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000
_WS_SYSMENU = 0x00080000
#: 必须**加上**这个位。
#:
#: 只清掉 CAPTION/THICKFRAME 是不够的：窗口仍然是 WS_OVERLAPPEDWINDOW 语义，
#: 窗口矩形比客户区大一圈（实测宽 14px、高 7px），Windows 11 的 DWM 会在那一圈
#: 画圆角和阴影 —— 用户看到的就是"外面还有一层窗体"。
#:
#: 真正的无边框窗口是 WS_POPUP：窗口矩形等于客户区，DWM 没有可装饰的东西。
#:
#: 注意写成有符号值：SetWindowLongW 的 style 参数是 LONG（32 位有符号），
#: 直接传 0x80000000（Python 里是正数 2^31）会 ArgumentError 溢出。
_WS_POPUP = -0x80000000

_GWL_STYLE = -16
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

_SW_MINIMIZE = 6
_SW_RESTORE = 9
_SW_MAXIMIZE = 3


def _user32():
    """``user32`` with the pointer-sized signatures spelled out.

    The default ``restype`` is ``c_long``, which is 32-bit even on 64-bit
    Windows and silently truncates every handle. That exact mistake already cost
    us a day on the folder picker; do not repeat it.
    """
    import ctypes
    from ctypes import wintypes

    if not hasattr(_user32, "_cached"):
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                                    wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowExW.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                               ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                     ctypes.c_long]
        u.SetWindowLongW.restype = ctypes.c_long
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        _user32._cached = (ctypes, u)
    return _user32._cached


def find_window_for_pid(pid: int, *, timeout: float = 12.0) -> int | None:
    """Wait for the top-level window owned by ``pid`` and return its handle.

    Chromium takes a moment to create its window, and in ``--app`` mode it may
    create a hidden helper window first. We poll until we see a *visible*
    top-level window belonging to that process.
    """
    if sys.platform != "win32":
        return None
    try:
        ctypes, u = _user32()
    except Exception as exc:                        # noqa: BLE001
        event("window", "user32_unavailable", level=logging.DEBUG, error=exc)
        return None

    import ctypes as _c
    from ctypes import wintypes

    found: list[int] = []

    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int

    @_c.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, _c.byref(owner))
        if owner.value != pid or not u.IsWindowVisible(hwnd):
            return True
        # Chromium 同时有 Chrome_WidgetWin_0（隐藏的辅助窗口）和
        # Chrome_WidgetWin_1（真正的顶层窗口）。按类名挑，否则可能改到一个
        # 谁也看不见的窗口，然后以为成功了。
        #
        # 用前缀而不是精确匹配：Edge/Brave/Vivaldi 都是 Chromium 内核，窗口类
        # 同样是 Chrome_WidgetWin_*，但未来某个版本可能带后缀（_2 之类）。
        name = _c.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, name, 256)
        cls = name.value
        if cls == "Chrome_WidgetWin_1" or cls.startswith("Chrome_WidgetWin_"):
            # _0 是辅助窗口，跳过；其余接受。
            if cls.endswith("_0"):
                return True
            found.append(hwnd)
            return False                            # stop enumerating
        # Edge 某些版本用这个类名
        if cls.startswith("Edge"):
            found.append(hwnd)
            return False
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found.clear()
        u.EnumWindows(_visit, 0)
        if found:
            return found[0]
        time.sleep(0.2)

    event("window", "not_found", level=logging.DEBUG, pid=pid)
    return None


def _work_area(hwnd: int) -> tuple[int, int, int, int] | None:
    """Window's monitor work area (excluding taskbar), in screen coords.

    不直接用 GetSystemMetrics：多显示器和任务栏会让它算错。
    """
    if sys.platform != "win32":
        return None
    try:
        ctypes, u = _user32()
        import ctypes.wintypes as wt

        MONITOR_DEFAULTTONEAREST = 2
        monitor = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD),
                        ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT),
                        ("dwFlags", wt.DWORD)]

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not u.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return (work.left, work.top, work.right, work.bottom)
    except Exception:                               # noqa: BLE001
        return None


def strip_frame(hwnd: int) -> bool:
    """Remove the caption and border so the app owns the whole rectangle."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        ctypes, u = _user32()
    except Exception:                               # noqa: BLE001
        return False

    style = u.GetWindowLongW(hwnd, _GWL_STYLE)
    # 去掉标题栏、可调边框、系统菜单和最小化/最大化框：
    # 这几个按钮我们自己画，留着系统的会出现两套。
    remove = (_WS_CAPTION | _WS_THICKFRAME | _WS_MINIMIZEBOX
              | _WS_MAXIMIZEBOX | _WS_SYSMENU)
    # 再加 WS_POPUP：没有它，窗口矩形永远比客户区大一圈，
    # Win11 的 DWM 就在那一圈画圆角和阴影，看起来还是"有个窗体"。
    # 用掩码做位运算时要注意符号：style 本身可能已经是负数（高位被置位）。
    base = style & ~remove
    new_style = base | _WS_POPUP

    if new_style == style:
        return True                                 # 已经是无边框了
    u.SetWindowLongW(hwnd, _GWL_STYLE, new_style)
    u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                   _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER
                   | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
    event("window", "frame_stripped", hwnd=hwnd)

    # 去掉 Windows 那层标题栏（DWM 画的圆角/阴影/系统按钮）就够了。
    #
    # --app 模式**还有**一条 Chromium 画在客户区内部的标题条（Chrome 图标 +
    # 窗口按钮，约 28px）。它不是窗口样式，GetWindowLong/SetWindowPos 都碰不到，
    # 也没有启动参数能关掉：
    #   --kiosk                               能去掉，但独占全屏、吃 Esc，
    #                                         且已有 Chrome 实例时直接不开窗
    #   --disable-features=WebAppWindowControlsOverlay   无效
    #
    # 曾用"窗口上移 28px 让那条落在屏幕外"绕过，结果挡到了用户的菜单栏，
    # 桌面其他程序看不全 —— 这个代价不能接受，已放弃。
    # 保留这条 Chromium 标题条是当前技术路线（系统浏览器 + --app）的固有代价。
    # **不要 SW_MAXIMIZE。**
    #
    # 上面刚把窗口改成 WS_POPUP（不属于任务栏、不受窗口管理器排版）。
    # 紧接着 MAXIMIZE 会让系统按"最大化"重算矩形，而 POPUP 窗口没有
    # 归属显示器，算出来的位置在某些环境（虚拟机、非 100% DPI、没有完整
    # DWM 合成）会落到屏幕外或者变成 0 尺寸 —— 用户看到的是**窗口消失了**，
    # 但进程好好活着、后端还在端口上。这正是"前端没了、黑框还在"。
    #
    # 改成 RESTORE + 明确的一次 SetWindowPos 到当前显示器工作区，
    # 位置是我们自己算的，不依赖系统对 POPUP 窗口的猜测。
    u.ShowWindow(hwnd, _SW_RESTORE)

    rect = _work_area(hwnd)
    if rect is not None:
        left, top, right, bottom = rect
        u.SetWindowPos(hwnd, 0, left, top, right - left, bottom - top,
                       _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        event("window", "placed", hwnd=hwnd,
              size=(right - left, bottom - top))

    # 兜底：确认它真的可见，不可见就退回有边框的普通窗口。
    # 装饰性的事情不能把窗口弄丢。
    if not u.IsWindowVisible(hwnd):
        event("window", "not_visible_after_strip", level=logging.WARNING,
              hwnd=hwnd)
        u.SetWindowLongW(hwnd, _GWL_STYLE, style)
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER
                       | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        u.ShowWindow(hwnd, _SW_RESTORE)
        return False

    return True


def prepare(pid: int | None, *, timeout: float = 12.0) -> int | None:
    """Find our window, strip its frame, and return the handle.

    Called in a background thread from startup: Chromium needs a moment, and
    blocking the UI on it would delay the window the user is waiting for.
    """
    if not pid:
        return None
    hwnd = find_window_for_pid(pid, timeout=timeout)
    if hwnd:
        strip_frame(hwnd)
    return hwnd


def minimize(hwnd: int | None) -> bool:
    """Minimise the app window. The page cannot do this itself."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        ctypes, u = _user32()
    except Exception:                               # noqa: BLE001
        return False
    ok = bool(u.ShowWindow(hwnd, _SW_MINIMIZE))
    event("window", "minimized", hwnd=hwnd, ok=ok)
    return ok
