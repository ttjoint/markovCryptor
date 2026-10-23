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


def pick_folder_win32(title: str) -> str | None:
    """``SHBrowseForFolderW`` with a caller-supplied title."""
    import ctypes
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

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
