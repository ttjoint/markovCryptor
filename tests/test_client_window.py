"""去边框不能把窗口弄丢。

用户报告：**前端窗口消失了，但后端黑框还在**。

后端活着、窗口没了 —— 说明进程没崩，是窗口本身不见了。`strip_frame`
把窗口改成 ``WS_POPUP``（不属于任务栏、不受窗口管理器排版）之后紧接着
``SW_MAXIMIZE``，系统要按"最大化"重算矩形，而 POPUP 窗口没有归属显示器，
某些环境（虚拟机、非 100% DPI、没有完整 DWM 合成）算出来的位置会落到
屏幕外、或者尺寸变 0。

去边框是纯装饰。装饰性的事情绝不允许把窗口弄丢。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from markovcryptor.client import window as window_mod


class StripFrameTest(unittest.TestCase):
    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("Windows only")

    def test_never_uses_maximize(self):
        """不能再用 SW_MAXIMIZE。

        POPUP + MAXIMIZE 是"窗口消失"的直接原因：系统按最大化重算矩形，
        POPUP 窗口没有归属显示器，算出来可能是屏幕外或 0 尺寸。
        """
        import inspect

        source = inspect.getsource(window_mod.strip_frame)
        self.assertNotIn("_SW_MAXIMIZE", source,
                         "strip_frame 不该再用 SW_MAXIMIZE")

    def test_restores_when_window_turns_invisible(self):
        """改完样式如果窗口不可见，要把样式退回去。

        这条是"装饰不能弄丢窗口"的兜底：真出问题时退回有边框的普通窗口，
        用户至少能看见界面。
        """
        fake_user32 = mock.MagicMock()
        fake_user32.GetWindowLongW.return_value = 0x00CF0000   # 常见带标题栏样式
        fake_user32.IsWindowVisible.return_value = False        # 改完不可见
        fake_user32.MonitorFromWindow.return_value = 0          # 也没有显示器信息

        with mock.patch.object(window_mod, "_user32",
                               return_value=(mock.MagicMock(), fake_user32)):
            ok = window_mod.strip_frame(12345)

        self.assertFalse(ok, "不可见时必须报告失败，让调用方知道")
        # 样式写了两次：先改成 POPUP，发现不可见后又写回原样式。
        self.assertGreaterEqual(fake_user32.SetWindowLongW.call_count, 2,
                                "应当把原样式写回去")

    def test_reports_success_when_window_stays_visible(self):
        fake_user32 = mock.MagicMock()
        fake_user32.GetWindowLongW.return_value = 0x00CF0000
        fake_user32.IsWindowVisible.return_value = True

        with mock.patch.object(window_mod, "_user32",
                               return_value=(mock.MagicMock(), fake_user32)):
            ok = window_mod.strip_frame(12345)

        self.assertTrue(ok)
        self.assertTrue(fake_user32.SetWindowPos.called, "应当显式摆位置")


class WorkAreaTest(unittest.TestCase):
    def test_returns_none_when_monitor_unavailable(self):
        """拿不到显示器信息时返回 None，让调用方保持原样而不是乱摆。"""
        if sys.platform != "win32":
            self.skipTest("Windows only")
        fake_user32 = mock.MagicMock()
        fake_user32.MonitorFromWindow.return_value = 0
        with mock.patch.object(window_mod, "_user32",
                               return_value=(mock.MagicMock(), fake_user32)):
            self.assertIsNone(window_mod._work_area(1))

    def test_uses_work_area_not_full_monitor(self):
        """要用 rcWork（排除任务栏），不是 rcMonitor。"""
        import inspect

        source = inspect.getsource(window_mod._work_area)
        self.assertIn("rcWork", source)
        self.assertNotIn("info.rcMonitor", source,
                         "rcMonitor 会把窗口压到任务栏下面")


if __name__ == "__main__":
    unittest.main()
