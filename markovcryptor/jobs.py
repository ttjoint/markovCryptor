"""Batch jobs: many carriers, one operation, resumable.

The reason this is a module and not a ``for`` loop in the CLI:

* **Partial failure is the normal case.** A folder of 15 000 images contains
  flat gradients, 20-pixel icons, corrupt files and CMYK JPEGs. A batch that
  aborts on the first bad file is useless, so every item records its own
  outcome and its own real error.
* **It takes hours.** The measured full-library run was 38 minutes. Anything
  that long must survive Ctrl-C and a reboot, so progress is appended to a
  JSONL checkpoint after every item and a restart skips what is already done.
* **It must not hold the batch in memory.** ``Carrier.load`` is a thunk, so an
  item's pixels exist only while that item is being processed.

Concurrency uses a thread pool: numpy releases the GIL for the transform work,
Pillow's decode/encode does not, so the default worker count is deliberately low
and channels may override it via ``Channel.concurrency_hint``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import time
import uuid
from typing import Any, Callable, Iterable, Iterator, Literal

from .channels import Carrier, CarrierKind
from .core.errors import CarrierDamaged, StegoError
from .engine import Engine

OnError = Literal["abort", "skip", "retry"]

#: Suffixes worth handing to an image channel. Kept here (not in the CLI) so the
#: API and the CLI agree on what "an image folder" means.
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".avif"}
)
ANIMATION_SUFFIXES = frozenset({".gif", ".webp", ".apng"})
TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".json", ".log"})


def _suffixes_for(kind: CarrierKind) -> frozenset[str]:
    if kind is CarrierKind.ANIMATION:
        return ANIMATION_SUFFIXES
    if kind is CarrierKind.TEXT:
        return TEXT_SUFFIXES
    return IMAGE_SUFFIXES


@dataclass(frozen=True)
class Item:
    """One carrier plus where its result should go."""

    source: Path
    target: Path


@dataclass(frozen=True)
class ItemResult:
    source: str
    target: str
    ok: bool
    error: str = ""
    error_code: str = ""
    reasons: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    payload_bytes: int = 0
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobSpec:
    channel_id: str
    items: tuple[Item, ...]
    key: bytes = b""
    options: dict[str, Any] = field(default_factory=dict)
    #: Which direction. ``extract`` reads carriers and writes payloads.
    action: Literal["embed", "extract"] = "embed"
    #: The payload for every item when ``action == "embed"``.
    payload: bytes = b""
    on_error: OnError = "skip"
    max_workers: int = 4
    kind: CarrierKind = CarrierKind.IMAGE
    retries: int = 1


@dataclass
class JobStatus:
    job_id: str
    state: Literal["queued", "running", "done", "failed", "cancelled"]
    total: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    current: str | None = None
    started_at: float = 0.0
    elapsed: float = 0.0
    results: list[ItemResult] = field(default_factory=list)

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 1.0
        return min(1.0, (self.succeeded + self.failed + self.skipped) / self.total)


class Checkpoint:
    """Append-only JSONL of finished items, so a restart can skip them.

    JSONL rather than a rewritten JSON file: an append is atomic enough for our
    purposes and costs O(1) per item, whereas rewriting 15 000 results after
    every item would dominate the runtime.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.done: dict[str, ItemResult] = {}
        if path is not None and path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A half-written last line is expected after a hard kill.
                continue
            result = ItemResult(
                source=record.get("source", ""),
                target=record.get("target", ""),
                ok=bool(record.get("ok")),
                error=record.get("error", ""),
                error_code=record.get("error_code", ""),
                reasons=tuple(record.get("reasons") or ()),
                hints=tuple(record.get("hints") or ()),
                payload_bytes=int(record.get("payload_bytes") or 0),
                elapsed_ms=int(record.get("elapsed_ms") or 0),
            )
            if result.source:
                self.done[result.source] = result

    def record(self, result: ItemResult) -> None:
        self.done[result.source] = result
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.as_dict(), ensure_ascii=False) + "\n")


def discover(root: Path, kind: CarrierKind, *, recursive: bool = True,
             limit: int | None = None) -> list[Path]:
    """Every file under ``root`` whose suffix suits the carrier kind."""
    suffixes = _suffixes_for(kind)
    walker: Iterator[Path] = root.rglob("*") if recursive else root.glob("*")
    found = sorted(
        (p for p in walker if p.is_file() and p.suffix.lower() in suffixes),
        key=lambda p: str(p).lower(),
    )
    return found[:limit] if limit else found


