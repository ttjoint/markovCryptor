"""Message parsing: the two kinds we render, and the honest fallback.

The real message shapes here are copied from what a live SnowLuma actually
returned (``get_friend_msg_history``), not invented, because the whole point of
this module is to survive QQ's actual variety.
"""

from __future__ import annotations

import unittest

from markovcryptor.client import messages
from markovcryptor.client.messages import describe_unsupported, parse


def _event(*segments, user_id=20001, message_id=42):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "message_id": message_id,
        "message": list(segments),
    }


def _text(value):
    return {"type": "text", "data": {"text": value}}


def _image(**data):
    base = {"url": "https://gchat.qpic.cn/a.jpg", "file": "a.jpg",
            "summary": "[图片]", "sub_type": 0}
    base.update(data)
    return {"type": "image", "data": base}


class ParseTest(unittest.TestCase):
    def test_plain_text(self):
        got = messages.parse(_event(_text("在吗")))
        self.assertEqual(got.text, "在吗")
        self.assertEqual(got.sender, "20001")
        self.assertEqual(got.unsupported, ())
        self.assertTrue(got.has_content)

    def test_text_segments_are_joined_in_order(self):
        """QQ 会把一条消息拆成多个 text 段；拼起来才是人看到的那句话。"""
        got = messages.parse(_event(_text("今晚"), _text("八点"), _text("老地方")))
        self.assertEqual(got.text, "今晚八点老地方")

    def test_image_records_survive_undownloaded(self):
        """这里只认出"有几张图"，下载交给网络层 —— 它才有 token 和超时。"""
        got = messages.parse(_event(_image(), _image(url="https://x/b.png")))
        self.assertEqual(len(got.images), 2)
        self.assertEqual(got.images[0].locate(), "https://gchat.qpic.cn/a.jpg")
        self.assertEqual(got.images[1].locate(), "https://x/b.png")

    def test_text_plus_many_images_stays_one_message(self):
        """一条 QQ 消息可以同时带文字和多张图，不能拆成 n 条。"""
        got = messages.parse(_event(_text("看这个"), _image(), _image(), _image()))
        self.assertEqual(got.text, "看这个")
        self.assertEqual(len(got.images), 3)
        self.assertEqual(got.unsupported, ())

    def test_sticker_is_flagged(self):
        """表情包通常装不下握手报文，预先标出来才能跳过。"""
        got = messages.parse(_event(_image(sub_type=1, summary="[动画表情]")))
        self.assertTrue(got.images[0].is_sticker)

    def test_image_without_url_uses_file(self):
        """有些实现只给 file，且 file 本身就是链接。"""
        got = messages.parse(_event(_image(url=None, file="https://y/c.png")))
        self.assertEqual(got.images[0].locate(), "https://y/c.png")

    def test_image_with_no_fetchable_handle_is_kept(self):
        """取不到链接也要记录存在，界面至少能说"对方发了张图"。"""
        got = messages.parse(_event(_image(url=None, file="a.jpg")))
        self.assertEqual(len(got.images), 1)
        self.assertIsNone(got.images[0].locate())


