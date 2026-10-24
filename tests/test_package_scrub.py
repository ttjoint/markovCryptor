"""发布包不能夹带构建机的个人信息。

**真实事故**：0.3.0 的跨机测试包里带着

    snowluma/config/onebot_1741473279.json
      "accessToken": "9w-avp3W-sO_sgxHqld7cwqtcQ4sL5zdNDOUPolV0UA"

拿到这个 access token 就能完全控制构建机上那个 QQ 号——收发任意消息、
读取全部好友与群。另外还带着聊天数据库（``data/<QQ>/``）和明文聊天日志。

从包里**看不出**它是谁的，但它是真的能用。所以必须在打包时清掉，而不是
指望事后发现。
"""

from __future__ import annotations

import sys
import tempfile
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_bundle  # noqa: E402
import make_client_bundle  # noqa: E402


def _dirty_tree(root: Path) -> None:
    """造一个"在构建机上跑过"的 SnowLuma 目录。"""
    for name in ("config", "data", "logs", "cache", "tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "config" / "onebot_1741473279.json").write_text(
        '{"accessToken": "9w-avp3W-sO_sgxHqld7cwqtcQ4sL5zdNDOUPolV0UA"}',
        encoding="utf-8")
    (root / "config" / "webui.json").write_text(
        '{"password_hash": "abc", "salt": "def"}', encoding="utf-8")
    (root / "data" / "1741473279").mkdir()
    (root / "data" / "1741473279" / "message.db").write_text("历史")
    (root / "logs" / "snowluma-2026-10-22.log").write_text("聊天原文")
    (root / "index.mjs").write_text("// 正常文件", encoding="utf-8")


class ScrubTest(unittest.TestCase):
    """两个打包脚本都必须清干净。"""

    def _check(self, scrub) -> None:
        root = Path(tempfile.mkdtemp())
        try:
            _dirty_tree(root)
            scrub(root)

            left = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            for bad in ("onebot_", "webui.json", ".log", "message.db"):
                self.assertFalse(
                    [x for x in left if bad in x],
                    f"{bad} 还在包里：{left}")
            self.assertNotIn("data", left, "聊天数据库目录必须整个删掉")
            self.assertNotIn("logs", left, "日志目录必须整个删掉")

            # config/ 目录本身要留着：SnowLuma 没有它拒绝启动。
            self.assertIn("config", left)
            # 正常文件不能误删。
            self.assertIn("index.mjs", left)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_client_bundle_scrubs(self):
        self._check(make_client_bundle._scrub_personal_state)

    def test_testbed_bundle_scrubs(self):
        self._check(make_bundle._scrub_personal_state)


class ShippedTreeTest(unittest.TestCase):
    """已经打好的包（如果有）必须已经是干净的。

    **历史遗留**：``dist/`` 里有两个 2026-10-23 01:43 之前打出的包，那时
    清理逻辑还没加入（提交 3a8f644），它们确实带着真实 access token。
    这些是构建残留，留在磁盘上供对照，但**绝不能发布**。

    所以只检查"清理逻辑生效之后"打出来的包 —— 用目录名里的时间戳判断。
    """

    #: ``_scrub_personal_state`` 落地的时刻，比它早的包不检查（见 docstring）。
    SCRUB_BATCH = "202610230143"

    def _bucket_stamp(self, path: Path) -> str:
        """``qqclient-0.3.0-20261023-0138`` -> ``202610230138``."""
        import re
        match = re.search(r"-(\d{8})-(\d{4})$", path.name)
        return match.group(1) + match.group(2) if match else ""

    def test_no_credentials_in_dist(self):
        dist = ROOT / "dist"
        if not dist.is_dir():
            self.skipTest("还没有 dist/")

        # 只挑**真正危险**的：SnowLuma 写出来的那份账号配置和聊天日志。
        #
        # 不能按 "data/"/"logs/" 目录普通匹配 —— numpy 自己就带
        # ``numpy/lib/tests/data/``，``onebot_config.py`` 也只是源码文件名。
        # 第一版这么写过，误报了 25000 字。
        offenders = []
        for bundle in dist.rglob("*"):
            if not bundle.is_dir() or not (bundle / "manifest.json").exists():
                continue
            stamp = self._bucket_stamp(bundle)
            if not stamp or stamp < self.SCRUB_BATCH:
                continue                       # 历史遗留，不检查
            for path in bundle.rglob("*"):
                if not path.is_file():
                    continue
                name = path.name.lower()
                if (name.startswith("onebot_") and name.endswith(".json"))                         or name == "webui.json" or path.suffix == ".log":
                    offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual(offenders, [],
                         "包里有构建机的个人信息，不要发布："
                         + "；".join(offenders[:5]))


if __name__ == "__main__":
    unittest.main()
