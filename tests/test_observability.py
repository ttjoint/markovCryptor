"""Logging and metrics.

The value of these is that they never break anything else, so most of what is
tested here is the failure behaviour: a log call that raises, a broken sink, a
metric label set that would explode cardinality.
"""

from __future__ import annotations

import io
import json
import logging
import unittest
from pathlib import Path
import tempfile

import anyio
from httpx import ASGITransport, AsyncClient

from markovcryptor import observability
from markovcryptor.engine import Engine
from markovcryptor.model import MarkovModel
from markovcryptor.web import create_app

import fixtures


def _run(coro):
    async def _await():
        return await coro

    return anyio.run(_await)


class FormatterTest(unittest.TestCase):
    def test_plain_value_is_bare(self):
        self.assertEqual(observability._render({"channel": "image"}),
                         "channel=image")

    def test_value_with_space_is_quoted(self):
        self.assertEqual(observability._render({"message": "两个 词"}),
                         'message="两个 词"')

    def test_quotes_and_newlines_are_escaped(self):
        rendered = observability._render({"m": 'a"b\nc'})
        self.assertNotIn("\n", rendered, "换行会把一条日志拆成两行")
        # A bare quote inside a quoted value must be escaped, otherwise a reader
        # scanning for the closing quote stops in the wrong place.
        self.assertIn('\\"', rendered)
        self.assertTrue(rendered.startswith('m="') and rendered.endswith('"'))

    def test_empty_value_is_quoted_so_it_stays_visible(self):
        self.assertEqual(observability._render({"label": ""}), 'label=""')


class ConfigureTest(unittest.TestCase):
    def tearDown(self):
        # Closing matters on Windows: an open FileHandler locks the file and the
        # TemporaryDirectory in the test below cannot delete it.
        for handler in list(observability.get_logger().handlers):
            observability.get_logger().removeHandler(handler)
            handler.close()
        observability.configure("CRITICAL")

    def test_silent_until_configured(self):
        """A library must not write to stderr before anyone asked it to.

        Without this, every embed in a test run or in an application that merely
        imports markovcryptor prints INFO lines nobody requested.
        """
        previous = observability._configured
        observability._configured = False
        try:
            sink = io.StringIO()
            handler = logging.StreamHandler(sink)
            observability.get_logger().addHandler(handler)
            observability.get_logger().setLevel(logging.DEBUG)
            observability.event("test", "should be swallowed")
            handler.flush()
            self.assertEqual(sink.getvalue(), "")
        finally:
            observability.get_logger().removeHandler(handler)
            observability._configured = previous

    def test_configure_is_idempotent(self):
        observability.configure("INFO")
        first = len(observability.get_logger().handlers)
        observability.configure("INFO")
        self.assertEqual(len(observability.get_logger().handlers), first,
                         "重复 configure 不应叠加 handler，否则日志重复")

    def test_file_sink_is_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log" / "run.log"
            observability.configure("INFO", log_file=path)
            observability.event("test", "中文事件", channel="图片")
            for handler in observability.get_logger().handlers:
                handler.flush()
            text = path.read_text(encoding="utf-8")
            self.assertIn("中文事件", text)
            self.assertIn("channel=图片", text)
            # Release the file before the directory goes away: on Windows an
            # open handle makes the cleanup fail, not the assertion.
            observability.configure("CRITICAL")

    def test_unknown_level_falls_back_to_info(self):
        observability.configure("不许的级别")
        self.assertEqual(observability.get_logger().level, logging.INFO)

    def test_event_never_raises_even_with_a_broken_handler(self):
        observability.configure("INFO", stream=io.StringIO())
        logger = observability.get_logger()
        original = list(logger.handlers)
        logger.addHandler(_Exploding())
        try:
            observability.event("test", "should not raise", a="b")
        finally:
            for handler in list(logger.handlers):
                if isinstance(handler, _Exploding):
                    logger.removeHandler(handler)
            self.assertEqual(logger.handlers, original)