class UnsupportedTest(unittest.TestCase):
    """不解析的消息要有一行小字，而不是静默消失、也不是瞎猜内容。"""

    def test_voice_becomes_a_short_line(self):
        got = messages.parse(_event(
            {"type": "record", "data": {"file": "x.amr"}}))
        self.assertEqual(got.unsupported, ("对方发送了 语音",))
        self.assertTrue(got.only_unsupported)
        self.assertFalse(got.has_content)

    def test_shake_becomes_a_short_line(self):
        got = messages.parse(_event({"type": "shake", "data": {}}))
        self.assertIn("窗口抖动", got.unsupported[0])

    def test_forward_becomes_a_short_line(self):
        got = messages.parse(_event(
            {"type": "forward", "data": {"id": "abc"}}))
        self.assertIn("聊天记录", got.unsupported[0])

    def test_xml_card_is_not_parsed(self):
        """XML/JSON 卡片内容不猜 —— 猜错比承认不处理更糟。"""
        got = messages.parse(_event(
            {"type": "xml", "data": {"data": "<msg>一大坨</msg>"}}))
        self.assertEqual(got.unsupported, ("对方发送了 卡片消息",))

    def test_repeated_kind_is_announced_once(self):
        """连发三个表情不该刷三行一样的字。"""
        got = messages.parse(_event(
            {"type": "face", "data": {"id": "1"}},
            {"type": "face", "data": {"id": "2"}},
            {"type": "face", "data": {"id": "3"}}))
        self.assertEqual(len(got.unsupported), 1)

    def test_different_kinds_each_get_a_line(self):
        got = messages.parse(_event(
            {"type": "face", "data": {}},
            {"type": "record", "data": {}}))
        self.assertEqual(len(got.unsupported), 2)

    def test_unknown_kind_still_says_something(self):
        """没见过的新类型不能变成空白 —— 那和丢消息分不清。"""
        got = messages.parse(_event({"type": "brand_new_thing", "data": {}}))
        self.assertIn("brand_new_thing", got.unsupported[0])

    def test_mixed_text_and_unsupported_keeps_both(self):
        got = messages.parse(_event(_text("听"), {"type": "record", "data": {}}))
        self.assertEqual(got.text, "听")
        self.assertEqual(got.unsupported, ("对方发送了 语音",))
        self.assertFalse(got.only_unsupported)

    def test_markup_segments_are_silent(self):
        """reply/at 紧挨着正文，再报一行"对方发送了 回复"就是噪音。"""
        got = messages.parse(_event(
            {"type": "reply", "data": {"id": "1"}},
            {"type": "at", "data": {"qq": "1"}},
            _text("收到")))
        self.assertEqual(got.unsupported, ())
        self.assertEqual(got.text, "收到")


class RobustnessTest(unittest.TestCase):
    """解析跑在 socket 读循环上，一条畸形消息不能把连接带走。"""

    def test_string_message_format_still_works(self):
        """配置可能被手改成 messageFormat=string。"""
        got = messages.parse({"user_id": 1, "message": "纯文本形式"})
        self.assertEqual(got.text, "纯文本形式")

    def test_garbage_segments_are_skipped(self):
        got = messages.parse({"user_id": 1, "message": [
            "not-a-dict", None, 42, _text("活下来了")]})
        self.assertEqual(got.text, "活下来了")

    def test_bad_sub_type_does_not_raise(self):
        got = messages.parse(_event(_image(sub_type="not-a-number")))
        self.assertEqual(got.images[0].sub_type, 0)

    def test_empty_message_is_empty_not_an_error(self):
        got = messages.parse({"user_id": 1, "message": []})
        self.assertFalse(got.has_content)
        self.assertEqual(got.unsupported, ())


if __name__ == "__main__":
    unittest.main()


class SelfAttributionTest(unittest.TestCase):
    """未解析的消息要按**谁发的**选人称。

    QQ 会把自己发的消息回推（reportSelfMessage），历史里也两边都有。
    写死"对方发送了"会让自己的语音显示成对方发的 —— 而气泡在右边，
    自相矛盾。用户明确要求：我方发出的未能解析消息也要用我方气泡展示。
    """

    def _voice(self, sender):
        return {
            "post_type": "message", "message_type": "private",
            "user_id": 20001,
            "sender": {"user_id": sender},
            "message": [{"type": "record", "data": {"file": "a.amr"}}],
            "time": 1750000000,
        }

    def test_peer_voice_says_peer(self):
        got = parse(self._voice(20001), self_uin="10001")
        self.assertFalse(got.mine)
        self.assertEqual(got.unsupported, ("对方发送了 语音",))

    def test_own_voice_says_me(self):
        """我方发出的语音必须说"我发送了"，不能说成对方。"""
        got = parse(self._voice(10001), self_uin="10001")
        self.assertTrue(got.mine, "自己发的要标记为 mine")
        self.assertEqual(got.unsupported, ("我发送了 语音",))

    def test_without_self_uin_nothing_is_mine(self):
        """没传本机账号时不能凭空认领消息。"""
        got = parse(self._voice(10001))
        self.assertFalse(got.mine)
        self.assertEqual(got.unsupported, ("对方发送了 语音",))

    def test_describe_unsupported_picks_pronoun(self):
        self.assertEqual(describe_unsupported("record"),
                         "对方发送了 语音")
        self.assertEqual(describe_unsupported("record", mine=True),
                         "我发送了 语音")
