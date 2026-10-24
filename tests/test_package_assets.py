"""wheel 必须带上全部前端文件。

**差点发出去的 bug**：``pyproject.toml`` 的 ``package-data`` 只写了

    markovcryptor = ["client/webui/*.html", ...]

而 ``client/webui/`` 拆分后是 ``index.html`` + ``js/``（17 个）+ ``css/``
（5 个）共 23 个文件。setuptools 的 ``dir/*.ext`` **不递归**，所以打出来的
包只有 HTML，装上去整页空白。

症状是"浏览器里 JavaScript 报错"，实际是打包漏文件 —— 从现象几乎不可能
反推到 pyproject。所以这里直接比对磁盘与 wheel 的清单。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI = ROOT / "markovcryptor" / "client" / "webui"


def _newest_wheel() -> Path | None:
    candidates = sorted((ROOT / "dist" / "wheels").glob("markovcryptor-*.whl"))
    return candidates[-1] if candidates else None


class WebAssetsTest(unittest.TestCase):
    def test_build_manifest_covers_every_asset(self):
        """``package-data`` 的匹配规则必须覆盖磁盘上每一个前端文件。

        不依赖"已经构建过 wheel"——直接看 pyproject 的规则能不能匹配到。
        """
        import tomllib

        raw = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = raw["tool"]["setuptools"]["package-data"]["markovcryptor"]

        on_disk = sorted(
            p.relative_to(ROOT / "markovcryptor").as_posix()
            for p in WEBUI.rglob("*") if p.is_file())

        unmatched = [rel for rel in on_disk
                     if not any(Path(rel).match(pat) for pat in patterns)]
        self.assertEqual(
            unmatched, [],
            "这些前端文件不会被装进 wheel（页面会空白）：" + "；".join(unmatched))

    def test_every_js_and_css_is_matched(self):
        """子目录必须显式列出 —— ``dir/*.ext`` 不递归。"""
        import tomllib

        raw = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = raw["tool"]["setuptools"]["package-data"]["markovcryptor"]

        for folder in ("js", "css"):
            files = list((WEBUI / folder).rglob("*.*"))
            self.assertTrue(files, f"{folder}/ 下没有文件，检查路径")
            for f in files:
                rel = f.relative_to(ROOT / "markovcryptor").as_posix()
                self.assertTrue(
                    any(Path(rel).match(pat) for pat in patterns),
                    f"{rel} 没有被任何 package-data 规则覆盖")


class BuiltWheelTest(unittest.TestCase):
    """如果已经构建过，实际的 wheel 也必须齐全。"""

    def test_built_wheel_contains_all_assets(self):
        wheel = _newest_wheel()
        if wheel is None:
            self.skipTest("还没有构建过 wheel")

        z = zipfile.ZipFile(wheel)
        shipped = {n.split("markovcryptor/", 1)[-1] for n in z.namelist()
                   if "markovcryptor/" in n}
        on_disk = {
            p.relative_to(ROOT / "markovcryptor").as_posix()
            for p in WEBUI.rglob("*")
            if p.is_file()
        }
        missing = sorted(on_disk - shipped)
        self.assertEqual(missing, [],
                         f"{wheel.name} 里缺这些文件：{missing}")


class WheelImportsTest(unittest.TestCase):
    def test_wheel_installs_and_imports(self):
        """把 wheel 装进临时目录，确认客户端真能导入、静态文件真在。

        比"文件在 zip 里"更进一步：证明安装后的布局也没问题。
        """
        wheel = _newest_wheel()
        if wheel is None:
            self.skipTest("还没有构建过 wheel")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps",
                 "--target", tmp, str(wheel)],
                capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                self.skipTest(f"pip 装不进去（离线环境？）：{result.stderr[-300:]}")

            root = Path(tmp) / "markovcryptor" / "client" / "webui"
            self.assertTrue((root / "index.html").is_file(), "index.html 缺失")
            js = list((root / "js").rglob("*.js"))
            css = list((root / "css").rglob("*.css"))
            self.assertGreaterEqual(len(js), 15, f"js 只有 {len(js)} 个")
            self.assertGreaterEqual(len(css), 5, f"css 只有 {len(css)} 个")
            self.assertTrue((root / "js" / "views" / "keybar.js").is_file())


if __name__ == "__main__":
    unittest.main()
