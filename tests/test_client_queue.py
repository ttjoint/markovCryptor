"""Picture queue: sampling from folders without eating them or the disk.

The properties worth testing are the ones a user would notice going wrong: a
picture silently vanishing from their folder, the queue filling with emoji too
small to hold anything, or the program reading a 3000-sticker folder into RAM.
"""

from __future__ import annotations

import tempfile
import unittest
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
