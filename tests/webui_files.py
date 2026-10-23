"""Shared helper for tests that assert on the web UI's *content*.

The UI used to be one ``index.html`` with inline ``<style>`` and ``<script>``.
It is now ``index.html`` + ``css/*.css`` + ``js/**/*.js``. Tests that check
"this rule exists" or "this JS is syntactically valid" don't care which file
the text lives in -- they care that it's somewhere in the shipped UI.

Use :func:`ui_text` for content assertions and :func:`ui_scripts` when you need
per-file boundaries (e.g. running a syntax check on each script separately).
"""

from __future__ import annotations

import re
from pathlib import Path

WEBUI = (Path(__file__).resolve().parent.parent
         / "markovcryptor" / "client" / "webui")


def ui_text() -> str:
    """All shipped UI text: HTML + every CSS + every JS, concatenated.

    Only for "does this string appear somewhere" assertions. Don't use it to
    check structure -- use ``test_client_webui.py`` for that.
    """
    if not WEBUI.is_dir():
        return ""
    parts = []
    for path in sorted(WEBUI.rglob("*")):
        if path.suffix in {".html", ".css", ".js"}:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def ui_scripts() -> dict[str, str]:
    """Every JS file in the UI, plus the module block inside index.html.

    Keys are paths relative to ``webui/``; the inline module block is keyed
    as ``index.html``. Syntax-check each value separately -- concatenating
    them would hide per-file errors.
    """
    out: dict[str, str] = {}
    if not WEBUI.is_dir():
        return out
    for path in sorted(WEBUI.rglob("*.js")):
        out[path.relative_to(WEBUI).as_posix()] = path.read_text(encoding="utf-8")
    index = WEBUI / "index.html"
    if index.is_file():
        body = index.read_text(encoding="utf-8")
        match = re.search(r'<script type="module">(.*?)</script>', body, re.S)
        if match:
            out["index.html"] = match.group(1)
    return out


def ui_stylesheets() -> dict[str, str]:
    """Every CSS file, keyed by path relative to ``webui/``."""
    if not WEBUI.is_dir():
        return {}
    return {p.relative_to(WEBUI).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(WEBUI.rglob("*.css"))}
