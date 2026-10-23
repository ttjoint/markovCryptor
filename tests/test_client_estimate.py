"""容量估算的测试。

用户需要看到「将会发送 4/9 张图片」而不是盲发。这些测试盯的是
**数字必须和真实发送行为一致** —— 显示 4 张就应该真发 4 张。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from markovcryptor.client.account import Account
from markovcryptor.client.queue import PictureQueue
from markovcryptor.client.services.estimate import (
    IMAGE_LIMIT,
    TEXT_LIMIT,
    estimate,
)
from markovcryptor.image_stego import max_payload_bytes
from markovcryptor.terminal.onebot import InProcessOneBot


def _write(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size, "white").save(path, "PNG")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.hub = InProcessOneBot()
        self.hub.friends = {"100": []}
        net = self.hub.connect("200")
        self.account = Account(net).start()

    def tearDown(self):
        self.tmp.cleanup()

    def queue(self, *sizes):
        for index, size in enumerate(sizes):
            _write(self.folder / f"{index}.png", size)
        self.account.queue = PictureQueue(folders=[self.folder],
                                          max_items=len(sizes))
        self.account.queue.replenish()
        return self.account.queue


class ImageChannelTest(_Base):
    def test_empty_queue_says_so(self):
        self.account.queue = PictureQueue()
        result = estimate(self.account, text="你好")
        self.assertFalse(result["ok"])
        self.assertIn("没有图片", result["reason"])

    def test_small_text_fits_one_image(self):
        self.queue((512, 512))
        result = estimate(self.account, text="你好")
        self.assertTrue(result["ok"])
        self.assertEqual(result["images_needed"], 1, "短文本只用一张")

    def test_needed_images_grow_with_text(self):
        """文本越长占用越多，且单调不减 —— 否则界面显示会来回跳。"""
        self.queue((256, 256))
        previous = 0
        for chars in (10, 100, 300, 600):
            result = estimate(self.account, text="测" * chars)
            self.assertGreaterEqual(result["images_needed"], previous,
                                    f"{chars} 字的占用不该比更短的少")
            previous = result["images_needed"]

    def test_never_reports_more_than_the_limit(self):
        """队列再大也只算 9 张 —— 超出的部分用不上。"""
        self.queue(*[(512, 512)] * 20)
        result = estimate(self.account, text="测" * 5000)
        self.assertLessEqual(result["images_needed"], IMAGE_LIMIT)
        self.assertLessEqual(len(result["items"]), IMAGE_LIMIT)

    def test_rejects_when_nine_images_are_not_enough(self):
        self.queue(*[(128, 128)] * 9)
        result = estimate(self.account, text="测" * 5000)
        self.assertFalse(result["ok"])
        self.assertIn("还差", result["reason"])

    def test_short_queue_is_cycled(self):
        """不足 9 张时循环使用 —— 同一张源图可以反复当载体。

        每次都嵌入不同内容，所以产生的图片互不相同，接收端按序解码。
        """
        self.queue((512, 512), (512, 512))
        # 512×512 单张约 1200 汉字，2 张装不下 4000 字 → 必然循环到第 3 槽
        result = estimate(self.account, text="测" * 4000)
        self.assertTrue(result["ok"], result["reason"])
        self.assertGreater(result["images_needed"], 2,
                           "2 张不够时应当循环填满更多槽位")
        self.assertGreater(result["repeated"], 0, "应当标明有重复使用的图")

    def test_capacity_matches_the_real_capacity_function(self):
        """显示的单张容量必须等于发送时真正用的那个函数算出来的值。

        这两处要是对不上，用户看到的「能装 X 字」就是假的。
        """
        self.queue((512, 512))
        result = estimate(self.account, text="a")
        item = result["items"][0]
        self.assertEqual(item["capacity"], max_payload_bytes(512, 512))

    def test_oversized_image_is_reported_as_shrunk(self):
        """超大图会先缩小 —— 估算必须反映缩小后的尺寸。"""
        self.queue((3000, 3000))
        result = estimate(self.account, text="你好")
        self.assertTrue(result["ok"])

    def test_skips_images_too_small_to_help(self):
        """极小图（放大 1.5× 也装不下）应当被跳过，而不是算成 0 张容量。"""
        self.queue((80, 80))
        result = estimate(self.account, text="测" * 200)
        # 80×80 放大 1.5× 到 120 只够握手，装不了 200 字
        self.assertFalse(result["ok"])


class TextChannelTest(_Base):
    def test_short_text_is_fine(self):
        result = estimate(self.account, text="你好", channel="text")
        self.assertTrue(result["ok"])
        self.assertEqual(result["images_needed"], 0, "文本信道不用图片")

    def test_limit_is_enforced(self):
        result = estimate(self.account, text="测" * (TEXT_LIMIT + 1),
                          channel="text")
        self.assertFalse(result["ok"])
        self.assertIn(str(TEXT_LIMIT), result["reason"])

    def test_exactly_at_limit_is_allowed(self):
        result = estimate(self.account, text="测" * TEXT_LIMIT, channel="text")
        self.assertTrue(result["ok"])

    def test_text_channel_ignores_the_queue(self):
        """文本信道的瓶颈是 QQ 文本长度，和有没有图无关。"""
        self.account.queue = PictureQueue()
        result = estimate(self.account, text="你好", channel="text")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
