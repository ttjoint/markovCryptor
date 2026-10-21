"""Friend list: which name to show, and what survives a refresh."""

from __future__ import annotations

import unittest

from markovcryptor.client.directory import Directory, Friend


def _rows():
    return [
        # 真实返回的字段就这三个
        {"user_id": 93797318, "nickname": "旺鎵智能锁", "remark": ""},
        {"user_id": 3925697820, "nickname": "三马赫", "remark": "老马"},
        {"user_id": 1741473279, "nickname": "术力口宝贝", "remark": ""},
    ]


class DisplayNameTest(unittest.TestCase):
    def test_remark_wins_over_nickname(self):
        """用户设的备注才是他自己认得的名字。"""
        self.assertEqual(Friend(uin="1", nickname="三马赫", remark="老马").display_name, "老马")

    def test_nickname_used_when_no_remark(self):
        self.assertEqual(Friend(uin="1", nickname="三马赫", remark="").display_name, "三马赫")

    def test_whitespace_remark_is_not_a_name(self):
        """备注是几个空格时不能显示成空行。"""
        self.assertEqual(Friend(uin="1", nickname="三马赫", remark="   ").display_name, "三马赫")

    def test_number_falls_back(self):
        self.assertEqual(Friend(uin="42").display_name, "42")


class DirectoryTest(unittest.TestCase):
    def test_refresh_populates(self):
        d = Directory()
        d.refresh(_rows())
        self.assertEqual(len(d), 3)
        self.assertIn("3925697820", d)
        self.assertEqual(d.name_of("3925697820"), "老马")

    def test_unknown_contact_renders_as_number(self):
        """陌生人能给你发消息；不能因此显示空行。"""
        d = Directory()
        d.refresh(_rows())
        self.assertEqual(d.name_of("99999999"), "99999999")
        self.assertIsNone(d.get("99999999"))

    def test_refresh_replaces_so_deleted_friends_disappear(self):
        """不合并：刚删掉的好友不能变成鬼影行。"""
        d = Directory()
        d.refresh(_rows())
        d.refresh(_rows()[:1])
        self.assertEqual(len(d), 1)
        self.assertNotIn("3925697820", d)

    def test_order_is_by_name_then_number(self):
        d = Directory()
        d.refresh([
            {"user_id": 2, "nickname": "bbb", "remark": ""},
            {"user_id": 1, "nickname": "aaa", "remark": ""},
            {"user_id": 3, "nickname": "aaa", "remark": ""},
        ])
        self.assertEqual([f.uin for f in d.ordered()], ["1", "3", "2"])

    def test_owner_recorded(self):
        d = Directory()
        d.refresh(_rows(), owner="1741473279")
        self.assertEqual(d.owner, "1741473279")

    def test_junk_rows_are_skipped(self):
        """登录后立刻跑，一条怪记录不能拦住整个客户端启动。"""
        d = Directory()
        d.refresh([None, 42, {"nickname": "无号码"}, {"user_id": 7, "nickname": "ok"}])
        self.assertEqual(len(d), 1)
        self.assertEqual(d.name_of("7"), "ok")

    def test_clear_empties_everything(self):
        d = Directory()
        d.refresh(_rows(), owner="1")
        d.clear()
        self.assertEqual(len(d), 0)
        self.assertEqual(d.owner, "")

    def test_uins_are_strings(self):
        """线上地址一律按字符串处理，跟终端层保持一致。"""
        d = Directory()
        d.refresh([{"user_id": 3925697820, "nickname": "x", "remark": ""}])
        self.assertIn("3925697820", d)
        self.assertIsNotNone(d.get(3925697820), "数字形式也要能查到")


if __name__ == "__main__":
    unittest.main()
