"""队列与磁盘的自动同步。

用户要的是「文件夹变了界面自己跟着变」，而不是点「补图」按钮。
但每 2.5 秒轮询都去 scandir 几十张图是浪费，所以先比对文件夹指纹
（mtime + 文件数），变了才真扫。

这些测试盯四件事：
* 删掉的图**必须**离开队列（否则点了发送才失败）
* 新增的图**必须**自己补进来（否则得手点按钮）
* 没变化时不能重复入队（同一张出现两遍最让人困惑）
* 临时图（拖入的）不能被当成"文件夹里没有"而删掉
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from markovcryptor.client.queue import PictureQueue


def _write(path: Path, size=(512, 512), color="white") -> None:
    Image.new("RGB", size, color).save(path, "PNG")


class SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _bump(self):
        """把文件夹 mtime 推后一点，避免同一秒内两次操作被漏检。"""
        future = time.time() + 2
        os.utime(self.folder, (future, future))

    def test_new_file_appears_without_manual_action(self):
        """在别处存了新图，界面应当自己看到 —— 不需要点「补图」。"""
        _write(self.folder / "a.png")
        queue = PictureQueue(folders=[self.folder], max_items=9)
        queue.replenish()
        queue.sync_with_disk()                 # 建立指纹
        self.assertEqual(len(queue), 1)

        _write(self.folder / "b.png")
        self._bump()
        result = queue.sync_with_disk()

        self.assertTrue(result["changed"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(queue), 2)

    def test_deleted_file_leaves_the_queue(self):
        """文件夹里删掉的图必须离开队列。

        否则用户点了发送才失败，而且不知道是哪一步错了。
        """
        _write(self.folder / "a.png")
        _write(self.folder / "b.png")
        queue = PictureQueue(folders=[self.folder], max_items=9)
        queue.replenish()
        queue.sync_with_disk()
        self.assertEqual(len(queue), 2)

        (self.folder / "b.png").unlink()
        self._bump()
        result = queue.sync_with_disk()

        self.assertEqual(result["removed"], 1)
        self.assertEqual(len(queue), 1)
        names = [item.path.name for item in queue.items]
        self.assertNotIn("b.png", names)

    def test_no_change_means_no_duplicates(self):
        """反复调用不能把同一张图重复塞进队列。

        轮询每 2.5 秒一次，重复入队会让用户看到一堆一样的缩略图。
        """
        _write(self.folder / "a.png")
        queue = PictureQueue(folders=[self.folder], max_items=9)
        queue.replenish()
        for _ in range(5):
            queue.sync_with_disk()
        self.assertEqual(len(queue), 1)
        self.assertEqual([item.path.name for item in queue.items], ["a.png"])

    def test_unchanged_folders_skip_the_scan(self):
        """指纹没变就不该报 changed，也不该做任何改动。"""
        _write(self.folder / "a.png")
        queue = PictureQueue(folders=[self.folder], max_items=9)
        queue.replenish()
        queue.sync_with_disk()
        result = queue.sync_with_disk()
        self.assertFalse(result["changed"])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["removed"], 0)

    def test_uploaded_pictures_survive_a_sync(self):
        """拖入的临时图没有磁盘对应，同步时不能被当成"已删除"清掉。

        它们是用户刚选的，清掉等于把用户的操作用静默作废。
        """
        _write(self.folder / "lib.png")
        queue = PictureQueue(folders=[self.folder], max_items=9)
        queue.replenish()
        queue.sync_with_disk()

        import io

        buffer = io.BytesIO()
        Image.new("RGB", (256, 256), "white").save(buffer, "PNG")
        queue.insert_front(buffer.getvalue(), name="dragged.png")
        self.assertEqual(len(queue), 2)

        self._bump()                            # 强制重新扫描
        queue.sync_with_disk()
        origins = [item.origin for item in queue.items]
        self.assertIn("upload", origins, "拖入的图不能被同步清掉")

    def test_total_candidates_is_reported(self):
        """状态栏要显示"文件夹里一共多少张"。"""
        for index in range(5):
            _write(self.folder / f"{index}.png")
        queue = PictureQueue(folders=[self.folder], max_items=2)
        queue.replenish()
        self.assertEqual(queue.stats()["total"], 5,
                         "总数是文件夹里的数量，不是队列长度")

    def test_folder_names_are_reported(self):
        """设置里配了哪些文件夹，状态栏要能说出来。"""
        _write(self.folder / "a.png")
        queue = PictureQueue(folders=[self.folder], max_items=4)
        queue.replenish()
        self.assertEqual(queue.stats()["folder_names"], [self.folder.name])


class SentCounterTest(unittest.TestCase):
    def test_take_increments_the_sent_counter(self):
        """已发总量是唯一的全局进度感 —— 用户要知道这次会话发过几张。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(3):
                _write(folder / f"{index}.png")
            queue = PictureQueue(folders=[folder], max_items=3)
            queue.replenish()
            self.assertEqual(queue.stats()["sent"], 0)
            queue.take()
            self.assertEqual(queue.stats()["sent"], 1)
            queue.take()
            self.assertEqual(queue.stats()["sent"], 2)

    def test_counter_survives_refills(self):
        """补图不该清零累计量 —— 那是累计值，不是队列长度。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write(folder / "a.png")
            queue = PictureQueue(folders=[folder], max_items=3)
            queue.replenish()
            queue.take()
            queue.replenish()
            self.assertEqual(queue.stats()["sent"], 1)


if __name__ == "__main__":
    unittest.main()
