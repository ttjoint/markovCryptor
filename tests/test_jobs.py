"""Batch jobs.

The properties worth testing are the ones that make a batch usable rather than
merely runnable:

* one bad file must not kill the run, and must report its own real error;
* a 15 000-item run must survive being interrupted -- so the checkpoint has to
  be reloadable and must actually be skipped;
* the job must never hold the whole batch in memory, which is why carriers stay
  lazy (``Carrier.load``) instead of being decoded up front.

No test here reads from a shared fixture folder on disk.
"""

from __future__ import annotations

from io import BytesIO
import shutil
import tempfile
import unittest
from pathlib import Path

from markovcryptor.channels import CarrierKind
from markovcryptor.engine import Engine
from markovcryptor.jobs import (
    BatchRunner, Checkpoint, Item, ItemResult, JobSpec, classify, discover,
    plan_targets,
)
from markovcryptor.model import MarkovModel

import fixtures


def _png(image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _TempTree(unittest.TestCase):
    """Shared helper: a scratch folder with N real PNGs."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mkx1-jobs-"))
        self.inp = self.root / "in"
        self.out = self.root / "out"
        self.inp.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make_images(self, count: int) -> list[Path]:
        """Distinct images, because identical bytes hide per-item bugs."""
        made = []
        for i in range(count):
            path = self.inp / f"img{i:03d}.png"
            path.write_bytes(_png(fixtures.varied_photo(i)))
            made.append(path)
        return made

    def engine(self) -> Engine:
        return Engine(model=MarkovModel.train(fixtures.corpus_text(),
                                              vocabulary_size=512))


class DiscoverTest(_TempTree):
    def test_finds_images_by_suffix_and_ignores_others(self):
        self.make_images(3)
        (self.inp / "notes.txt").write_text("x", encoding="utf-8")
        (self.inp / "clip.gif").write_bytes(b"GIF89a")
        found = discover(self.inp, CarrierKind.IMAGE)
        self.assertEqual(len(found), 3)
        self.assertTrue(all(p.suffix == ".png" for p in found))

    def test_animation_kind_picks_gifs(self):
        self.make_images(1)
        (self.inp / "clip.gif").write_bytes(b"GIF89a")
        self.assertEqual([p.name for p in discover(self.inp, CarrierKind.ANIMATION)],
                         ["clip.gif"])

    def test_limit_is_honoured(self):
        self.make_images(5)
        self.assertEqual(len(discover(self.inp, CarrierKind.IMAGE, limit=2)), 2)

    def test_results_are_sorted_so_runs_are_reproducible(self):
        self.make_images(4)
        found = [p.name for p in discover(self.inp, CarrierKind.IMAGE)]
        self.assertEqual(found, sorted(found))


class PlanTargetsTest(unittest.TestCase):
    def test_stem_is_preserved(self):
        items = plan_targets([Path("a/cat.png")], Path("out"), suffix=".png")
        self.assertEqual(items[0].target.name, "cat.png")

    def test_duplicate_stems_get_the_parent_folder(self):
        """Two folders with the same filename must not overwrite each other."""
        sources = [Path("one/cat.png"), Path("two/cat.png")]
        items = plan_targets(sources, Path("out"), suffix=".png")
        self.assertEqual(len({i.target.name for i in items}), 2)
        self.assertIn("one", items[0].target.name)


class CheckpointTest(_TempTree):
    def _result(self, name: str, ok: bool = True) -> ItemResult:
        return ItemResult(source=name, target=name + ".out", ok=ok,
                          error="" if ok else "boom")

    def test_records_survive_a_reload(self):
        path = self.root / "ck.jsonl"
        checkpoint = Checkpoint(path)
        checkpoint.record(self._result("a.png"))
        checkpoint.record(self._result("b.png", ok=False))

        reloaded = Checkpoint(path)
        self.assertEqual(set(reloaded.done), {"a.png", "b.png"})
        self.assertFalse(reloaded.done["b.png"].ok)

    def test_a_truncated_last_line_is_ignored(self):
        """A hard kill leaves half a line; that must not break the resume."""
        path = self.root / "ck.jsonl"
        checkpoint = Checkpoint(path)
        checkpoint.record(self._result("a.png"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"source": "b.png", "ok": tr')

        reloaded = Checkpoint(path)
        self.assertEqual(set(reloaded.done), {"a.png"})

    def test_errors_keep_their_reasons_and_hints(self):
        path = self.root / "ck.jsonl"
        checkpoint = Checkpoint(path)
        checkpoint.record(ItemResult(
            source="x.png", target="", ok=False,
            error="容量不足：最多 34 字节，需要 5000 字节",
            error_code="capacity_exceeded",
            reasons=("容量不足：最多 34 字节，需要 5000 字节",),
            hints=("放大到 900×900 即可承载",),
        ))
        reloaded = Checkpoint(path).done["x.png"]
        self.assertEqual(reloaded.error_code, "capacity_exceeded")
        self.assertTrue(reloaded.reasons)
        self.assertTrue(reloaded.hints)


class BatchRunnerTest(_TempTree):
    def test_every_item_succeeds_and_writes_output(self):
        self.make_images(4)
        engine = self.engine()
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), self.out,
                             suffix=".png")
        spec = JobSpec(channel_id="image", items=items, key=b"k",
                       payload=b"batch-secret", max_workers=2)
        status = BatchRunner(engine).run(spec)

        self.assertEqual(status.state, "done")
        self.assertEqual((status.succeeded, status.failed), (4, 0))
        self.assertEqual(len(list(self.out.glob("*.png"))), 4)
        self.assertAlmostEqual(status.progress, 1.0)

    def test_one_bad_file_does_not_stop_the_run(self):
        """Real folders contain corrupt files; the run must survive them.

        A truncated PNG names its suffix correctly, so discovery picks it up --
        which is exactly the case that made whole-folder runs die before.
        """
        self.make_images(3)
        (self.inp / "broken.png").write_bytes(b"PNG-broken")
        engine = self.engine()
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), self.out,
                             suffix=".png")
        status = BatchRunner(engine).run(JobSpec(
            channel_id="image", items=items, key=b"k", payload=b"x" * 16,
        ))

        self.assertEqual(status.state, "done")
        self.assertEqual(status.succeeded, 3)
        self.assertEqual(status.failed, 1)
        failure = next(r for r in status.results if not r.ok)
        self.assertIn("broken.png", failure.source)
        # The real reason must survive into the report, not become "failed".
        self.assertTrue(failure.error, "失败项必须带原因")
        self.assertTrue(failure.error_code)

    def test_abort_stops_at_the_first_failure(self):
        self.make_images(2)
        (self.inp / "broken.png").write_bytes(b"PNG-broken")
        engine = self.engine()
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), self.out,
                             suffix=".png")
        status = BatchRunner(engine).run(JobSpec(
            channel_id="image", items=items, key=b"k", payload=b"y" * 16,
            on_error="abort", max_workers=1,
        ))
        self.assertEqual(status.state, "failed")
        self.assertEqual(status.failed, 1)

    def test_resume_skips_what_is_already_done(self):
        self.make_images(3)
        engine = self.engine()
        checkpoint = self.root / "ck.jsonl"
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), self.out,
                             suffix=".png")
        spec = JobSpec(channel_id="image", items=items, key=b"k", payload=b"s")

        first = BatchRunner(engine, checkpoint).run(spec)
        self.assertEqual(first.succeeded, 3)
        shutil.rmtree(self.out)

        calls: list[str] = []
        runner = BatchRunner(engine, checkpoint)
        original = runner._run_one
        runner._run_one = lambda s, item: (calls.append(str(item.source)),
                                           original(s, item))[1]
        second = runner.run(spec)

        self.assertEqual(calls, [], "已完成的项被重新处理了")
        self.assertEqual(second.succeeded, 3)
        self.assertFalse(self.out.exists(), "续跑不应重写输出")

    def test_extract_round_trip(self):
        self.make_images(2)
        engine = self.engine()
        hidden = self.root / "hidden"
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), hidden,
                             suffix=".png")
        BatchRunner(engine).run(JobSpec(
            channel_id="image", items=items, key=b"k", payload=b"roundtrip",
        ))

        back = self.root / "back"
        back_items = plan_targets(discover(hidden, CarrierKind.IMAGE), back,
                                  suffix=".bin")
        status = BatchRunner(engine).run(JobSpec(
            channel_id="image", items=back_items, key=b"k", action="extract",
        ))
        self.assertEqual(status.failed, 0)
        for produced in back.glob("*.bin"):
            self.assertEqual(produced.read_bytes(), b"roundtrip")

    def test_progress_callback_sees_intermediate_states(self):
        self.make_images(3)
        engine = self.engine()
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), self.out,
                             suffix=".png")
        seen: list[float] = []
        BatchRunner(engine).run(
            JobSpec(channel_id="image", items=items, key=b"k", payload=b"p"),
            on_progress=lambda s: seen.append(s.progress),
        )
        self.assertGreater(len(seen), 2, "进度至少要有起步、过程、结束")
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(seen, sorted(seen), "进度不应回退")

    def test_cancel_stops_scheduling_new_items(self):
        self.make_images(6)
        engine = self.engine()
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), self.out,
                             suffix=".png")
        runner = BatchRunner(engine)

        def on_progress(status) -> None:
            if status.succeeded >= 1:
                runner.cancel()

        status = runner.run(
            JobSpec(channel_id="image", items=items, key=b"k", payload=b"p",
                    max_workers=1),
            on_progress=on_progress,
        )
        self.assertEqual(status.state, "cancelled")
        # Deterministic, not a race: items are now submitted lazily, so after the
        # first result cancels the job the remaining five are never read. (This
        # used to be a coin flip -- everything was submitted up front, so a fast
        # machine finished the whole batch before the assertion.)
        self.assertEqual(status.succeeded + status.failed, 1)
        self.assertLess(status.succeeded + status.failed, status.total)

    def test_a_cancelled_runner_can_be_reused(self):
        """Cancelling one job must not poison the next."""
        self.make_images(3)
        engine = self.engine()
        items = plan_targets(discover(self.inp, CarrierKind.IMAGE), self.out,
                             suffix=".png")
        runner = BatchRunner(engine)
        runner.cancel()
        status = runner.run(
            JobSpec(channel_id="image", items=items, key=b"k", payload=b"p"),
        )
        self.assertEqual(status.state, "done", "runner 被上一次取消污染了")
        self.assertEqual(status.succeeded, len(items))

    def test_concurrency_is_capped_by_the_channel_hint(self):
        """GIF re-encodes every frame; the channel says how many at once."""
        engine = self.engine()
        channel = engine.channel("gif")
        self.assertIsNotNone(channel.concurrency_hint)
        self.assertLessEqual(channel.concurrency_hint, 4)

    def test_missing_source_file_reports_the_path(self):
        engine = self.engine()
        status = BatchRunner(engine).run(JobSpec(
            channel_id="image",
            items=(Item(source=self.inp / "ghost.png", target=self.out / "g.png"),),
            key=b"k", payload=b"x",
        ))
        self.assertEqual(status.failed, 1)
        self.assertIn("ghost.png", status.results[0].error)


class ClassifyTest(unittest.TestCase):
    def test_project_errors_keep_code_reasons_hints(self):
        from markovcryptor.core.errors import CapacityExceeded

        code, message, reasons, hints = classify(
            CapacityExceeded("容量不足：最多 34 字节，需要 5000 字节",
                             reasons=("容量不足：最多 34 字节，需要 5000 字节",),
                             hints=("放大到 900×900",))
        )
        self.assertEqual(code, "capacity_exceeded")
        self.assertIn("34", message)
        self.assertTrue(reasons)
        self.assertTrue(hints)

    def test_foreign_exception_is_named_not_swallowed(self):
        code, message, _, _ = classify(FileNotFoundError("no such file"))
        self.assertEqual(code, "FileNotFoundError")
        self.assertIn("no such file", message)


if __name__ == "__main__":
    unittest.main()
