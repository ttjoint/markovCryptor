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


def open_ui(url: str, *, browser: str | None = None) -> str:
    """Open ``url`` in app mode when possible. Returns what was used.

    The return value is a short label ("chrome --app", "firefox") rather than a
    path, because the caller logs it and the user does not need a path.
    """
    exe = browser or find_default_browser()
    if not exe:
        event("browser", "none_found", level=logging.WARNING, url=url)
        return "none"

    if _is_chromium(exe):
        args = [exe] + [flag.format(url=url) for flag in _APP_FLAGS]
        try:
            subprocess.Popen(args, close_fds=True)
            event("browser", "opened", mode="app", exe=exe, url=url)
            return f"{Path(exe).stem} --app"
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
        return f"{Path(exe).stem} (标签页)"
    except OSError as exc:
        event("browser", "open_failed", level=logging.ERROR, url=url, error=exc)
        return "failed"
