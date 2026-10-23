"""原生文件/文件夹选择器（Win32 shell API）。

**不能用 tkinter**：它必须拥有创建 root 的线程，而这些函数跑在
uvicorn 的工作线程上 —— ``filedialog.askdirectory()`` 会永久阻塞，
HTTP 请求再也回不来，按钮看起来像坏的。这是真实踩过的坑：
对话框不出现，也不报错。

``SHBrowseForFolder`` 没有这个限制。非 Windows 平台返回 None，
界面会退化成让用户手输路径。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...observability import event

def pick_folder(title: str) -> str | None:
    """Native folder picker, via the Win32 shell API.

    **Not tkinter.** tkinter must own the thread that created its root window,
    and this runs on a uvicorn worker thread -- so ``filedialog.askdirectory()``
    blocks forever, the HTTP request never returns, and the button looks dead.
    That was the actual bug: the dialog never appeared and nothing errored.

    ``SHBrowseForFolder`` has no such constraint. Returns ``None`` when the user
    cancels or the platform is not Windows (the UI then lets them type a path).
    """
    try:
        return pick_folder_win32(title)
    except Exception as exc:                        # noqa: BLE001 - optional
        event("client", "folder_picker_unavailable", level=logging.WARNING,
              error=exc)
        return None


def pick_file_win32(title: str, filter: str = "") -> str | None:
    """``GetOpenFileNameW`` -- same pointer caveat as the folder picker."""
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
        ]

    comdlg32 = ctypes.windll.comdlg32
    ole32 = ctypes.windll.ole32

    comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
    comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    ole32.CoInitialize(None)
    try:
        buffer = ctypes.create_unicode_buffer(4096)
        # 过滤器用  分隔，最后再补一个  结束。
        spec = filter or "所有文件 (*.*)\0*.*\0\0"
        info = OPENFILENAMEW()
        info.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        info.hwndOwner = None
        info.lpstrFilter = spec
        info.nFilterIndex = 1
        info.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        info.nMaxFile = len(buffer)
        info.lpstrTitle = title
        # OFN_FILEMUSTEXIST(0x1000) | OFN_PATHMUSTEXIST(0x800)
        info.Flags = 0x1000 | 0x800
        if not comdlg32.GetOpenFileNameW(ctypes.byref(info)):
            return None
        return buffer.value or None
    finally:
        ole32.CoUninitialize()


def pick_file(title: str, filter: str = "") -> str | None:
    """Native file picker. Returns None when cancelled or unavailable."""
    try:
        return pick_file_win32(title, filter)
    except Exception as exc:                        # noqa: BLE001 - optional
        event("client", "file_picker_unavailable", level=logging.WARNING,
              error=exc)
        return None


#: ``BROWSEINFOW`` 的模块级定义。
#:
#: 提到模块级有两个理由：一是每次调用不必重建类，二是测试要能验证
#: 字段类型和布局 —— 用户反馈的「点了没反应」正是这里声明错了。
#:
#: **pszDisplayName 必须是 ``POINTER(c_wchar)`` 而不是 ``wintypes.LPWSTR``。**
#: 这个字段是**输出缓冲**（shell 把所选文件夹的显示名写回来），而我们传进去
#: 的是 ``create_unicode_buffer(260)`` 返回的 ``c_wchar_Array_260``。用
#: LPWSTR（= ``c_wchar_p``，不可变）赋值会抛
#: ``TypeError: incompatible types, c_wchar_Array_260 instead of c_wchar_p``，
#: 而那个异常又被 pick_folder() 的兜底 except 吞成 None —— 界面表现就是
#: 「点了没反应」，且日志里什么都没有。
BROWSEINFOW_FIELDS = (
    ("hwndOwner", "HWND"),
    ("pidlRoot", "VOIDP"),
    ("pszDisplayName", "PWCHAR"),
    ("lpszTitle", "LPCWSTR"),
    ("ulFlags", "UINT"),
    ("lpfn", "VOIDP"),
    ("lParam", "LPARAM"),
    ("iImage", "INT"),
)


def _browse_info_structure():
    """Build (or return) the ``BROWSEINFOW`` class. Exposed for tests."""
    import ctypes
    from ctypes import wintypes as wt

    kind = {
        "HWND": wt.HWND, "VOIDP": ctypes.c_void_p,
        "PWCHAR": ctypes.POINTER(ctypes.c_wchar),
        "LPCWSTR": wt.LPCWSTR, "UINT": wt.UINT,
        "LPARAM": wt.LPARAM, "INT": ctypes.c_int,
    }
    if getattr(_browse_info_structure, "_cls", None) is None:
        _browse_info_structure._cls = type(
            "BROWSEINFOW", (ctypes.Structure,),
            {"_fields_": [(name, kind[spec]) for name, spec in BROWSEINFOW_FIELDS]})
    return _browse_info_structure._cls


def pick_folder_win32(title: str) -> str | None:
    """``SHBrowseForFolderW`` with a caller-supplied title."""
    import ctypes
    from ctypes import wintypes

    BROWSEINFOW = _browse_info_structure()
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    # **必须声明指针返回值。** ctypes 的默认 restype 是 c_long（32 位），
    # 64 位进程里 SHBrowseForFolderW 返回的 ITEMIDLIST* 高 32 位会被截断，
    # 再传给 SHGetPathFromIDListW 必然失败 —— 对话框弹出来了、用户也选了，
    # 但函数返回 None，界面表现为"点了没反应"。这是实际反馈的 bug。
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p,
                                             wintypes.LPWSTR]
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    # 对话框要求当前线程已初始化 COM；uvicorn 的工作线程通常没有。
    ole32.CoInitialize(None)
    try:
        display = ctypes.create_unicode_buffer(260)
        info = BROWSEINFOW()
        info.hwndOwner = None
        info.pidlRoot = None
        info.pszDisplayName = display
        info.lpszTitle = title
        # BIF_RETURNONLYFSDIRS(0x1) | BIF_NEWDIALOGSTYLE(0x40)
        # 新版对话框才有可输入路径的地址栏，老式那个很难用。
        info.ulFlags = 0x1 | 0x40
        info.lpfn = None
        info.lParam = 0
        info.iImage = 0

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return None                         # 用户取消

        try:
            path = ctypes.create_unicode_buffer(1024)
            if not shell32.SHGetPathFromIDListW(pidl, path):
                return None
            return path.value or None
        finally:
            # 不释放这块 pidl 就是内存泄漏 —— 每次点一次按钮漏一点。
            ole32.CoTaskMemFree(pidl)
    finally:
        ole32.CoUninitialize()
