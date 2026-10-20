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


def _noise_png(size: int, seed: int = 11) -> bytes:
    """A cover the image channel accepts: flat colour is rejected as too smooth."""
    import io

    import numpy as np
    from PIL import Image

    array = np.random.default_rng(seed).integers(64, 192, (size, size, 3), dtype="uint8")
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

    def test_hidden_attribute_actually_hides(self):
        """`hidden` 只有在没人给它写 display 的时候才管用。

        页面里 `.client` 是 grid、`.await` 是 flex —— 这些都压过 `hidden`
        自带的 UA 样式。于是 `node.hidden = true` 在 DOM 里成立、屏幕上照
        样显示。单账号部署隐藏 bob 窗口时就是这么漏的：JS 断言都对，截图
        里却有两个客户端。要么每处都补一条 `[hidden]` 规则，要么就别写
        display。这里要求存在一条带 `!important` 的兜底规则。
        """
        page = INDEX.read_text(encoding="utf-8")
        rules = re.findall(r"([^{}]*\[hidden\][^{}]*)\{([^}]*)\}", page)
        self.assertTrue(rules, "页面里一条 [hidden] 规则都没有")
        strong = [sel for sel, body in rules
                  if "display: none" in body.replace("  ", " ") and "!important" in body]
        self.assertTrue(
            strong,
            "没有一条 [hidden] 规则用 !important —— 只要元素自己写了 "
            "display:grid/flex，hidden 就会被压掉：" + repr([s for s, _ in rules]),
        )

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
        """Every element id the JS touches must exist in the HTML."""
        used = set(re.findall(r'\$\("([a-z][a-z0-9-]*)"\)', self.html))
        used = {name for name in used
                if name.startswith(("a-", "b-", "wire"))}
        self.assertTrue(used, "终端 JS 没有找到任何元素引用，可能被重写")
        # JS 里也会拼出 ``me + "-queue"`` 这类 id，补进来一起校验。
        for me in ("alice", "bob"):
            for suffix in ("plain", "covert", "plain-text", "plain-send",
                           "plain-files", "plain-preview", "covert-text",
                           "send-text", "send-image", "handshake",
                           "handshake-hint", "queue", "files", "queue-clear",
                           "sent", "sent-clear", "state",
                           "plain-peer", "covert-peer", "q-count", "q-status",
                           "s-count"):
                used.add(me[0] + "-" + suffix)
        defined = set(re.findall(r'id="([a-z][a-z0-9-]*)"', self.html))
        missing = used - defined
        self.assertEqual(missing, set(), f"JS 引用了不存在的元素 id：{sorted(missing)}")

    def test_two_clients_each_with_two_windows(self):
        """两侧各有明文窗与密文窗，一共四个窗，无需切换身份。"""
        html = self.html
        for me in ("a", "b"):
            self.assertIn(f'id="{me}-plain"', html, f"{me} 缺少明文窗")
            self.assertIn(f'id="{me}-covert"', html, f"{me} 缺少密文窗")
            # 明文侧随时可用：输入框不带 disabled。
            match = re.search(rf'<input[^>]*id="{me}-plain-text"[^>]*>', html)
            self.assertIsNotNone(match)
            self.assertNotIn("disabled", match.group(0), "明文输入框不该被禁用")
            # 密文侧未握手不可输入。
            match = re.search(rf'<input[^>]*id="{me}-covert-text"[^>]*>', html)
            self.assertIsNotNone(match)
            self.assertIn("disabled", match.group(0), "密文输入框初始必须禁用")
            # 两种密文发送方式各一个按钮。
            self.assertIn(f'id="{me}-send-text"', html)
            self.assertIn(f'id="{me}-send-image"', html)
            # 每个客户端自己的图片队列。
            self.assertIn(f'id="{me}-queue"', html)
            self.assertIn(f'id="{me}-sent"', html)

    def test_network_wire_between_clients(self):
        """两个客户端之间有一条网络连线，收发时会闪。"""
        self.assertIn('id="wire"', self.html)
        self.assertIn("function flashWire", self.html)

    def test_terminal_js_defines_every_function_it_calls(self):
        """终端页的发送/接收链路必须完整。

        一次误编辑曾把 ``sendPlain``/``renderMessage`` 整段删掉：页面照常
        加载，按钮也在，但点下去只报 ``not defined``，而当时的冒烟只查了
        元素 id，全部"通过"。这里按名字点一遍，缺一个就失败。
        """
        required = (
            "post", "sendPlain", "sendCovertText", "sendCovertImage", "handshake",
            "handshakeNotice", "renderMessage", "imageSource", "stamp",
            "clearPlaceholder", "scrollDown", "pinToBottom", "poll",
            "renderQueue", "renderSent", "consume", "addFiles", "addAttachments",
            "renderAttachments", "applyAvailability", "refreshState", "flashWire",
            "bootTerminal",
        )
        for name in required:
            self.assertRegex(
                self.html, rf"(?m)^(async )?function {re.escape(name)}\(",
                f"终端 JS 缺少函数 {name}（调用处会抛 not defined）")

    def test_queue_is_per_client(self):
        """图片队列各自独立：抽走 alice 的图不影响 bob 的。"""
        html = self.html
        self.assertIn("const clients = {", html)
        self.assertIn("function consume(me, count)", html)
        self.assertIn("function renderSent(me)", html)

    def test_terminal_flow_end_to_end(self):
        """全流程：明文 → 握手 → 图片混淆 → 文本混淆，两个窗口各收各的。"""
        png = _noise_png(256)
        files = [("files", ("c.png", png, "image/png"))]

        response = _run(self.client.post("/terminal/alice/send",
                                         data={"text": "普通消息", "mode": "plain"},
                                         files=files))
        self.assertEqual(response.status_code, 200)

        # 握手也要图：alice 从队列里拿一张，bob 用自己那张回包。
        response = self._establish()
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

    def _establish(self, *, bob_covers=True):
        """Let alice start a handshake; bob answers with a picture of his own.

        The reply must travel in a picture bob supplies, so his side has to have
        one queued. ``bob_covers=False`` leaves him empty to exercise the
        "waiting for the other side to upload" path.
        """
        if bob_covers:
            _run(self.client.post("/terminal/bob/covers", files=[
                ("files", ("bob.png", _noise_png(256, seed=5), "image/png"))]))
        return _run(self.client.post(
            "/terminal/alice/send", data={"mode": "handshake"},
            files=[("files", ("c.png", _noise_png(256), "image/png"))]))

    def test_covert_image_uses_only_as_many_covers_as_needed(self):
        """短密文只消耗够用的张数，剩下的留在队列里。

        以前是把整个队列都发给服务端，于是一句话也会把用户攒的十几张图
        全吃掉——图用光了，下一次就没得发。
        """
        self._establish()
        png = _noise_png(256)
        files = [("files", (f"{i}.png", png, "image/png")) for i in range(6)]
        response = _run(self.client.post(
            "/terminal/alice/send", data={"text": "一句话", "mode": "covert-image"},
            files=files))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["images_used"], 1)
        # 收到的那一条只带一张图。
        inbox = _run(self.client.get("/terminal/inbox/bob")).json()
        sent = [m for m in inbox["messages"] if m["covert"]]
        self.assertEqual(sent[-1]["chat_image_count"], 1)

    def test_covert_text_is_not_echoed_into_plaintext_window(self):
        """密文是给对端的，不该同时出现在双方的明文窗里。

        图片混淆发送的 ``chat_text`` 必须为空：那句"图片机密"是载荷，
        不是公开说的话。夹在明文窗里既泄密，也让人分不清自己公开说了什么。
        """
        self._establish()
        _run(self.client.post(
            "/terminal/alice/send", data={"text": "秘密一句", "mode": "covert-image"},
            files=[("files", ("c.png", _noise_png(256), "image/png"))]))

        for name in ("alice", "bob"):
            body = _run(self.client.get(f"/terminal/inbox/{name}")).json()
            texts = [m["chat_text"] for m in body["messages"] if m["chat_text"]]
            self.assertNotIn("秘密一句", texts, f"{name} 的明文窗混进了密文")
            covert = [s for m in body["messages"] for s in m["covert"]]
            self.assertIn("秘密一句", covert, f"{name} 的密文窗没收到")

    def test_plaintext_window_shows_the_wire_text(self):
        """明文窗是网关视角：显示网线上真正传出去的那串内容。

        文本混淆传出去的是生成的中文宿主文本，不是用户键入的密文；那串
        文本双方都能看到，而密文只在密文窗。以前这里留空，用户看不出
        自己到底往网上发了什么。
        """
        self._establish()
        _run(self.client.post("/terminal/alice/send",
                              data={"text": "机密一句话", "mode": "covert-text"}))

        for name in ("alice", "bob"):
            body = _run(self.client.get(f"/terminal/inbox/{name}")).json()
            sent = [m for m in body["messages"] if m["covert"]]
            self.assertTrue(sent, f"{name} 没收到密文")
            wire = sent[-1]["chat_text"]
            self.assertTrue(wire, f"{name} 的明文窗是空的（应显示宿主文本）")
            self.assertNotEqual(wire, "机密一句话", "明文窗显示的是原文，不是网线内容")
            # 宿主文本是中文散文，不是十六进制或 base64 之类的不可读东西。
            self.assertTrue(any("一" <= ch <= "鿿" for ch in wire))

    def test_image_covert_leaves_no_text_in_plaintext_window(self):
        """图片混淆没有可见文本：明文窗只放图，不编造文字。"""
        self._establish()
        _run(self.client.post(
            "/terminal/alice/send", data={"text": "秘密", "mode": "covert-image"},
            files=[("files", ("c.png", _noise_png(256), "image/png"))]))
        body = _run(self.client.get("/terminal/inbox/bob")).json()
        sent = [m for m in body["messages"] if m["covert"]]
        self.assertTrue(sent)
        self.assertIsNone(sent[-1]["chat_text"])

    def test_reply_waits_for_this_side_to_upload_a_picture(self):
        """本端没图就等着，状态明说在等，用户补图后自动回包。

        之前是拿对端发来的图回写响应：等于替对端发了他自己的图，聊天里会
        出现两条一样的图。现在改为用本端队列的头一张，没有就等。
        """
        _run(self.client.post("/terminal/alice/send", data={"mode": "handshake"},
                              files=[("files", ("c.png", _noise_png(256),
                                                "image/png"))]))
        # bob 手上没图：谁都没建立，bob 明确显示在等图。
        self.assertFalse(_run(self.client.get("/terminal/state"))
                         .json()["terminals"]["bob"]["established"])
        bob = _run(self.client.get("/terminal/state")).json()["terminals"]["bob"]
        self.assertTrue(bob["awaiting_cover"])
        self.assertEqual(bob["state"], "waiting")

        # 用户给 bob 补了一张图：回包出去，两端同时可用。
        response = _run(self.client.post("/terminal/bob/covers", files=[
            ("files", ("bob.png", _noise_png(256, seed=5), "image/png"))]))
        self.assertTrue(response.json()["released"])
        for name in ("alice", "bob"):
            info = _run(self.client.get("/terminal/state")).json()["terminals"][name]
            self.assertTrue(info["established"], f"{name} 还没建立")
            self.assertFalse(info["awaiting_cover"])

        # 之后正常发密文。
        response = _run(self.client.post(
            "/terminal/alice/send", data={"text": "机密", "mode": "covert-image"},
            files=[("files", ("c.png", _noise_png(256), "image/png"))]))
        self.assertEqual(response.status_code, 200)
        body = _run(self.client.get("/terminal/inbox/bob")).json()
        self.assertIn("机密", [c for m in body["messages"] for c in m["covert"]])

    def test_handshake_images_never_show_up_as_chat_pictures(self):
        """握手图是控制报文的载体，不该当成"我发了张图"。

        握手会用**同一张图**来回一趟（对方拿请求原图回写响应），如果照常
        渲染，聊天记录里就冒出两条几乎一样的图，看着像重复发送。这里断言
        握手那几条的 ``chat_image_count`` 都是 0，只有真正的图片消息才带图。
        """
        self._establish()
        png = _noise_png(256)
        _run(self.client.post(
            "/terminal/alice/send", data={"text": "正文", "mode": "covert-image"},
            files=[("files", ("c.png", png, "image/png"))]))

        for name in ("alice", "bob"):
            body = _run(self.client.get(f"/terminal/inbox/{name}")).json()
            for message in body["messages"]:
                if message["mode"] == "handshake" or message["handshake"]:
                    self.assertEqual(message["chat_image_count"], 0,
                                     f"{name} 的握手记到了聊天图片")
            # 正好一条真正带图的密文消息，不多不少。
            with_images = [m for m in body["messages"] if m["chat_image_count"]]
            self.assertEqual(len(with_images), 1, f"{name} 的图片消息不止一条")

    def test_every_message_carries_a_timestamp(self):
        """每条消息都要有时刻：没有时间的聊天记录没法对账。"""
        self._establish()
        for name in ("alice", "bob"):
            body = _run(self.client.get(f"/terminal/inbox/{name}")).json()
            for message in body["messages"]:
                self.assertIsInstance(message["at"], (int, float))
                self.assertGreater(message["at"], 0)

    def test_own_images_are_served_back(self):
        """自己发的图也要能显示：收件历史里没有"我发的包"，得单独留一份。"""
        self._establish()
        png = _noise_png(256)
        _run(self.client.post(
            "/terminal/alice/send", data={"text": "带图", "mode": "plain"},
            files=[("files", ("c.png", png, "image/png"))]))

        mine = [m for m in _run(self.client.get(
            "/terminal/inbox/alice")).json()["messages"] if m["direction"] == "out"]
        own = [m for m in mine if m["chat_image_count"]]
        self.assertTrue(own, "没记到自己发的图片")
        response = _run(self.client.get(
            f"/terminal/media/alice/{own[-1]['own_index']}?own=true&image=0"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/"))

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
