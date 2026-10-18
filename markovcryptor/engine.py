"""The engine: one object that owns model loading and dispatches to channels.

Before this existed, every entry point (CLI, ``gui.py``, ``chat_gui.py``) did
its own model discovery, its own "which channel am I?" branching, and its own
error printing. ``Engine`` centralises the two things that actually needed
centralising:

* **Model caching.** Loading a 3000-hanzi model is ~50 ms and the Chinese
  channel needs one for *every* operation, including ``probe``. Without a cache
  a batch of 500 files pays it 500 times. The cache invalidates on mtime so an
  edit to the JSON on disk is picked up without a restart.
* **Channel resolution.** ``chinese`` is only usable once a model is loaded;
  the registry decides whether it exists, and the engine decides whether it is
  *available right now*.

Everything else is a pass-through. The engine deliberately does **not** wrap
``Channel`` errors: they already carry ``code``/``http_status`` and the verbatim
cause, and re-wrapping is how those get lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import logging
import time

from . import observability
from .channels import Carrier, Feasibility
from .channels import registry
from .channels.base import Envelope
from .channels.options import coerce
from .codec import MarkovStegoCodec, decode_with_models, identify_model
from .core.errors import ChannelUnavailable, InvalidInput
from .core.progress import ProgressFn
from .model import MarkovModel, default_model_dir, discover_model_paths


@dataclass
class Config:
    """Filesystem locations the engine reads models from."""

    model_dir: Path = field(default_factory=default_model_dir)
    #: Extra model files/dirs, tried before ``model_dir``.
    extra_models: tuple[Path, ...] = ()


class ModelCache:
    """Loads models once and keeps them until the file changes on disk."""

    def __init__(self) -> None:
        #: path -> (mtime_ns, model)
        self._cache: dict[Path, tuple[int, MarkovModel]] = {}

    def load(self, path: Path) -> MarkovModel:
        key = path.resolve()
        try:
            mtime = key.stat().st_mtime_ns
        except OSError as exc:
            raise InvalidInput(f"模型文件不可读：{key}（{exc}）") from exc
        hit = self._cache.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        model = MarkovModel.load(key)
        self._cache[key] = (mtime, model)
        return model

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


class Engine:
    """Resolve channels and drive them. Stateless per call, cached across calls."""

    def __init__(self, config: Config | None = None, *,
                 model: MarkovModel | None = None,
                 models: Iterable[MarkovModel] = ()) -> None:
        self.config = config or Config()
        self._cache = ModelCache()
        #: Models supplied directly (tests, embedding callers). They win over
        #: disk discovery because the caller was explicit.
        self._injected: list[MarkovModel] = ([model] if model is not None else [])
        self._injected.extend(m for m in models)

    # ------------------------------------------------------------------ models

    def models(self) -> list[MarkovModel]:
        """Every model this engine can use, injected ones first."""
        found: list[MarkovModel] = list(self._injected)
        seen = {id(m) for m in found}
        for path in self._model_paths():
            try:
                loaded = self._cache.load(path)
            except (InvalidInput, OSError, ValueError, KeyError):
                # A corrupt JSON in the model folder must not break image work.
                continue
            if id(loaded) not in seen:
                found.append(loaded)
                seen.add(id(loaded))
        return found

    def _model_paths(self) -> list[Path]:
        sources: list[Path] = list(self.config.extra_models)
        if self.config.model_dir:
            sources.append(self.config.model_dir)
        try:
            return discover_model_paths(sources, recursive=True) if sources else []
        except OSError:
            return []

    def default_model(self) -> MarkovModel | None:
        """The model used for a newly registered Chinese channel."""
        found = self.models()
        return found[0] if found else None

    # ---------------------------------------------------------------- channels

    #: Ids that ``register_defaults`` owns. Anything else registered by a plugin
    #: must survive a rebuild -- otherwise an external channel added after the
    #: Engine was constructed would vanish on the next ``channels()`` call.
    BUILTIN_IDS = frozenset({"image", "gif", "chinese"})

    def _registered(self):
        """The channel registry, with built-ins refreshed and plugins preserved.

        Rebuilding the registry on every call is what keeps ``chinese`` in sync
        with the models on disk. Doing it *destructively* was a real defect: the
        registry is process-global, so a plugin registered by the embedding
        application disappeared the next time anything listed channels.
        """
        plugins = [channel for channel in registry.all_channels()
                   if channel.id not in self.BUILTIN_IDS]
        registry.register_defaults(self.default_model)
        for channel in plugins:
            registry.register(channel)
        return registry.all_channels()

    def channels(self):
        """Registered channels, with ``chinese`` present only if a model exists."""
        return self._registered()

    def channel(self, channel_id: str):
        self.channels()                     # ensure registration happened
        return registry.get(channel_id)

    def describe_channels(self) -> list[dict[str, Any]]:
        self.channels()
        return registry.describe_all()

    # --------------------------------------------------------------- operations

    def probe(self, channel_id: str, carrier: Carrier, payload_bytes: int) -> Feasibility:
        return self.channel(channel_id).probe(carrier, payload_bytes)

    def embed(self, channel_id: str, carrier: Carrier, payload: bytes, key: bytes,
              options: Any = None, progress: ProgressFn | None = None) -> Envelope:
        channel = self.channel(channel_id)
        # Measured here rather than taken from the report: the report's
        # elapsed_ms is per-channel and may be absent or zero for channels that
        # do not time themselves, which would make the metric useless.
        start = time.perf_counter()
        try:
            envelope = channel.embed(carrier, payload, key,
                                     coerce(channel.options_schema, options), progress)
        except Exception as exc:
            observability.event("engine", "embed_failed", level=logging.WARNING,
                                channel=channel_id, error=type(exc).__name__)
            raise
        elapsed = (time.perf_counter() - start) * 1000
        observability.METRICS.incr("embed", channel=channel_id)
        observability.METRICS.observe("embed_ms", elapsed, channel=channel_id)
        observability.event("engine", "embed", channel=channel_id,
                            payload_bytes=len(payload),
                            carrier_bytes=envelope.report.carrier_bytes,
                            elapsed_ms=round(elapsed, 1))
        return envelope

    def extract(self, channel_id: str, carrier: Carrier, key: bytes,
                options: Any = None, progress: ProgressFn | None = None) -> bytes:
        channel = self.channel(channel_id)
        start = time.perf_counter()
        try:
            payload = channel.extract(carrier, key,
                                      coerce(channel.options_schema, options), progress)
        except Exception as exc:
            observability.event("engine", "extract_failed", level=logging.WARNING,
                                channel=channel_id, error=type(exc).__name__)
            raise
        elapsed = (time.perf_counter() - start) * 1000
        observability.METRICS.incr("extract", channel=channel_id)
        observability.METRICS.observe("extract_ms", elapsed, channel=channel_id)
        observability.event("engine", "extract", channel=channel_id,
                            payload_bytes=len(payload), elapsed_ms=round(elapsed, 1))
        return payload

    # -------------------------------------------------------------- convenience

    def codec(self) -> MarkovStegoCodec:
        """The Chinese codec for message-level work (identify, decode-with-models).

        ``Channel.extract`` handles the one-model case; this exposes the codec
        for callers that must try *several* models, which is the normal shape of
        ``decode`` because the writer's model may not be the default one.
        """
        model = self.default_model()
        if model is None:
            raise ChannelUnavailable(
                "中文信道需要先训练模型",
                hints=("运行 markovcryptor train --corpus <语料> 生成模型",),
            )
        return MarkovStegoCodec(model)

    def decode_text(self, covertext: str, key: bytes = b"",
                    progress: ProgressFn | None = None) -> tuple[bytes, MarkovModel]:
        """Decode hidden payload from Chinese text, identifying the model.

        Tries every loaded model and returns the one that matched, because a
        cover text carries no model name -- only a fingerprint the frame is
        keyed with. Keeping this on the engine (rather than the channel) is
        deliberate: a channel speaks one model, the engine knows them all.
        """
        models = self.models()
        if not models:
            raise ChannelUnavailable(
                "中文信道需要先训练模型",
                hints=("运行 markovcryptor train --corpus <语料> 生成模型",),
            )
        return decode_with_models(covertext, models, key or None, progress)

    def identify(self, covertext: str) -> MarkovModel | None:
        """Which loaded model wrote ``covertext``, judged from the frame alone."""
        return identify_model(covertext, self.models())


__all__ = ["Config", "Engine", "ModelCache"]
