"""Opening the UI in whatever browser the user actually uses.

A chat window should look like an application, not like a tab. Chrome and Edge
both accept ``--app=URL``, which opens a chromeless window with no address bar
and no tab strip -- indistinguishable from a native app at a glance. Firefox has
no equivalent, so it gets a normal window; the feature still works, it just looks
like a browser.

Detecting the default browser is done through the registry rather than by trying
to launch each candidate: launching a browser to find out which one is default
is both slow and rude (windows pop up).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from ..observability import event

#: Chromium-family flags. ``--app`` needs a *separate* profile directory or the
#: window joins the user's existing session and inherits its window size/state.
#:
#: This is not cosmetic. Without ``--user-data-dir`` the launched process is a
#: *launcher* that hands the URL to whatever Chrome is already running and then
#: exits -- so the window's owning pid is the user's long-running browser, and
#: there is no way to tell our window from their tabs. With a private profile
#: the process really owns its window, which is what lets us find it and strip
#: the title bar.
_APP_FLAGS = ("--app={url}", "--window-size=1280,860")


def _read_default_browser() -> str | None:
    """The executable path registered for ``http`` on Windows.

    ``HKCU`` wins over ``HKLM`` because per-user associations are what the user
    actually chose; the machine-wide entry is often just the default that shipped.
    """
    try:
        import winreg
    except ImportError:                            # not Windows
        return None

    key_path = (r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice")
    prog_id = None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key_path) as key:
                prog_id, _ = winreg.QueryValueEx(key, "ProgId")
                break
        except OSError:
            continue
    if not prog_id:
        return None

    # ProgId -> command line, e.g. "ChromeHTML" -> "...\chrome.exe" -- "%1"
    command_key = rf"Software\Classes\{prog_id}\shell\open\command"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, command_key) as key:
                command, _ = winreg.QueryValueEx(key, "")
                break
        except OSError:
            continue
    if not command:
        return None
    return _exe_from_command(command)


def _exe_from_command(command: str) -> str | None:
    """Pull the executable out of a shell command line.

    Handles the quoted form (``"C:\\...\\chrome.exe" -- "%1"``) and the bare one.
    Anything else is not worth parsing -- a wrong guess would launch the wrong
    program, so returning ``None`` is the safe answer.
    """
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 1:
            return command[1:end]
        return None
    # Unquoted: the path ends at the first space followed by a flag or "%1".
    head = command.split(" --")[0].split(" %")[0].strip()
    return head or None


def _is_chromium(exe: str) -> bool:
    name = Path(exe).name.lower()
    return name in ("chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe",
                    "chromium.exe", "opera.exe")


def find_default_browser() -> str | None:
    """Best guess at the user's default browser executable.

    Falls back to looking for Chrome/Edge on PATH and in their usual install
    locations, because a fresh profile may have no ``UserChoice`` entry yet.
    """
    exe = _read_default_browser()
    if exe and Path(exe).exists():
        return exe

    candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _profile_dir(port: int | None = None) -> str:
    """A private Chromium profile for this app.

    Lives next to the rest of our state so it survives between runs (a fresh
    profile every launch would lose window size) but is ours alone -- the user's
    own browser session is never touched.

    **每个实例一个 profile。** Chromium 的 ``--user-data-dir`` 是独占的：
    第二个进程拿着同一个目录时，会把 URL 交给已经在跑的那个实例、然后
    自己退出。后果是——

    * 用户先开一个，再开一个（或调试时另起一个），第二个窗口根本不出现，
      看起来像"127.0.0.1 拒绝连接"；
    * 更隐蔽的：先开的那个窗口内容被后来的接管，用户以为程序串了。

    端口是每个实例唯一的、稳定的标识，所以拿它做后缀。
    """
    base = os.environ.get("APPDATA") or str(Path.home())
    name = "browser-profile" if port is None else f"browser-profile-{int(port)}"
    path = Path(base) / "markovcryptor" / name
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:                          # noqa: BLE001
        event("browser", "profile_dir_failed", level=logging.WARNING,
              path=str(path), error=exc)
    return str(path)


def open_ui(url: str, *, browser: str | None = None,
            port: int | None = None) -> tuple[str, int | None]:
    """Open ``url`` in app mode when possible.

    Returns ``(label, pid)``. The label is a short string ("chrome --app") for
    logging; the pid is the browser process we spawned, or ``None`` when we did
    not spawn one (a tab handed to the shell).

    The pid matters because the window has no frame of its own accord -- we strip
    the OS title bar afterwards and need to know *which* window is ours. Only an
    app-mode window we launched ourselves can be safely touched; a normal tab in
    the user's existing browser must never be restyled.
    """
    exe = browser or find_default_browser()
    if not exe:
        event("browser", "none_found", level=logging.WARNING, url=url)
        return "none", None

    if _is_chromium(exe):
        profile = _profile_dir(port)
        args = [exe] + [flag.format(url=url) for flag in _APP_FLAGS]
        args += [f"--user-data-dir={profile}", "--no-first-run",
                 "--no-default-browser-check"]
        try:
            # **pythonw 下必须显式断开三个标准流。**
            #
            # pythonw 是 GUI 子系统程序，进程没有真正的 stdin/stdout/stderr
            # （句柄是无效值）。子进程默认继承这些句柄，Chrome 启动时会
            # 因为拿不到有效句柄而卡住 —— 表现为"客户端已启动"之后再无
            # 任何日志、窗口不出现、几秒后退出码 1。用 python.exe 跑就一切
            # 正常，所以这个 bug 只在双击 start.bat 时出现。
            proc = subprocess.Popen(args, close_fds=True,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            event("browser", "opened", mode="app", exe=exe, url=url,
                  pid=proc.pid)
            return f"{Path(exe).stem} --app", proc.pid
        except OSError as exc:
            event("browser", "app_mode_failed", level=logging.WARNING,
                  exe=exe, error=exc)
            # Fall through to a plain open rather than showing nothing.

    try:
        # Windows: the shell picks the default handler. On other platforms this
        # is the standard opener too.
        if hasattr(os, "startfile"):
            os.startfile(url)                      # noqa: S606 - intended
        else:
            subprocess.Popen(["xdg-open", url], close_fds=True)
        event("browser", "opened", mode="tab", exe=exe, url=url)
        return f"{Path(exe).stem} (标签页)", None
    except OSError as exc:
        event("browser", "open_failed", level=logging.ERROR, url=url, error=exc)
        return "failed", None