class _Exploding(logging.Handler):
    def emit(self, record):
        raise RuntimeError("sink is broken")


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.metrics = observability.Metrics()

    def test_counters_accumulate_with_labels(self):
        self.metrics.incr("embed", channel="image")
        self.metrics.incr("embed", channel="image")
        self.metrics.incr("embed", channel="gif")
        counters = self.metrics.snapshot()["counters"]
        self.assertEqual(counters["embed{channel=image}"], 2)
        self.assertEqual(counters["embed{channel=gif}"], 1)

    def test_labels_are_sorted_so_key_order_cannot_split_a_counter(self):
        self.metrics.incr("x", b="2", a="1")
        self.metrics.incr("x", a="1", b="2")
        counters = self.metrics.snapshot()["counters"]
        self.assertEqual(list(counters), ["x{a=1,b=2}"])
        self.assertEqual(counters["x{a=1,b=2}"], 2)

    def test_latency_reports_a_mean(self):
        self.metrics.observe("t", 10.0, route="/a")
        self.metrics.observe("t", 30.0, route="/a")
        latency = self.metrics.snapshot()["latency"]["t{route=/a}"]
        self.assertEqual(latency["count"], 2)
        self.assertEqual(latency["mean_ms"], 20.0)

    def test_no_label_means_no_braces(self):
        self.metrics.incr("bare")
        self.assertIn("bare", self.metrics.snapshot()["counters"])

    def test_reset_clears_everything(self):
        self.metrics.incr("x")
        self.metrics.observe("t", 1.0)
        self.metrics.reset()
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["counters"], {})
        self.assertEqual(snapshot["latency"], {})


class EngineInstrumentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(
            model=MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        )

    def setUp(self):
        observability.METRICS.reset()
        observability.configure("CRITICAL")

    def test_embed_and_extract_are_counted_with_real_latency(self):
        from markovcryptor.channels import Carrier

        envelope = self.engine.embed(
            "image",
            Carrier.of_bytes(fixtures.PHOTO.read_bytes(), kind="image"),
            b"metrics", b"k",
        )
        self.engine.extract(
            "image", Carrier.of_bytes(envelope.primary, kind="image"), b"k"
        )
        snapshot = observability.METRICS.snapshot()
        self.assertEqual(snapshot["counters"]["embed{channel=image}"], 1)
        self.assertEqual(snapshot["counters"]["extract{channel=image}"], 1)
        # Measured inside the engine, not read from the report: the report's
        # elapsed_ms may be zero for channels that do not time themselves.
        self.assertGreater(snapshot["latency"]["embed_ms{channel=image}"]["mean_ms"], 0)

    def test_failures_are_not_counted_as_successes(self):
        from markovcryptor.channels import Carrier
        from markovcryptor.core.errors import StegoError

        envelope = self.engine.embed(
            "image",
            Carrier.of_bytes(fixtures.PHOTO.read_bytes(), kind="image"),
            b"x", b"right",
        )
        with self.assertRaises(StegoError):
            self.engine.extract(
                "image", Carrier.of_bytes(envelope.primary, kind="image"), b"wrong"
            )
        snapshot = observability.METRICS.snapshot()
        self.assertNotIn("extract{channel=image}", snapshot["counters"])


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
        cls.app = create_app(engine=Engine(model=model))
        cls.client = AsyncClient(transport=ASGITransport(app=cls.app),
                                 base_url="http://test")

    def setUp(self):
        observability.METRICS.reset()
        observability.configure("CRITICAL")

    def test_metrics_endpoint_reports_requests_by_route_template(self):
        _run(self.client.get("/health"))
        _run(self.client.post("/extract?channel=image&kind=image",
                              files={"file": ("c.png", b"not a png")}))
        counters = _run(self.client.get("/metrics")).json()["counters"]
        self.assertEqual(counters["requests{route=/health,status=200}"], 1)
        # A dynamic path must collapse onto its template, or one counter per id
        # would be created and the metric would be unreadable.
        failed = [k for k in counters if k.startswith("requests{route=/extract")]
        self.assertEqual(len(failed), 1, f"路由模板没有生效：{failed}")
        # Any 4xx is acceptable; the point is that there is exactly one bucket.
        self.assertRegex(failed[0], r"status=4\d\d\}$")

    def test_failed_requests_are_counted_by_stable_error_code(self):
        response = _run(self.client.post("/extract?channel=image&kind=image",
                                         files={"file": ("c.png", b"not a png")}))
        self.assertGreaterEqual(response.status_code, 400)
        body = _run(self.client.get("/metrics")).json()
        codes = [k for k in body["counters"] if k.startswith("requests_failed")]
        self.assertTrue(codes, f"失败请求没有被计数：{body['counters']}")

    def test_metrics_is_not_counted_as_a_request(self):
        """Otherwise the act of watching changes the numbers you watch."""
        _run(self.client.get("/metrics"))
        body = _run(self.client.get("/metrics")).json()
        self.assertFalse([k for k in body["counters"] if "route=/metrics" in k],
                         "GET /metrics 不应给自己计数")

    def test_metrics_is_in_the_openapi_schema(self):
        schema = _run(self.client.get("/openapi.json")).json()
        self.assertIn("/metrics", schema["paths"])


if __name__ == "__main__":
    unittest.main()