def classify(exc: BaseException) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Map an exception to ``(code, message, reasons, hints)`` verbatim.

    The message is never replaced with a generic phrase: for a batch, the whole
    point of the report is to say *why* each of 300 files failed.
    """
    if isinstance(exc, StegoError):
        return exc.code, str(exc), tuple(exc.reasons), tuple(exc.hints)
    return type(exc).__name__, str(exc) or type(exc).__name__, (), ()


class BatchRunner:
    """Runs one job, reporting progress through a callback."""

    def __init__(self, engine: Engine, checkpoint_path: Path | None = None) -> None:
        self.engine = engine
        self.checkpoint = Checkpoint(checkpoint_path)
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the job to stop. Items already in flight still finish."""
        self._cancelled = True

    def run(self, spec: JobSpec, *, job_id: str | None = None,
            on_progress: Callable[[JobStatus], None] | None = None) -> JobStatus:
        status = JobStatus(
            job_id=job_id or uuid.uuid4().hex[:12],
            state="running",
            total=len(spec.items),
            started_at=time.time(),
        )
        channel = self.engine.channel(spec.channel_id)
        workers = max(1, int(spec.max_workers))
        hint = getattr(channel, "concurrency_hint", None)
        if hint:
            workers = max(1, min(workers, int(hint)))

        pending: list[Item] = []
        for item in spec.items:
            cached = self.checkpoint.done.get(str(item.source))
            if cached is not None:
                # Already handled by an earlier run: count it, do not redo it.
                status.results.append(cached)
                if cached.ok:
                    status.succeeded += 1
                else:
                    status.failed += 1
            else:
                pending.append(item)
        if on_progress:
            on_progress(status)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._run_one, spec, item): item for item in pending
            }
            for future in as_completed(futures):
                if self._cancelled:
                    # ``abort`` already set "failed"; only an explicit cancel
                    # while still running means "cancelled".
                    if status.state == "running":
                        status.state = "cancelled"
                    for remaining in futures:
                        remaining.cancel()
                    break
                result = future.result()
                self.checkpoint.record(result)
                status.results.append(result)
                status.current = result.source
                if result.ok:
                    status.succeeded += 1
                else:
                    status.failed += 1
                if on_progress:
                    on_progress(status)
                if not result.ok and spec.on_error == "abort":
                    status.state = "failed"
                    self._cancelled = True

        if status.state == "running":
            status.state = "done"
        status.elapsed = time.time() - status.started_at
        if on_progress:
            on_progress(status)
        return status

    # ------------------------------------------------------------------ workers

    def _run_one(self, spec: JobSpec, item: Item) -> ItemResult:
        started = time.perf_counter()
        attempts = 1 + max(0, spec.retries if spec.on_error == "retry" else 0)
        last: BaseException | None = None
        for _ in range(attempts):
            try:
                payload_bytes = self._attempt(spec, item)
            except CarrierDamaged as exc:
                last = exc
                continue        # the one class that a retry can plausibly fix
            except (StegoError, OSError, ValueError) as exc:
                return self._failure(item, exc, started)
            except Exception as exc:            # noqa: BLE001 - never lose an item
                return self._failure(item, exc, started)
            return ItemResult(
                source=str(item.source), target=str(item.target), ok=True,
                payload_bytes=payload_bytes,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        assert last is not None
        return self._failure(item, last, started)

    def _attempt(self, spec: JobSpec, item: Item) -> int:
        if spec.action == "embed":
            carrier = _carrier(item.source, spec.kind)
            envelope = self.engine.embed(spec.channel_id, carrier, spec.payload,
                                         spec.key, spec.options)
            _write_target(item.target, envelope.primary)
            return len(spec.payload)
        carrier = _carrier(item.source, spec.kind)
        payload = self.engine.extract(spec.channel_id, carrier, spec.key, spec.options)
        _write_target(item.target, payload)
        return len(payload)

    def _failure(self, item: Item, exc: BaseException,
                 started: float) -> ItemResult:
        code, message, reasons, hints = classify(exc)
        return ItemResult(
            source=str(item.source), target=str(item.target), ok=False,
            error=message, error_code=code, reasons=reasons, hints=hints,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def _carrier(path: Path, kind: CarrierKind) -> Carrier:
    if kind is CarrierKind.TEXT:
        from .model import read_text_file

        return Carrier(kind=kind, load=lambda: read_text_file(path),
                       size_hint=path.stat().st_size, media_type="text/plain",
                       label=path.name)
    return Carrier(kind=kind, load=path.read_bytes,
                   size_hint=path.stat().st_size, label=path.name)


def _write_target(target: Path, data: str | bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        target.write_text(data, encoding="utf-8", newline="")
    else:
        target.write_bytes(data)


def plan_targets(items: Iterable[Path], out_dir: Path, *, suffix: str) -> tuple[Item, ...]:
    """Derive one target per source, keeping the stem so results are traceable.

    A name clash can only happen when two source folders hold the same filename,
    so the parent folder name is prepended only in that case -- renaming every
    file would make the output folder unreadable.
    """
    sources = list(items)
    stems = [p.stem for p in sources]
    duplicated = {s for s in stems if stems.count(s) > 1}
    planned: list[Item] = []
    for source in sources:
        stem = source.stem
        if stem in duplicated:
            stem = f"{source.parent.name}_{stem}"
        planned.append(Item(source=source, target=out_dir / f"{stem}{suffix}"))
    return tuple(planned)


__all__ = [
    "ANIMATION_SUFFIXES", "BatchRunner", "Checkpoint", "IMAGE_SUFFIXES", "Item",
    "ItemResult", "JobSpec", "JobStatus", "TEXT_SUFFIXES", "classify", "discover",
    "plan_targets",
]
