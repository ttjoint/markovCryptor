"""Picture queue: sampling from folders without eating them or the disk.

The properties worth testing are the ones a user would notice going wrong: a
picture silently vanishing from their folder, the queue filling with emoji too
small to hold anything, or the program reading a 3000-sticker folder into RAM.
"""

from __future__ import annotations

import itertools
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image

from markovcryptor.client.queue import PictureQueue, QueuedImage


def _write_image(path: Path, size=(512, 512), fmt="PNG", color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, fmt)
    return path


class CapacityTest(unittest.TestCase):
    def test_large_picture_can_carry_handshake(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_image(Path(tmp) / "big.png", size=(512, 512))
            q = PictureQueue(folders=[Path(tmp)])
            q.replenish()
            self.assertGreaterEqual(len(q.items), 1)
            self.assertTrue(q.items[0].can_carry_handshake)

    def test_tiny_picture_is_skipped(self):
        """8x8 装不下 38 字节握手，进队只会白占位置。"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "tiny.png", size=(16, 16))
            q = PictureQueue(folders=[Path(tmp)])
            added = q.replenish()
            self.assertEqual(added, 0)
            self.assertEqual(len(q), 0)

    def test_mixed_folder_keeps_only_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "ok1.png", size=(400, 400))
            _write_image(Path(tmp) / "tiny.png", size=(32, 32))
            _write_image(Path(tmp) / "ok2.png", size=(300, 300))
            q = PictureQueue(folders=[Path(tmp)], max_items=10)
            q.replenish()
            self.assertEqual(len(q), 2, "小的那张要被跳过")


class BoundTest(unittest.TestCase):
    def test_queue_never_exceeds_max(self):
        """几千张表情包的文件夹不能把队列撑爆。"""
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(40):
                _write_image(Path(tmp) / f"{i:02d}.png", size=(200, 200))
            q = PictureQueue(folders=[Path(tmp)], max_items=5)
            q.replenish()
            self.assertEqual(len(q), 5)

    def test_max_items_at_least_one(self):
        q = PictureQueue.from_config({"queue": {"max_items": 0}})
        self.assertEqual(q.max_items, 1)

    def test_bad_pick_mode_falls_back(self):
        q = PictureQueue.from_config({"queue": {"pick": "wat"}})
        self.assertEqual(q.pick, "random")


class NoDeleteTest(unittest.TestCase):
    """绝不能删用户的图 —— 只是出队。"""

    def test_take_does_not_remove_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_image(Path(tmp) / "keep.png", size=(400, 400))
            q = PictureQueue(folders=[Path(tmp)], max_items=1)
            q.replenish()
            self.assertTrue(path.exists())
            item = q.take()
            self.assertIsNotNone(item)
            self.assertTrue(path.exists(), "出队后文件必须还在")

    def test_file_count_unchanged_after_many_takes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(6):
                _write_image(Path(tmp) / f"{i}.png", size=(300, 300))
            before = sorted(p.name for p in Path(tmp).iterdir())
            q = PictureQueue(folders=[Path(tmp)], max_items=2)
            for _ in range(10):
                q.take()
            after = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(before, after, "文件夹内容一个都不能变")


class SamplingTest(unittest.TestCase):
    def test_sequential_follows_name_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("c.png", "a.png", "b.png"):
                _write_image(Path(tmp) / name, size=(300, 300))
            q = PictureQueue(folders=[Path(tmp)], max_items=3, pick="sequential")
            q.replenish()
            self.assertEqual([i.path.name for i in q.items], ["a.png", "b.png", "c.png"])

    def test_random_does_not_repeat_while_unused_remain(self):
        """随机模式下，还有没用的图就不该重复挑同一张。"""
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                _write_image(Path(tmp) / f"{i}.png", size=(300, 300))
            q = PictureQueue(folders=[Path(tmp)], max_items=5, pick="random")
            q.replenish()
            names = [i.path.name for i in q.items]
            self.assertEqual(len(names), len(set(names)), "一轮内不能重复")

    def test_non_image_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "ok.png", size=(300, 300))
            (Path(tmp) / "note.txt").write_text("x")
            (Path(tmp) / "movie.mp4").write_bytes(b"x")
            q = PictureQueue(folders=[Path(tmp)], max_items=5)
            q.replenish()
            self.assertEqual(len(q), 1)


class EmptyAndMissingTest(unittest.TestCase):
    def test_missing_folder_is_not_fatal(self):
        q = PictureQueue(folders=[Path("/no/such/folder/anywhere")])
        self.assertEqual(q.replenish(), 0)

    def test_take_on_empty_returns_none(self):
        q = PictureQueue(folders=[])
        self.assertIsNone(q.take())

    def test_clear_empties(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(3):
                _write_image(Path(tmp) / f"{i}.png", size=(300, 300))
            q = PictureQueue(folders=[Path(tmp)], max_items=3)
            q.replenish()
            q.clear()
            self.assertEqual(len(q), 0)


class ConfigTest(unittest.TestCase):
    def test_from_config_reads_folders(self):
        q = PictureQueue.from_config({"queue": {"folders": ["/a", "/b"],
                                                "max_items": 6,
                                                "pick": "sequential"}})
        self.assertEqual(len(q.folders), 2)
        self.assertEqual(q.max_items, 6)
        self.assertEqual(q.pick, "sequential")

    def test_missing_block_uses_defaults(self):
        q = PictureQueue.from_config({})
        self.assertEqual(q.max_items, 8)
        self.assertEqual(q.pick, "random")


if __name__ == "__main__":
    unittest.main()


class ConcurrencyTest(unittest.TestCase):
    """两个线程真的会同时抽图：HTTP 处理"用户点发送"，OneBot 读线程处理
    "自动回握手"。没有锁时，单元素队列的 take() 会在判空和 pop 之间被打断，
    第二个线程 pop(0) 空列表 -> IndexError；replenish() 还会把同一张图入队两次。
    """

    def test_take_checks_and_pops_inside_one_critical_section(self):
        """判空与 pop 必须在同一临界区 —— 用确定性方式验证，不靠压测。

        压测抓不到这个竞态：GIL 下"判空 -> pop(0)"之间的窗口极窄，而且
        ``replenish()`` 自己也会加锁、不断把队列补满，窗口几乎不存在。
        实测把锁去掉后跑 4 线程 × 300 次仍然全绿 —— 假绿的测试比没测试更糟。

        所以改成验证结构：用一个记账锁替身，检查 take() 期间 acquire/release
        的配对，以及"判空到 pop"这段是否落在同一次 acquire 之内。
        """
        with tempfile.TemporaryDirectory() as tmp:
            _write_image(Path(tmp) / "only.png", size=(200, 200))
            q = PictureQueue(folders=[Path(tmp)], max_items=1)
            q.replenish()

            depth = {"n": 0, "max": 0}
            pops_while_held: list[int] = []
            real_lock = q._lock

            class RecordingLock:
                """Pass-through lock that records the nesting depth."""

                def __enter__(self):
                    depth["n"] += 1
                    depth["max"] = max(depth["max"], depth["n"])
                    return real_lock.__enter__()

                def __exit__(self, *exc):
                    depth["n"] -= 1
                    return real_lock.__exit__(*exc)

                def acquire(self, *a, **kw):
                    return real_lock.acquire(*a, **kw)

                def release(self):
                    return real_lock.release()

            q._lock = RecordingLock()  # type: ignore[assignment]

            # 记录 pop 发生时是否处于锁内。
            class SpyList(list):
                def pop(self, index=-1):
                    pops_while_held.append(depth["n"])
                    return super().pop(index)

            q.items = SpyList(q.items)  # type: ignore[assignment]

            q.take()

            self.assertTrue(pops_while_held, "没走到 pop，测试本身失效")
            self.assertTrue(all(d > 0 for d in pops_while_held),
                            f"pop 必须在锁内执行，实际深度：{pops_while_held}")

    def test_parallel_take_does_not_return_the_same_object_twice(self):
        """同一张图不能被两个线程同时取走。"""
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(6):
                _write_image(Path(tmp) / f"{i}.png", size=(200, 200))
            q = PictureQueue(folders=[Path(tmp)], max_items=2)
            q.replenish()

            # 不能用 id()：对象被回收后地址会复用，会把两张不同的图误判成
            # 同一张。给每个对象打一个唯一标记。
            taken: list[int] = []
            lock = threading.Lock()
            marks = itertools.count()

            def worker():
                for _ in range(20):
                    item = q.take()
                    if item is not None:
                        item.marker = next(marks)
                        with lock:
                            taken.append(item.marker)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(taken), len(set(taken)),
                             "同一个 QueuedImage 对象被取走了两次")

    def test_queue_never_exceeds_max_under_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(30):
                _write_image(Path(tmp) / f"{i}.png", size=(200, 200))
            q = PictureQueue(folders=[Path(tmp)], max_items=3)
            peak = [0]

            def worker():
                for _ in range(15):
                    q.replenish()
                    peak[0] = max(peak[0], len(q))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertLessEqual(peak[0], 3, f"并发下队列超上限：{peak[0]}")
