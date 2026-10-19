"""The web UI.

There is no build step and no framework, so the things that can break are:

* the page stops being served (a wrong path silently 404s and the tab is blank);
* the form generator stops understanding the schema the server sends, so options
  quietly disappear -- a failure a human notices only after embedding with the
  wrong step;
* the API contract the page assumes drifts from the real one.

The tests below cover the first two structurally and the third by asserting the
page and the API agree on the endpoint set. A real-browser check lives in
``tools/ui_smoke.py`` because it needs Chrome, which CI need not have.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import anyio
from httpx import ASGITransport, AsyncClient

from markovcryptor.engine import Engine
from markovcryptor.model import MarkovModel
from markovcryptor.web import create_app

import fixtures

UI_DIR = Path(__file__).resolve().parent.parent / "markovcryptor" / "webui"
INDEX = UI_DIR / "index.html"


def _run(coro):
    async def _await():
        return await coro

    return anyio.run(_await)


def _noise_png(size: int) -> bytes:
    """A cover the image channel accepts: flat colour is rejected as too smooth."""
    import io

    import numpy as np
    from PIL import Image

    array = np.random.default_rng(11).integers(64, 192, (size, size, 3), dtype="uint8")
    buffer = io.BytesIO()
    Image.fromarray(array, "RGB").save(buffer, "PNG")
    return buffer.getvalue()


class AssetTest(unittest.TestCase):
    def test_index_exists_and_is_single_file(self):
        self.assertTrue(INDEX.exists(), "index.html 必须存在，否则首页 404")
        extra = [p.name for p in UI_DIR.iterdir() if p != INDEX]
        self.assertEqual(extra, [], f"前端应保持单文件、无构建，发现额外文件：{extra}")

    def test_index_has_no_external_dependencies(self):
        """No CDN, no bundler output. The page must work fully offline."""
        html = INDEX.read_text(encoding="utf-8")
        for pattern in ("<script src=", "<link rel=\"stylesheet\"", "https://", "http://"):
            self.assertNotIn(pattern, html,
                             f"前端不应引用外部资源（{pattern}），否则离线不可用")


class ServingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        cls.app = create_app(engine=Engine(model=model))
        cls.client = AsyncClient(transport=ASGITransport(app=cls.app),
                                 base_url="http://test")

    def test_root_serves_the_page(self):
        response = _run(self.client.get("/"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("markovCryptor", response.text)

    def test_page_is_not_in_the_openapi_schema(self):
        """The UI is a page, not an API operation; it would only add noise."""
        schema = _run(self.client.get("/openapi.json")).json()
        self.assertNotIn("/", schema["paths"])


class ContractTest(unittest.TestCase):
    """The page hardcodes a few things; those must match the server."""

    @classmethod
    def setUpClass(cls):
        model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        cls.app = create_app(engine=Engine(model=model))
        cls.client = AsyncClient(transport=ASGITransport(app=cls.app),
                                 base_url="http://test")
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_every_endpoint_the_page_calls_exists(self):
        called = set(re.findall(r'api\("(/[a-z{}]+)', self.html))
        called |= set(re.findall(r'api\(`(/[a-z]+)', self.html))
        self.assertTrue(called, "没有找到任何接口调用，正则可能已失效")
        paths = set(_run(self.client.get("/openapi.json")).json()["paths"])
        for endpoint in called:
            # /jobs/${id} style calls appear as the bare prefix.
            matched = any(p == endpoint or p.startswith(endpoint + "/")
                          for p in paths)
            self.assertTrue(matched, f"页面调用了不存在的接口：{endpoint}")

    def test_form_generator_handles_every_schema_shape(self):
        """The generator only knows a few JSON Schema shapes.

        If a channel ever declares an option the page cannot render, the option
        would silently vanish from the form and the user would embed with a
        default they never chose. This test fails at that moment instead.
        """
        supported = {"string", "number", "integer", "boolean"}
        channels = _run(self.client.get("/channels")).json()
        for channel in channels:
            schema = (channel.get("options_schema") or {}).get("properties") or {}
            for name, spec in schema.items():
                kind = spec.get("type")
                if kind is None and "anyOf" in spec:
                    kinds = {b.get("type") for b in spec["anyOf"]} - {"null"}
                    self.assertEqual(len(kinds), 1,
                                     f"{channel['id']}.{name} 的 anyOf 太复杂，表单无法渲染")
                    kind = kinds.pop()
                self.assertIn(kind, supported,
                              f"{channel['id']}.{name} 的类型 {kind} 表单不支持")
                if kind in ("number", "integer"):
                    branch = spec.get("anyOf", [spec])[0] if "anyOf" in spec else spec
                    for bound in ("minimum", "maximum"):
                        if bound in branch:
                            self.assertIsInstance(branch[bound], (int, float))

    def test_page_knows_the_kind_mapping_for_every_channel(self):
        """`kind` decides how the server parses the upload; a wrong guess 422s."""
        ids = [c["id"] for c in _run(self.client.get("/channels")).json()]
        for channel_id in ids:
            self.assertIn(f'"{channel_id}"', self.html,
                          f"页面没有为信道 {channel_id} 定义行为")


class SchemaShapeTest(unittest.TestCase):
    """Unit-level check of the unwrap rule, independent of the server."""

    def test_nullable_number_unwraps_to_its_number_branch(self):
        spec = {"anyOf": [{"type": "number", "minimum": 8.0}, {"type": "null"}],
                "default": None}
        branch = next(b for b in spec["anyOf"] if b["type"] != "null")
        self.assertEqual(branch["type"], "number")
        self.assertEqual(branch["minimum"], 8.0)


class TerminalContractTest(unittest.TestCase):
    """The terminal tab and the /terminal/* endpoints must agree.

    These cover the pieces a typo breaks silently: an endpoint the page calls
    but the server doesn't expose, and the UI elements the JS wires to (a
    renamed id leaves the tab dead with no error anywhere).
    """

    @classmethod
    def setUpClass(cls):
        model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        cls.app = create_app(engine=Engine(model=model))
        cls.client = AsyncClient(transport=ASGITransport(app=cls.app),
                                 base_url="http://test")
        cls.html = INDEX.read_text(encoding="utf-8")

    def setUp(self):
        # 会话状态活在 app 上，用例之间会互相污染（握手过一次就一直“已建立”）。
        self.app.state.terminal_hub.reset()

    def test_terminal_endpoints_served(self):
        paths = set(_run(self.client.get("/openapi.json")).json()["paths"])
        for expected in ("/terminal/state", "/terminal/inbox/{name}",
                         "/terminal/media/{name}/{index}",
                         "/terminal/{name}/send"):
            self.assertIn(expected, paths)

    def test_page_terminal_ids_all_exist(self):
        """Every element id the JS touches must exist in the HTML.

        Covers the whole terminal tab, plaintext pane included: a renamed id
        leaves a box dead with no error anywhere.
        """
        used = set(re.findall(r'\$\("([a-z][a-z0-9-]*)"\)', self.html))
        # 只校验终端相关的前缀，其它页签有自己的用例。
        used = {name for name in used
                if name.startswith(("t-", "p-", "c-", "q-", "s-"))}
        self.assertTrue(used, "终端 JS 没有找到任何元素引用，可能被重写")
        defined = set(re.findall(r'id="([a-z][a-z0-9-]*)"', self.html))
        missing = used - defined
        self.assertEqual(missing, set(), f"JS 引用了不存在的元素 id：{sorted(missing)}")

    def test_two_panes_and_a_train_queue(self):
        """Layout contract: one plaintext pane, one covert pane, one picture train."""
        html = self.html
        # 左明文 / 右密文两窗口。
        self.assertIn('id="p-chat"', html, "缺少明文聊天窗口")
        self.assertIn('id="c-chat"', html, "缺少密文聊天窗口")
        # 明文侧随时可用：输入框不带 disabled。
        plain_input = re.search(r'<input[^>]*id="p-text"[^>]*>', html)
        self.assertIsNotNone(plain_input)
        self.assertNotIn("disabled", plain_input.group(0),
                         "明文输入框不该被禁用")
        # 密文侧未握手不可输入 —— 初始就带 disabled，由 JS 解锁。
        covert_input = re.search(r'<input[^>]*id="c-text"[^>]*>', html)
        self.assertIsNotNone(covert_input)
        self.assertIn("disabled", covert_input.group(0),
                      "密文输入框初始必须禁用")
        # 两种密文发送方式各一个按钮：文本混淆不耗图，图片混淆耗图。
        self.assertIn('id="c-send-text"', html)
        self.assertIn('id="c-send-image"', html)
        # 火车序列：待发送与已发送两栏。
        self.assertIn("待发送图片", html)
        self.assertIn("已发送", html)
        self.assertIn('id="t-queue"', html)
        self.assertIn('id="t-sent"', html)
        # 队列项：删除按钮 + 队首抽取 + 归档到已发送。
        self.assertIn("从队列移出", html, "队列项缺少移出按钮")
        self.assertIn("function tConsume", html, "缺少“从队首抽 N 张”的逻辑")
        self.assertIn("function tRenderSent", html, "缺少已发送渲染")

    def test_terminal_flow_end_to_end(self):
        """全流程：明文 → 握手 → 图片混淆 → 文本混淆，两个窗口各收各的。"""
        png = _noise_png(256)
        files = [("files", ("c.png", png, "image/png"))]

        response = _run(self.client.post("/terminal/alice/send",
                                         data={"text": "普通消息", "mode": "plain"},
                                         files=files))
        self.assertEqual(response.status_code, 200)

        # 握手也要图：从队列里拿一张。
        response = _run(self.client.post("/terminal/alice/send",
                                         data={"mode": "handshake"}, files=files))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["state_label"].startswith("已建立"))

        response = _run(self.client.post("/terminal/alice/send",
                                         data={"text": "机密", "mode": "covert-image"},
                                         files=files))
        self.assertEqual(response.status_code, 200)

        response = _run(self.client.post("/terminal/alice/send",
                                         data={"text": "文字机密", "mode": "covert-text"}))
        self.assertEqual(response.status_code, 200)

        inbox = _run(self.client.get("/terminal/inbox/bob")).json()
        covert = [s for m in inbox["messages"] for s in m["covert"]]
        # 图片混淆的可见文本原样到达聊天窗。
        chat = [m["chat_text"] for m in inbox["messages"] if m["chat_text"] == "普通消息"]
        self.assertEqual(chat, ["普通消息"])
        # 两路密文都进隐密窗，且互不混淆。
        self.assertIn("机密", covert)
        self.assertIn("文字机密", covert)

    def test_covert_requires_session(self):
        # bob 未握手就发密文 → 422，错误体带可操作建议。
        response = _run(self.client.post("/terminal/bob/send",
                                         data={"text": "x", "mode": "covert-image"},
                                         files=[("files", ("c.png", _noise_png(256),
                                                           "image/png"))]))
        self.assertEqual(response.status_code, 422)
        self.assertIn("hints", response.json())

    def test_covert_text_without_session_is_refused_honestly(self):
        """未握手时文本混淆也必须拒绝，且给出可操作建议。"""
        response = _run(self.client.post("/terminal/bob/send",
                                         data={"text": "x", "mode": "covert-text"}))
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertIn("会话", body["message"])
        self.assertTrue(body["hints"])

    def test_covert_image_without_files_is_refused(self):
        """图片混淆不给图必须拒绝：绝不自动配图。"""
        _run(self.client.post("/terminal/alice/send",
                              data={"mode": "handshake"},
                              files=[("files", ("c.png", _noise_png(200), "image/png"))]))
        response = _run(self.client.post("/terminal/alice/send",
                                         data={"text": "x", "mode": "covert-image"}))
        self.assertEqual(response.status_code, 422)
        self.assertIn("hints", response.json())

    def test_state_reports_text_channel_availability(self):
        body = _run(self.client.get("/terminal/state")).json()
        self.assertIn("text_channel_ready", body)
        self.assertIsInstance(body["text_channel_ready"], bool)

    def test_media_serves_received_image(self):
        import io
        import numpy as np
        from PIL import Image

        array = np.random.default_rng(5).integers(64, 192, (200, 200, 3), dtype="uint8")
        buffer = io.BytesIO()
        Image.fromarray(array, "RGB").save(buffer, "PNG")
        _run(self.client.post("/terminal/alice/send",
                              data={"text": "图", "mode": "plain"},
                              files=[("files", ("c.png", buffer.getvalue(), "image/png"))]))
        response = _run(self.client.get("/terminal/media/bob/0"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content[:4], b"\x89PNG")


if __name__ == "__main__":
    unittest.main()
