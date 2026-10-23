"""Settings and model management endpoints.

These are the parts a normal user touches through the GUI, so the contract
matters: settings must round-trip, changes must apply without a restart, and the
destructive operation (deleting a model) must refuse anything outside the model
folder.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from httpx import ASGITransport, AsyncClient
from PIL import Image

from markovcryptor.client import config as config_mod
from markovcryptor.client.account import Account
from markovcryptor.client.app import create_client_app
from markovcryptor.client.models import ModelLibrary
from markovcryptor.client.queue import PictureQueue
from markovcryptor.terminal.onebot import InProcessOneBot

ALICE = "10001"
BOB = "20001"
ROOT = Path(__file__).resolve().parent.parent
SHIPPED_MODELS = ROOT / "models"


def _run(coro):
    return asyncio.run(coro)


def _cover(folder: Path, name="c.png"):
    path = folder / name
    Image.new("RGB", (400, 400), (70, 120, 160)).save(path, "PNG")
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self.hub = InProcessOneBot()
        self.hub.friends = {ALICE: [{"user_id": int(BOB), "nickname": "b",
                                     "remark": ""}]}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.covers = self.root / "covers"
        self.covers.mkdir()
        _cover(self.covers)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()

        # 配置文件重定向到临时目录，绝不碰真实 %APPDATA%。
        self.cfg_path = self.root / "client.json"
        patcher = mock.patch.object(config_mod, "default_config_path",
                                    return_value=self.cfg_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.net = self.hub.connect(ALICE)
        self.account = Account(self.net, queue=PictureQueue(
            folders=[self.covers], max_items=4)).start()
        self.app = create_client_app(self.account)
        self.client = AsyncClient(transport=ASGITransport(app=self.app),
                                  base_url="http://test")

    def tearDown(self):
        _run(self.client.aclose())
        # 后台加载线程可能还握着模型文件句柄，Windows 上会让 rmtree 报
        # WinError 32。先等它放手 —— 这本身也是生产里删模型会遇到的问题。
        if self.account.models is not None:
            self.account.models.wait_idle(timeout=30.0)
        self.tmp.cleanup()

    def get(self, path):
        return _run(self.client.get(path))

    def post(self, path, json=None):
        return _run(self.client.post(path, json=json))


class SettingsTest(_Base):
    def test_get_returns_current_settings(self):
        # 注意：settings 读的是**磁盘上的配置文件**，不是内存里的队列对象。
        # 两者不同源是刻意的 —— 配置是持久的，队列是运行期的。
        body = self.get("/client/settings").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["max_queue"], 8, "没有配置文件时是默认值")
        self.assertIn(str(self.cfg_path), body["path"],
                      "要告诉用户配置文件在哪")

    def test_save_round_trips(self):
        r = self.post("/client/settings", json={"max_queue": 6,
                                                "pick": "sequential"})
        self.assertEqual(r.status_code, 200)
        body = self.get("/client/settings").json()
        self.assertEqual(body["max_queue"], 6)
        self.assertEqual(body["pick"], "sequential")

    def test_cover_folder_change_applies_without_restart(self):
        """换图片文件夹必须立刻生效 —— 这是新用户最常做的第一件事。"""
        other = self.root / "other"
        other.mkdir()
        _cover(other, "x.png")
        r = self.post("/client/settings",
                      json={"cover_folders": [str(other)]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.account.queue.folders, [other])
        self.assertGreaterEqual(len(self.account.queue), 1,
                                "新文件夹的图应当已经入队")

    def test_bad_pick_mode_is_rejected(self):
        r = self.post("/client/settings", json={"pick": "wat"})
        self.assertEqual(r.status_code, 422)

    def test_bad_max_queue_is_rejected(self):
        r = self.post("/client/settings", json={"max_queue": "abc"})
        self.assertEqual(r.status_code, 422)

    def test_cover_folders_must_be_a_list(self):
        r = self.post("/client/settings", json={"cover_folders": "not-a-list"})
        self.assertEqual(r.status_code, 422)

    def test_settings_are_persisted_to_disk(self):
        self.post("/client/settings", json={"max_queue": 7})
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["queue"]["max_items"], 7)


class PickFolderTest(_Base):
    def test_cancelled_picker_reports_cancelled(self):
        with mock.patch("markovcryptor.client.services.picker.pick_folder",
                        return_value=None):
            body = self.post("/client/settings/pick-folder",
                             json={"title": "x"}).json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["cancelled"])

    def test_chosen_path_is_returned(self):
        with mock.patch("markovcryptor.client.services.picker.pick_folder",
                        return_value=r"D:\Pics"):
            body = self.post("/client/settings/pick-folder", json={}).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["path"], r"D:\Pics")

    def test_missing_tkinter_does_not_break_settings(self):
        """没有图形环境时选择框要优雅降级，不能把设置页整个弄坏。"""
        with mock.patch.dict("sys.modules", {"tkinter": None}):
            from markovcryptor.client.services.picker import pick_folder as _pick_folder
            self.assertIsNone(_pick_folder("x"))


class ModelManagementTest(_Base):
    def setUp(self):
        super().setUp()
        self.account.models = ModelLibrary(self.models_dir)
        self.account.models.scan()

    def test_import_copies_a_model_in(self):
        """导入只看"文件存在且能复制"，不在这里校验是不是真模型 ——
        加载失败会在模型列表里显示为"加载失败"，那才是该报错的地方。"""
        src = self.root / "incoming.json"
        src.write_text('{"format": "markovcryptor-model-v3"}', encoding="utf-8")
        r = self.post("/client/models/import", json={"path": str(src)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((self.models_dir / src.name).exists())

    def test_import_missing_file_is_404(self):
        r = self.post("/client/models/import",
                      json={"path": str(self.root / "nope.json")})
        self.assertEqual(r.status_code, 404)

    def test_import_without_model_folder_is_409(self):
        """没有模型目录时，即使文件存在也要明确说"先配目录"。"""
        real = self.root / "real.json"
        real.write_text("{}")
        self.account.models = None
        r = self.post("/client/models/import", json={"path": str(real)})
        self.assertEqual(r.status_code, 409)
        self.assertIn("模型目录", r.json()["detail"])

    def test_delete_removes_only_from_model_folder(self):
        if not SHIPPED_MODELS.is_dir() or not any(SHIPPED_MODELS.glob("*.json")):
            self.skipTest("仓库里没有模型文件")
        src = sorted(SHIPPED_MODELS.glob("*.json"))[0]
        self.post("/client/models/import", json={"path": str(src)})
        self.account.models.scan()
        self.account.models.load_all()
        r = self.post("/client/models/delete", json={"name": src.stem})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((self.models_dir / src.name).exists())

    def test_delete_refuses_paths_outside_the_model_folder(self):
        """本地 API 不能因为界面要求就删任意路径。"""
        outside = self.root / "precious.json"
        outside.write_text("{}")
        lib = ModelLibrary(self.models_dir)
        lib.scan()
        # 手工塞一个指向外部的 entry，模拟被篡改的请求
        from markovcryptor.client.models import ModelEntry
        lib._entries.append(ModelEntry(path=outside, name="precious"))
        self.account.models = lib
        r = self.post("/client/models/delete", json={"name": "precious"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(outside.exists(), "目录外的文件绝不能被删")

    def test_train_creates_a_model_from_a_corpus(self):
        # 训练要求至少 64 个不同汉字，所以语料要够长够杂。
        # 这不是测试的取巧，是模型的真实约束（vocabulary_size 下限 64）。
        text = ("山中何所有岭上多白云只可自怡悦不堪持赠君松下问童子言师采药去"
                "只在此山中云深不知处千山鸟飞绝万径人踪灭孤舟蓑笠翁独钓寒江雪"
                "白日依山尽黄河入海流欲穷千里目更上一层楼春眠不觉晓处处闻啼鸟"
                "夜来风雨声花落知多少床前明月光疑是地上霜举头望明月低头思故乡"
                "锄禾日当午汗滴禾下土谁知盘中餐粒粒皆辛苦")
        corpus = self.root / "corpus.txt"
        corpus.write_text(text * 20, encoding="utf-8")
        r = self.post("/client/models/train",
                      json={"path": str(corpus), "name": "小模型",
                            "vocabulary_size": 128})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((self.models_dir / "小模型.json").exists())

    def test_train_missing_corpus_is_404(self):
        r = self.post("/client/models/train",
                      json={"path": str(self.root / "nope.txt")})
        self.assertEqual(r.status_code, 404)

    def test_train_without_model_folder_is_409(self):
        real = self.root / "real.txt"
        real.write_text("山中何所有")
        self.account.models = None
        r = self.post("/client/models/train", json={"path": str(real)})
        self.assertEqual(r.status_code, 409)
        self.assertIn("模型目录", r.json()["detail"])

    def test_export_copies_out(self):
        if not SHIPPED_MODELS.is_dir() or not any(SHIPPED_MODELS.glob("*.json")):
            self.skipTest("仓库里没有模型文件")
        src = sorted(SHIPPED_MODELS.glob("*.json"))[0]
        self.post("/client/models/import", json={"path": str(src)})
        self.account.models.scan()
        self.account.models.load_all()
        out = self.root / "out"
        out.mkdir()
        r = self.post("/client/models/export",
                      json={"name": src.stem, "path": str(out)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((out / src.name).exists())


class ModelFolderSettingTest(_Base):
    def test_setting_model_folder_creates_a_library(self):
        r = self.post("/client/settings",
                      json={"model_folder": str(self.models_dir)})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(self.account.models)
        self.assertEqual(self.account.models.folder, self.models_dir)

    def test_clearing_model_folder_disables_text_channel(self):
        self.post("/client/settings",
                  json={"model_folder": str(self.models_dir)})
        self.post("/client/settings", json={"model_folder": ""})
        self.assertIsNone(self.account.models)
        self.assertFalse(self.account.text_channel_usable)


if __name__ == "__main__":
    unittest.main()


class DeleteWhileLoadingTest(_Base):
    """训练完立刻删，是用户真会做的事。

    后台加载线程握着文件句柄时，Windows 上 unlink 会 WinError 32。删除前必须
    等加载放手，否则用户看到的是一句莫名其妙的"删除失败"。
    """

    def test_delete_waits_for_the_background_loader(self):
        if not SHIPPED_MODELS.is_dir() or not any(SHIPPED_MODELS.glob("*.json")):
            self.skipTest("仓库里没有模型文件")
        src = sorted(SHIPPED_MODELS.glob("*.json"))[0]
        import shutil as _sh
        target = self.models_dir / src.name
        _sh.copy2(src, target)

        lib = ModelLibrary(self.models_dir)
        lib.scan()
        lib.load_in_background()        # 故意让加载与删除重叠
        self.account.models = lib

        r = self.post("/client/models/delete", json={"name": target.stem})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(target.exists())


class FolderPickerTest(unittest.TestCase):
    """选择文件夹按钮必须能真的返回，不能永远挂着。

    实际 bug：原来用 tkinter 的 askdirectory，它要求调用方是创建 Tk 根窗口的
    那个线程。这个函数跑在 uvicorn 的工作线程里，于是对话框永远不返回、
    HTTP 请求一直挂着 —— 用户看到的就是"按钮无效"，而且没有任何报错。
    """

    def test_picker_returns_instead_of_blocking(self):
        """在工作线程里调用，必须在几秒内返回（不要求真的弹出对话框）。"""
        import threading
        import time

        from markovcryptor.client.services import picker as picker_mod

        done = {}
        started = time.monotonic()

        def worker():
            done["value"] = picker_mod.pick_folder("测试")
            done["elapsed"] = time.monotonic() - started

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=10.0)

        self.assertFalse(thread.is_alive(),
                         "选择对话框阻塞了 —— 界面会表现为按钮无效")
        self.assertIn("value", done)
        self.assertLess(done["elapsed"], 10.0)

    def test_picker_does_not_use_tkinter(self):
        """tkinter 的对话框依赖主线程，不能用在服务端工作线程里。"""
        import inspect

        from markovcryptor.client.services import picker as picker_mod

        source = inspect.getsource(picker_mod.pick_folder)
        helper = inspect.getsource(picker_mod.pick_folder_win32)
        # 只看代码，不看注释/文档字符串 —— 文档里正好在解释"为什么不用 tkinter"。
        code = chr(10).join(
            line for line in (source + helper).splitlines()
            if not line.lstrip().startswith("#")
            and not line.lstrip().startswith('"')
            and "``" not in line)
        self.assertNotIn("askdirectory(", code,
                         "不能用 tkinter 的 askdirectory")
        self.assertNotIn("import tkinter", code, "不能引入 tkinter")
        self.assertIn("SHBrowseForFolder", helper,
                      "应当用 Win32 的 SHBrowseForFolder")

    def test_picker_failure_is_none_not_an_exception(self):
        """拿不到对话框要返回 None，让界面退回手填路径。"""
        from unittest import mock

        from markovcryptor.client.services import picker as picker_mod

        with mock.patch.object(picker_mod, "pick_folder_win32",
                               side_effect=OSError("no shell")):
            self.assertIsNone(picker_mod.pick_folder("x"))


class ModelUploadTest(_Base):
    """把 .json 拖进窗口就能装模型。

    用户要求：拖进来 → 识别是文本模型 → 直接加载 → 存进运行路径下的默认
    模型文件夹。四步操作（设置→选目录→拷文件→重新扫描）压成一步。
    """

    def _real_model_bytes(self) -> bytes:
        root = Path(__file__).resolve().parent.parent / "models"
        if not root.is_dir():
            self.skipTest("仓库里没有模型文件")
        files = sorted(root.glob("*.json"))
        if not files:
            self.skipTest("仓库里没有模型文件")
        return files[0].read_bytes()

    def _payload(self, raw: bytes, name="拖进来的.json"):
        import base64
        return {"name": name,
                "data": "data:application/json;base64,"
                        + base64.b64encode(raw).decode("ascii")}

    def test_valid_model_is_saved_and_loaded(self):
        raw = self._real_model_bytes()
        target = self.root / "defaultmodels"
        from markovcryptor.client import models as models_mod

        with mock.patch.object(models_mod, "default_model_folder",
                               return_value=target):
            r = self.post("/client/models/upload", json=self._payload(raw))

        self.assertEqual(r.status_code, 200, r.text)
        saved = Path(r.json()["path"])
        self.assertTrue(saved.exists(), "必须真的落盘，否则重启就没了")
        self.assertEqual(saved.read_bytes(), raw, "内容要原样保存")
        self.assertEqual(saved.parent, target, "要存进默认模型目录")
        self.assertIsNotNone(self.account.models, "应当建立模型库")
        self.assertEqual(self.account.models.folder, target)

    def test_invalid_json_is_rejected_and_not_saved(self):
        """不是模型就明确报错，不能悄悄存进去变成"加载失败"。"""
        target = self.root / "defaultmodels"
        from markovcryptor.client import models as models_mod

        with mock.patch.object(models_mod, "default_model_folder",
                               return_value=target):
            r = self.post("/client/models/upload",
                          json=self._payload(b'{"hello": "world"}'))

        self.assertEqual(r.status_code, 422)
        self.assertIn("模型", r.json()["detail"])
        self.assertFalse(target.exists() and any(target.glob("*.json")),
                         "校验失败的文件不该落盘")

    def test_path_traversal_in_name_is_defused(self):
        """文件名里带 ../ 不能写到目录外面去。"""
        raw = self._real_model_bytes()
        target = self.root / "defaultmodels"
        from markovcryptor.client import models as models_mod

        with mock.patch.object(models_mod, "default_model_folder",
                               return_value=target):
            r = self.post("/client/models/upload",
                          json=self._payload(raw, name="../../evil.json"))

        self.assertEqual(r.status_code, 200, r.text)
        saved = Path(r.json()["path"])
        self.assertEqual(saved.parent, target, "只能落在模型目录里")
        self.assertNotIn("..", str(saved))

    def test_non_json_name_gets_json_suffix(self):
        raw = self._real_model_bytes()
        target = self.root / "defaultmodels"
        from markovcryptor.client import models as models_mod

        with mock.patch.object(models_mod, "default_model_folder",
                               return_value=target):
            r = self.post("/client/models/upload",
                          json=self._payload(raw, name="模型备份"))
        self.assertTrue(Path(r.json()["path"]).name.endswith(".json"))

    def test_empty_payload_is_rejected(self):
        r = self.post("/client/models/upload", json={"name": "x.json"})
        self.assertEqual(r.status_code, 422)
