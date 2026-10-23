"""原生选择器的结构体定义测试。

用户反馈：「设置界面里两个选择文件夹按钮点了无反应」。

根因不是浏览器不支持 —— 是 ``BROWSEINFOW.pszDisplayName`` 声明成了
``wintypes.LPWSTR``（``c_wchar_p``，不可变），而实际传进去的是
``create_unicode_buffer`` 返回的 ``c_wchar_Array_260``。ctypes 报
``TypeError: incompatible types``，异常又被 ``pick_folder()`` 的兜底
``except`` 吞成 ``None``，界面于是表现为"点了没反应"。

这些测试**不弹对话框**（会卡住 CI），只验证结构体字段能接住缓冲区 ——
那正是出错的那一步。
"""

from __future__ import annotations

import ctypes
import sys
import unittest

from markovcryptor.client.services import picker


class BrowseInfoLayoutTest(unittest.TestCase):
    def test_display_name_accepts_a_unicode_buffer(self):
        """pszDisplayName 必须能接住 create_unicode_buffer。

        这是**输出缓冲**（shell 把显示名写回来），不是常量字符串。
        用 LPWSTR 声明会在赋值时就抛 TypeError。
        """
        info = picker._browse_info_structure()
        buffer = ctypes.create_unicode_buffer(260)
        # 这一行就是原来报错的地方。能跑完就说明类型对了。
        info.pszDisplayName = buffer
        self.assertTrue(True, "赋值不该抛 TypeError")

    def test_structure_size_matches_the_windows_definition(self):
        """结构体布局必须和 Win32 的 BROWSEINFOW 一致。

        64 位下实测 64 字节（8×2 指针 + 指针 + 指针 + 4 + 4 填充 + 8 + 8 + 4 + 4 尾部对齐）。
        布局错了 shell 会读到垃圾，表现为"对话框不出现"或"选完返回 None"。

        注意 LONG_PTR/LPARAM 在 64 位是 8 字节，这点最容易算错。
        """
        info = picker._browse_info_structure()
        expected = 64 if sys.maxsize > 2**32 else 32
        self.assertEqual(ctypes.sizeof(info), expected,
                         f"BROWSEINFOW 应当是 {expected} 字节，"
                         f"实际 {ctypes.sizeof(info)}")

    def test_display_name_field_is_assignable_from_an_array(self):
        """直接验证字段类型：LPWSTR 会在这里抛 TypeError。

        上面的 size 测试抓不到这个 bug —— LPWSTR 和 POINTER(c_wchar) 都是
        8 字节，sizeof 完全一样。真正会崩的是**赋值那一步**。
        """
        info = picker._browse_info_structure()
        # 注意：不能读 info.pszDisplayName（未赋值时返回 None），
        # 要查**字段声明**的类型。
        declared = dict(info._fields_)["pszDisplayName"]
        self.assertIs(declared, ctypes.POINTER(ctypes.c_wchar),
                      "pszDisplayName 必须声明为 POINTER(c_wchar)，"
                      "而不是不可变的 LPCWSTR/LPWSTR")

    def test_field_order_is_the_documented_one(self):
        """字段顺序就是 API 契约，错一个后面全错。"""
        names = [name for name, _ in picker._browse_info_structure()._fields_]
        self.assertEqual(names, [
            "hwndOwner", "pidlRoot", "pszDisplayName", "lpszTitle",
            "ulFlags", "lpfn", "lParam", "iImage",
        ])


class FailureVisibilityTest(unittest.TestCase):
    def test_pick_folder_logs_instead_of_silently_returning_none(self):
        """失败要进日志。

        原来的问题是异常被吞掉、什么痕迹都不留，所以"点了没反应"这个
        现象查了很久。现在至少有一条 warning 可查。
        """
        from unittest import mock

        with mock.patch.object(picker, "pick_folder_win32",
                               side_effect=RuntimeError("模拟故障")):
            with mock.patch("markovcryptor.client.services.picker.event") as log:
                self.assertIsNone(picker.pick_folder("t"))
                log.assert_called_once()
                args, kwargs = log.call_args
                self.assertEqual(args[:2], ("client", "folder_picker_unavailable"))


if __name__ == "__main__":
    unittest.main()
