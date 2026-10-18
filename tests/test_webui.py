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


if __name__ == "__main__":
    unittest.main()
