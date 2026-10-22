"""Text-channel models: loaded on demand, indexed by fingerprint.

The Chinese channel does not modify a cover text, it *generates* one from a
Markov model. So the model is not optional decoration -- it is the carrier. Both
sides must hold a model, and the receiver must work out *which* model produced a
given cover text before it can decode it.

That identification is already solved inside the frame check code: ``codec.py``
keys the check with the model's fingerprint, so a decoder holding several models
tries each and the right one verifies. No negotiation, no extra bytes on the
wire. What this module adds is the part that makes that usable in a client:

**Do not load everything at startup.** A measured model costs ~0.53s and ~18MB.
Four models would be 2 seconds and 70MB before the window even appears, on every
launch. Users would notice, and would blame the program rather than the models.

**Load the smallest thing first.** Identification needs only the frame check,
which needs the fingerprint. But computing a fingerprint requires the whole model
(``model.py:377`` hashes vocabulary, counts and transitions), so there is no
cheap pre-filter -- the honest approach is to load one model eagerly (the most
recently used, which is usually the right one) and load the rest in the
background, retrying identification as they arrive.

**A missing model is not an error.** If no model matches, the message is just
ordinary text. The channel must stay silent about that -- see ``CLIENT.md`` §7.1.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..observability import event

#: Model files carry this in ``format``; anything else is not ours.
_MODEL_SUFFIX = ".json"


@dataclass
class ModelEntry:
    """One model file, possibly not yet loaded."""

    path: Path
    name: str = ""
    #: ``None`` until :meth:`ModelLibrary.load` has read it.
    model: object | None = None
    #: Seconds spent loading, for the log line that explains startup latency.
    load_seconds: float = 0.0
    #: Set when loading failed, so we do not retry a broken file forever.
    error: str = ""

    @property
    def loaded(self) -> bool:
        return self.model is not None

    @property
    def fingerprint(self) -> bytes | None:
        return self.model.fingerprint if self.model is not None else None


class ModelLibrary:
    """Model files on disk, loaded lazily and kept ready for identification.

    Thread-safe: the UI thread asks "can we send text yet", while the background
    loader is filling the list.
    """

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = Path(folder) if folder else None
        self._entries: list[ModelEntry] = []
        self._lock = threading.RLock()
        self._loading = False

    # ------------------------------------------------------------------ inventory

    def scan(self) -> int:
        """List model files without loading them. Returns how many were found."""
        if self.folder is None or not self.folder.is_dir():
            return 0
        found: list[ModelEntry] = []
        for path in sorted(self.folder.glob(f"*{_MODEL_SUFFIX}")):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            found.append(ModelEntry(path=path, name=path.stem))
        with self._lock:
            # 保留已经加载过的对象，避免重复付 0.5 秒。
            known = {str(e.path): e for e in self._entries}
            self._entries = [known.get(str(e.path), e) for e in found]
            return len(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def loaded_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries if e.loaded)

    def entries(self) -> list[ModelEntry]:
        with self._lock:
            return list(self._entries)

    def loaded_models(self) -> list:
        """Only the models that finished loading -- what identification can use."""
        with self._lock:
            return [e.model for e in self._entries if e.loaded]

    @property
    def usable(self) -> bool:
        """True when at least one model is ready to encode with."""
        return self.loaded_count > 0

    def status(self) -> dict:
        with self._lock:
            return {
                "folder": str(self.folder) if self.folder else "",
                "total": len(self._entries),
                "loaded": sum(1 for e in self._entries if e.loaded),
                "failed": sum(1 for e in self._entries if e.error),
                "names": [e.name for e in self._entries if e.loaded],
                "loading": self._loading,
            }

    # ------------------------------------------------------------------- loading

    def load(self, path: Path) -> ModelEntry | None:
        """Load one model file. Returns its entry, or None if it failed."""
        from ..model import MarkovModel

        with self._lock:
            entry = next((e for e in self._entries if e.path == path), None)
            if entry is None:
                entry = ModelEntry(path=path, name=path.stem)
                self._entries.append(entry)
            if entry.error:
                # 之前加载失败过：不再重试，也不假装成功。
                # 返回 entry 会让调用方以为加载好了，进而把它算进 loaded_count。
                return None
            if entry.loaded:
                return entry

        started = time.monotonic()
        try:
            model = MarkovModel.load(path)
        except Exception as exc:                    # noqa: BLE001 - user file
            with self._lock:
                entry.error = str(exc)
            # 坏文件记一次就够，不要每次识别都重试一遍。
            event("models", "load_failed", level=logging.WARNING,
                  path=str(path), error=exc)
            return None

        elapsed = time.monotonic() - started
        with self._lock:
            entry.model = model
            entry.load_seconds = elapsed
            entry.name = getattr(model, "name", "") or entry.name
        event("models", "loaded", path=str(path), seconds=round(elapsed, 3),
              model_name=entry.name)
        return entry

    def load_first(self) -> ModelEntry | None:
        """Load exactly one model -- enough to send, fast enough to wait for.

        The user only needs *a* model to start sending. Loading the rest is the
        background loader's job.
        """
        self.scan()
        for entry in self.entries():
            if entry.loaded:
                return entry
            if entry.error:
                continue
            loaded = self.load(entry.path)
            if loaded is not None:
                return loaded
        return None

    def load_all(self, *, on_progress=None) -> int:
        """Load every scanned model. Returns how many are loaded afterwards."""
        self.scan()
        count = 0
        for entry in self.entries():
            if entry.loaded:
                count += 1
                continue
            if entry.error:
                continue
            if self.load(entry.path) is not None:
                count += 1
            if on_progress is not None:
                on_progress(count, len(self))
        return count

    def load_in_background(self) -> threading.Thread:
        """Load the remaining models without blocking the UI.

        Identification needs every model to be loaded to be sure it will find a
        match. Doing that up front costs seconds; doing it in the background
        means the window appears immediately and the first few inbound messages
        may simply be treated as ordinary text -- which is the correct fallback
        anyway, and invisible to the peer.
        """
        with self._lock:
            if self._loading:
                return threading.current_thread()
            self._loading = True

        def worker():
            try:
                self.load_all()
            except Exception as exc:                # noqa: BLE001
                event("models", "background_load_failed", level=logging.WARNING,
                      error=exc)
            finally:
                with self._lock:
                    self._loading = False
                event("models", "background_load_done",
                      loaded=self.loaded_count, total=len(self))

        thread = threading.Thread(target=worker, name="model-loader", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------- identification

    def identify(self, covertext: str):
        """Which loaded model wrote this cover text, or None.

        Returns None both for "ordinary text" and for "a model I do not have".
        The caller must treat those identically: reply to neither.
        """
        from ..codec import identify_model

        models = self.loaded_models()
        if not models:
            return None
        try:
            return identify_model(covertext, models)
        except Exception as exc:                    # noqa: BLE001
            event("models", "identify_failed", level=logging.WARNING, error=exc)
            return None


class MultiModelTextChannel:
    """A text channel that holds every loaded model and picks per message.

    ``Terminal`` expects an object with ``embed_text``/``extract_text``. Sending
    uses the preferred model; receiving tries all loaded models via the frame
    check, so a peer using a different model is still understood -- that is what
    makes "no model negotiation needed" true in practice.
    """

    def __init__(self, library: ModelLibrary, *, preferred=None) -> None:
        self.library = library
        self._preferred = preferred

    @property
    def preferred(self):
        """Model used for outgoing messages: explicit choice, else first loaded."""
        if self._preferred is not None:
            return self._preferred
        models = self.library.loaded_models()
        return models[0] if models else None

    def use(self, model) -> None:
        self._preferred = model

    def embed_text(self, payload: bytes):
        """Encode ``payload`` into a Chinese cover text.

        Returns ``(covertext, stats)`` like the other channel adapters; the stats
        are the codec's own report so the caller can show how much it expanded.
        """
        model = self.preferred
        if model is None:
            from ..core.errors import InvalidInput
            raise InvalidInput(
                "还没有可用的文本模型",
                hints=("在界面上训练或导入一个模型，或改用密文图片发送",),
            )
        from ..codec import MarkovStegoCodec

        codec = MarkovStegoCodec(model)
        covertext, stats = codec.encode_with_stats(payload)
        return covertext, stats

    def extract_text(self, covertext: str) -> bytes:
        """Decode using whichever loaded model recognises the frame.

        Returns b"" when nothing matches, including when it is simply ordinary
        text. Never raises: a mismatch is the normal case, not an error.
        """
        from ..codec import MarkovStegoCodec, identify_model
        from ..core.errors import StegoError

        models = self.library.loaded_models()
        if not models:
            return b""
        try:
            model = identify_model(covertext, models)
            if model is None:
                return b""
            return MarkovStegoCodec(model).decode(covertext)
        except StegoError:
            return b""
        except Exception:                           # noqa: BLE001 - never surface
            return b""

