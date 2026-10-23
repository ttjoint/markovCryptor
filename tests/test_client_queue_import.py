"""拖入/粘贴图片进队列的测试。

用户要能把「这次就想用」的图直接拖进来，而不是只能从文件夹里挑。
关键契约：**插队首**（不是队尾）、**零磁盘**（内存里，退出即丢）、
**不参与轮转记账**（导入过一张不该让图库某张被跳过）。
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from markovcryptor.client.queue import PictureQueue


def _png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return buffer.getvalue()


def _write(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size, "white").save(path, "PNG")


class InsertFrontTest(unittest.TestCase):
    def test_inserted_picture_lands_at_the_head(self):
        """刚拖进来的图必须在最前面 —— 这是"这次想用"的语义。"""
        queue = PictureQueue(max_items=5)
        queue.insert_front(_png((512, 512)), name="a.png")
        queue.insert_front(_png((256, 256)), name="b.png")
        names = [item.path.name for item in queue.items]
        self.assertEqual(names, ["b.png", "a.png"], "后插入的排更前")

    def test_inserted_picture_carries_capacity_and_size(self):
        """容量和尺寸要当场算好，界面才能标出"这张能装 N 字"。"""
        queue = PictureQueue(max_items=5)
        entry = queue.insert_front(_png((512, 512)), name="a.png")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.size, (512, 512))
        self.assertGreater(entry.capacity, 0)

    def test_garbage_is_rejected_not_inserted(self):
        """不是图片的字节要拒绝，而且不能进队列。"""
        queue = PictureQueue(max_items=5)
        self.assertIsNone(queue.insert_front(b"definitely not an image"))
        self.assertEqual(len(queue), 0)

    def test_origin_is_marked_as_upload(self):
        """要能区分"图库里的"和"用户拖进来的" —— 界面标注方式和轮转都不同。"""
        queue = PictureQueue(max_items=5)
        entry = queue.insert_front(_png((512, 512)), name="a.png")
        self.assertEqual(entry.origin, "upload")

    def test_tiny_picture_is_rejected(self):
        """小到装不下握手帧的图没有意义，直接拒绝。"""
        queue = PictureQueue(max_items=5)
        self.assertIsNone(queue.insert_front(_png((16, 16))))


class OverflowTest(unittest.TestCase):
    def test_overflow_evicts_folder_pictures_first(self):
        """超上限时挤掉**图库图片**，而不是刚插进来的临时图。

        反过来的话，用户拖三张图进来、前两张立刻被自己的第三张挤掉，
        看起来像导入失败。
        """
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(3):
                _write(folder / f"lib{index}.png", (512, 512))
            queue = PictureQueue(folders=[folder], max_items=3)
            queue.replenish()
            self.assertEqual(len(queue), 3)

            for index in range(2):
                queue.insert_front(_png((256, 256)), name=f"up{index}.png")

            origins = [item.origin for item in queue.items]
            self.assertEqual(origins.count("upload"), 2,
                             "导入的两张都要还在")
            self.assertEqual(len(queue.items), 3, "总长不超过上限")

    def test_all_uploads_can_evict_each_other(self):
        """全是临时图时也要有个上限，否则无限拖入会吃内存。"""
        queue = PictureQueue(max_items=2)
        for index in range(5):
            queue.insert_front(_png((256, 256)), name=f"u{index}.png")
        self.assertEqual(len(queue), 2)
        self.assertEqual([i.path.name for i in queue.items],
                         ["u4.png", "u3.png"])


class RotationBookkeepingTest(unittest.TestCase):
    def test_import_does_not_consume_a_folder_slot(self):
        """导入临时图不该影响图库的轮转记账。

        否则「导入过一张」会导致图库某张图被跳过，用户永远看不到它。
        """
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(3):
                _write(folder / f"lib{index}.png", (512, 512))
            queue = PictureQueue(folders=[folder], max_items=5,
                                 pick="sequential")
            queue.replenish()
            before = set(queue._used)

            queue.insert_front(_png((256, 256)), name="up.png")
            self.assertEqual(queue._used, before,
                             "导入临时图不能改动轮转记账")

    def test_limit_is_configurable(self):
        """上限用 max_items，不是写死的 9 —— 9 只是发送时的槽位数。"""
        queue = PictureQueue(max_items=20)
        for index in range(12):
            queue.insert_front(_png((256, 256)), name=f"u{index}.png")
        self.assertEqual(len(queue), 12, "队列本身允许超过 9 张")


if __name__ == "__main__":
    unittest.main()
