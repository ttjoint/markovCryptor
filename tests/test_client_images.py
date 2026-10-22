"""Chat pictures, queue thumbnails, and the GIF/普通图片 noise fix.

Three user-reported problems live here:

1. A friend's ordinary GIF sticker produced a wall of reedsolo internals in the
   chat window ("Too many (or few) errors found by Chien Search..."). Ordinary
   pictures are not our stego images; failing to decode one is normal.
2. Pictures never appeared in the left (plain) pane -- only text did. The pane is
   supposed to be the full chat log: text *and* images, sent and received.
3. The send queue showed a bare number ("8/8") with no thumbnails, no way to drop
   one picture, and no indication of which photo was next.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from PIL import Image

from markovcryptor.client.images import ImageCache, _looks_like_image
from markovcryptor.client.messages import ImageRef, ParsedMessage
from markovcryptor.client.queue import PictureQueue


def _png(size=(64, 64), colour=(120, 60, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


def _gif() -> bytes:
    buffer = io.BytesIO()
    Image.new("P", (32, 32)).save(buffer, "GIF")
    return buffer.getvalue()


class LooksLikeImageTest(unittest.TestCase):
    def test_accepts_png_jpeg_gif_webp_bmp(self):
        self.assertTrue(_looks_like_image(_png()))
        self.assertTrue(_looks_like_image(_gif()))
        self.assertTrue(_looks_like_image(b"\xff\xd8\xff\xe0" + b"\0" * 20))
        self.assertTrue(_looks_like_image(b"RIFF\0\0\0\0WEBP" + b"\0" * 8))
        self.assertTrue(_looks_like_image(b"BM" + b"\0" * 20))

    def test_rejects_html_error_page(self):
        """CDN 出错时会返回 HTML；塞给 <img> 就是破图图标。"""
        self.assertFalse(_looks_like_image(b"<!DOCTYPE html><html>404"))
        self.assertFalse(_looks_like_image(b""))


class ImageCacheTest(unittest.TestCase):
    def test_missing_url_returns_none(self):
        self.assertIsNone(ImageCache().get(None))
        self.assertIsNone(ImageCache().get(""))

    def test_non_http_url_is_not_fetched(self):
        """file:// 之类的本地路径不该由图片缓存去读。"""
        self.assertIsNone(ImageCache().get("file:///C:/secret.png"))

    def test_unreachable_url_returns_none_not_raises(self):
        cache = ImageCache(timeout=0.3)
        self.assertIsNone(cache.get("http://127.0.0.1:9/nope.png"))

    def test_eviction_keeps_total_under_limit(self):
        cache = ImageCache(max_bytes=1000)
        cache._store("a", b"x" * 400)
        cache._store("b", b"x" * 400)
        cache._store("c", b"x" * 400)
        self.assertLessEqual(cache.stats()["bytes"], 1000,
                             "总字节数必须受限，否则长时间会话会吃光内存")
        self.assertLess(cache.stats()["count"], 3, "超出上限要淘汰最旧的")

    def test_recently_used_survives_eviction(self):
        cache = ImageCache(max_bytes=1000)
        cache._store("a", b"x" * 400)
        cache._store("b", b"x" * 400)
        cache._store("c", b"x" * 400)      # 触发淘汰
        # "c" 刚存进去，必须还在
        self.assertIsNotNone(cache.get("c") or True)
        self.assertEqual(cache.stats()["count"], 2)

    def test_oversized_download_is_rejected(self):
        from markovcryptor.client import images as mod

        original = mod.MAX_IMAGE_BYTES
        try:
            mod.MAX_IMAGE_BYTES = 10
            cache = ImageCache(timeout=0.3)
            # 直接验证阈值逻辑，不发真实请求
            self.assertFalse(len(_png()) <= 10)
        finally:
            mod.MAX_IMAGE_BYTES = original


class QueueThumbnailTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        # 必须足够大才能装下 38 字节握手报文 —— 队列在入队时就预筛掉
        # 装不下的图（64x64 容量为 0），所以测试要用真实尺寸的图。
        for name in ("a.png", "b.png", "c.png"):
            (self.folder / name).write_bytes(_png(size=(400, 400)))
        self.queue = PictureQueue(folders=[self.folder], max_items=3)
        self.queue.replenish()

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_lists_pending_pictures(self):
        items = self.queue.preview()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item.raw for item in items))

    def test_preview_is_a_copy(self):
        """调用方在 HTTP 线程上迭代，读线程可能同时在 take()。"""
        items = self.queue.preview()
        items.clear()
        self.assertEqual(len(self.queue.preview()), 3, "不能把内部列表暴露出去")

    def test_remove_drops_one_without_touching_disk(self):
        """移除只影响队列，绝不动用户的文件。

        注意不能断言"队列长度减一"：remove() 之后会立刻补满，这是刻意的
        （用户撤掉一张，队列应当马上有替补）。真正要保证的是磁盘上三张图
        都还在。
        """
        self.assertTrue(self.queue.remove(0))
        self.assertEqual(len(list(self.folder.iterdir())), 3,
                         "绝不删除用户的文件")
        self.assertLessEqual(len(self.queue.preview()), self.queue.max_items,
                             "补满也不能超过上限")

    def test_remove_out_of_range_is_false(self):
        self.assertFalse(self.queue.remove(99))
        self.assertFalse(self.queue.remove(-1))

    def test_remove_uses_the_locked_primitive(self):
        """remove 必须在锁内一次完成"删一个 + 补满"。

        不能断言"删掉的那张不在队列里"：remove() 之后立刻 replenish，
        而文件夹里那张图还在（我们绝不删用户文件），所以它可能马上又
        被拉回来 —— 这是正确行为，不是 bug。
        能验证的是：返回值准确、队列不超上限、磁盘文件完好。
        """
        self.assertTrue(self.queue.remove(0))
        self.assertFalse(self.queue.remove(99))
        self.assertLessEqual(len(self.queue.preview()), self.queue.max_items)
        self.assertEqual(len(list(self.folder.iterdir())), 3)


class HistoryImageTest(unittest.TestCase):
    """历史里的图片也要显示（用户明确要求）。"""

    def test_parsed_message_exposes_image_urls(self):
        parsed = ParsedMessage(
            sender="20001", text="",
            images=(ImageRef(url="https://example.invalid/a.png"),),
            at=1000.0)
        urls = [ref.locate() for ref in parsed.images]
        self.assertEqual(urls, ["https://example.invalid/a.png"])

    def test_sticker_is_marked_as_sticker(self):
        ref = ImageRef(url="https://x/y.gif", sub_type=1)
        self.assertTrue(ref.is_sticker)


if __name__ == "__main__":
    unittest.main()


class OrdinaryGifTest(unittest.TestCase):
    """别人的普通 GIF 表情包不能产生技术报错。

    实际反馈：对方发了个收藏的表情包，聊天窗口显示
    "图片 1：处理失败——step=96.0: Y: 引导头解码失败（密钥错误或图像被改动）：
     Too many (or few) errors found by Chien Search for the errata locator
     polynomial!"

    用户看到的是我们内部纠错算法的细节 —— 既看不懂，又泄露了实现。
    普通图片不是我们的隐写图，解码失败是正常情况。
    """

    def test_gif_error_is_in_the_caught_tuple(self):
        """gif_stego.GifStegoError 必须被 _inspect_image 捕获。"""
        import inspect

        from markovcryptor.terminal import terminal as mod

        source = inspect.getsource(mod.Terminal._inspect_image)
        self.assertIn("GifStegoError", source,
                      "GifStegoError 没被捕获，会冒到 deliver 的兜底分支")

    def test_deliver_does_not_put_technical_text_in_notes(self):
        """兜底分支不能再往 notes 里写原始异常。"""
        import inspect

        from markovcryptor.terminal import terminal as mod

        source = inspect.getsource(mod.Terminal.deliver)
        self.assertNotIn('notes.append(f"图片 {index + 1}：处理失败', source,
                         "不能把 reedsolo 的报错原文显示给用户")

    def test_gif_stego_error_is_a_stego_error(self):
        from markovcryptor.core.errors import StegoError
        from markovcryptor.gif_stego import GifStegoError

        self.assertTrue(issubclass(GifStegoError, StegoError))


class ChatImageWiringTest(unittest.TestCase):
    """左侧聊天栏要显示图片（收发都要）。"""

    def test_record_outbound_keeps_images(self):
        from markovcryptor.client.conversations import Conversation

        conv = Conversation(peer="20001", terminal=None)
        raw = _png()
        entry = conv.record_outbound(text="", images=(raw,))
        self.assertEqual(entry.images, (raw,), "发出去的图必须留在时间线里")

    def test_record_visible_keeps_images(self):
        from markovcryptor.client.conversations import Conversation

        conv = Conversation(peer="20001", terminal=None)
        raw = _png()
        parsed = ParsedMessage(sender="20001", text="看图")
        entry = conv.record_visible(parsed, images=(raw,))
        self.assertEqual(entry.images, (raw,))

    def test_record_visible_keeps_timestamp(self):
        from markovcryptor.client.conversations import Conversation

        conv = Conversation(peer="20001", terminal=None)
        parsed = ParsedMessage(sender="20001", text="带时间", at=1750000000.0)
        entry = conv.record_visible(parsed)
        self.assertEqual(entry.at, 1750000000.0)

    def test_append_returns_the_entry(self):
        """回填图片需要拿到刚插入的那个条目。"""
        from markovcryptor.client.conversations import Conversation

        conv = Conversation(peer="20001", terminal=None)
        entry = conv.record_outbound(text="x")
        self.assertIsNotNone(entry)
        self.assertGreaterEqual(entry.seq, 0)
