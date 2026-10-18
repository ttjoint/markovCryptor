from __future__ import annotations

from array import array
from collections import Counter
from datetime import datetime
from itertools import accumulate, chain
import hashlib
import json
from pathlib import Path
import random
import re
import time
from typing import Any, Iterable, Optional

from .core.arith import MAX_TOTAL
from .compression import TextModel
from .core.progress import ProgressFn, report, stage
from .punctuation import PunctuationModel, filter_vocabulary, segment_corpus

MODEL_FORMAT = "markovcryptor-model-v3"
_BOS = "<BOS>"
# Total weight of one transition table; leaves room for the +1 floor of every symbol.
_TABLE_RESOLUTION = MAX_TOTAL // 2


def _path_inputs(inputs: Iterable[str | Path] | str | Path) -> Iterable[str | Path]:
    if isinstance(inputs, (str, Path)):
        return (inputs,)
    return inputs


def default_model_dir(base_dir: str | Path | None = None) -> Path:
    """Return the local model directory shared by the CLI and GUI."""
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[1]
    return root / "models"


def discover_corpus_files(
    inputs: Iterable[str | Path] | str | Path,
    *,
    recursive: bool = True,
    suffixes: tuple[str, ...] = (".txt",),
) -> list[Path]:
    """Expand text files and directories into a stable, de-duplicated file list.

    Directories are scanned recursively by default. Files with other extensions are
    accepted when passed explicitly, which keeps the API useful for corpus exports
    that do not use the conventional ``.txt`` suffix.
    """
    normalized_suffixes = {suffix.lower() for suffix in suffixes}
    found: dict[str, Path] = {}
    for raw in _path_inputs(inputs):
        source = Path(raw).expanduser()
        if source.is_file():
            found[str(source.resolve()).casefold()] = source
            continue
        if source.is_dir():
            iterator = source.rglob("*") if recursive else source.glob("*")
            for candidate in iterator:
                if candidate.is_file() and candidate.suffix.lower() in normalized_suffixes:
                    found[str(candidate.resolve()).casefold()] = candidate
            continue
        raise FileNotFoundError(f"corpus path does not exist: {source}")
    files = sorted(found.values(), key=lambda path: str(path.resolve()).casefold())
    if not files:
        raise ValueError("no corpus text files were found")
    return files


def read_corpus_files(inputs: Iterable[str | Path] | str | Path, *, recursive: bool = True) -> str:
    """Read one or more corpus files, including directory inputs, as one corpus."""
    return _read_sources(discover_corpus_files(inputs, recursive=recursive))[0]


def read_text_file(path: str | Path) -> str:
    """Read a text file of unknown encoding with LF line endings."""
    text, _ = decode_corpus_bytes(Path(path).read_bytes())
    return text.replace("\r\n", "\n").replace("\r", "\n")


_BOM_ENCODINGS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)
# Chinese novel downloads are mostly UTF-8 or GBK/GB18030 (a GBK superset); Big5 covers
# traditional-Chinese sources.
_FALLBACK_ENCODINGS = ("utf-8", "gb18030", "big5")


def decode_corpus_bytes(data: bytes) -> tuple[str, str]:
    """Decode corpus bytes, guessing the encoding. Returns ``(text, encoding)``.

    BOMs are honoured first, then each fallback encoding is tried strictly. If none
    decodes cleanly (e.g. a file with a few corrupt bytes), the candidate producing
    the fewest replacement characters wins.
    """
    for bom, encoding in _BOM_ENCODINGS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace"), encoding
    for encoding in _FALLBACK_ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    candidates = [(data.decode(enc, errors="replace"), enc) for enc in _FALLBACK_ENCODINGS]
    return min(candidates, key=lambda item: item[0].count("�"))


def _read_sources(
    files: Iterable[Path], progress: Optional[ProgressFn] = None
) -> tuple[str, list[dict[str, Any]]]:
    """Read corpus files and describe each one for the model's provenance metadata."""
    texts: list[str] = []
    sources: list[dict[str, Any]] = []
    files = list(files)
    for number, path in enumerate(files):
        report(progress, number / len(files), f"读取语料 {path.name}")
        data = path.read_bytes()
        text, encoding = decode_corpus_bytes(data)
        # Match text-mode reading: normalise CRLF / bare CR line endings to LF.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        texts.append(text)
        sources.append(
            {"name": path.name, "bytes": len(data), "characters": len(text), "encoding": encoding}
        )
    return "\n".join(texts), sources


def model_file_name(name: str) -> str:
    """Turn a display name into a safe JSON file name."""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "-", name).strip("-.") or "model"
    return f"{cleaned[:80]}.json"


def unique_model_path(directory: str | Path, name: str) -> Path:
    """Return a not-yet-existing model path inside ``directory`` derived from ``name``."""
    base = Path(directory) / model_file_name(name)
    candidate, suffix = base, 2
    while candidate.exists():
        candidate = base.with_name(f"{base.stem}-{suffix}.json")
        suffix += 1
    return candidate


def discover_model_paths(inputs: Iterable[str | Path] | str | Path, *, recursive: bool = True) -> list[Path]:
    """Expand model files/directories into a stable list for multi-model decoding."""
    found: dict[str, Path] = {}
    for raw in _path_inputs(inputs):
        source = Path(raw).expanduser()
        if source.is_file():
            found[str(source.resolve()).casefold()] = source
            continue
        if source.is_dir():
            iterator = source.rglob("*.json") if recursive else source.glob("*.json")
            for candidate in iterator:
                if candidate.is_file():
                    found[str(candidate.resolve()).casefold()] = candidate
            continue
        raise FileNotFoundError(f"model path does not exist: {source}")
    paths = sorted(found.values(), key=lambda path: str(path.resolve()).casefold())
    if not paths:
        raise ValueError("no model files were found")
    return paths


def load_models(inputs: Iterable[str | Path] | str | Path, *, recursive: bool = True) -> list["MarkovModel"]:
    """Load all model files represented by file/directory inputs."""
    return [MarkovModel.load(path) for path in discover_model_paths(inputs, recursive=recursive)]


class MarkovModel:
    """Sparse first-order Markov model over hanzi only, plus a punctuation layer.

    The payload is carried exclusively by the hanzi chain. Punctuation, quotes,
    brackets and paragraph breaks are statistics kept in :attr:`punctuation`
    and applied by :meth:`render` after encoding; they never carry data.
    :attr:`text` holds corpus character statistics that prime payload
    compression. Transition tables are built lazily, only for contexts used.
    """

    def __init__(
        self,
        vocabulary: Iterable[str],
        global_counts: dict[str, int],
        transitions: dict[str, dict[str, int]],
        smoothing: float = 1.0,
        punctuation: PunctuationModel | None = None,
        text: TextModel | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.vocabulary = tuple(vocabulary)
        if len(self.vocabulary) < 64:
            raise ValueError("the vocabulary must contain at least 64 symbols")
        if len(set(self.vocabulary)) != len(self.vocabulary):
            raise ValueError("the vocabulary contains duplicate symbols")
        if not smoothing > 0:
            raise ValueError("smoothing must be positive")
        self.global_counts = {key: int(value) for key, value in global_counts.items()}
        self.transitions = {
            context: {key: int(value) for key, value in counts.items()}
            for context, counts in transitions.items()
        }
        self.smoothing = float(smoothing)
        self.punctuation = punctuation
        self.text = text
        # Provenance only (name, sources, training time); never affects coding.
        self.metadata: dict[str, Any] = dict(metadata or {})
        self._fingerprint: bytes | None = None
        self._tables: dict[str, tuple[array, int]] = {}
        self._vocabulary_set = set(self.vocabulary)
        self._unigram_total = sum(self.global_counts.values())
        self._validate()

    @classmethod
    def train(
        cls,
        corpus: str,
        vocabulary_size: int = 3000,
        smoothing: float = 1.0,
        *,
        name: str | None = None,
        sources: list[dict[str, Any]] | None = None,
        progress: Optional[ProgressFn] = None,
    ) -> "MarkovModel":
        started = time.perf_counter()
        if vocabulary_size < 64:
            raise ValueError("vocabulary_size must be at least 64")
        corpus = corpus.replace("\r\n", "\n").replace("\r", "\n")
        report(progress, 0.0, "切分汉字与标点")
        hanzi, gaps = segment_corpus(corpus)
        if len(hanzi) < 2:
            raise ValueError("corpus must contain at least two Chinese characters")

        report(progress, 0.25, "统计字频")
        frequencies = Counter(hanzi)
        vocabulary = [
            char
            for char, _ in sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))
        ][:vocabulary_size]
        if len(vocabulary) < 64:
            raise ValueError(
                f"corpus only produced {len(vocabulary)} distinct hanzi; at least 64 are required"
            )
        filtered, gaps = filter_vocabulary(hanzi, gaps, set(vocabulary))
        if len(filtered) < 2:
            raise ValueError("corpus has too few characters inside the selected vocabulary")

        report(progress, 0.4, "统计汉字转移")
        global_counts = Counter(filtered)
        transitions: dict[str, dict[str, int]] = {}
        for (previous, char), count in Counter(zip(chain((_BOS,), filtered), filtered)).items():
            transitions.setdefault(previous, {})[char] = count

        report(progress, 0.55, "统计标点")
        punctuation = PunctuationModel.train(filtered, gaps)
        report(progress, 0.75, "统计压缩用字符模型")
        text = TextModel.train(corpus)
        model = cls(
            vocabulary=vocabulary,
            global_counts=dict(global_counts),
            transitions=transitions,
            smoothing=smoothing,
            punctuation=punctuation,
            text=text,
            metadata={
                "name": name or "",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "corpus_characters": len(corpus),
                "hanzi_characters": len(hanzi),
                "distinct_characters": len(frequencies),
                "requested_vocabulary_size": vocabulary_size,
                "sources": list(sources or []),
            },
        )
        model.metadata["training_seconds"] = round(time.perf_counter() - started, 3)
        report(progress, 1.0, "训练完成")
        return model

    @classmethod
    def train_from_files(
        cls,
        inputs: Iterable[str | Path] | str | Path,
        vocabulary_size: int = 3000,
        smoothing: float = 1.0,
        *,
        recursive: bool = True,
        name: str | None = None,
        progress: Optional[ProgressFn] = None,
    ) -> "MarkovModel":
        """Train one model from multiple text files and/or directory inputs."""
        files = discover_corpus_files(inputs, recursive=recursive)
        corpus, sources = _read_sources(files, stage(progress, 0.0, 0.15))
        if not name:
            name = files[0].stem if len(files) == 1 else f"{files[0].stem} 等 {len(files)} 个文件"
        return cls.train(
            corpus,
            vocabulary_size=vocabulary_size,
            smoothing=smoothing,
            name=name,
            sources=sources,
            progress=stage(progress, 0.15, 1.0),
        )

    @classmethod
    def train_many(
        cls,
        inputs: Iterable[str | Path] | str | Path,
        vocabulary_size: int = 3000,
        smoothing: float = 1.0,
        *,
        recursive: bool = True,
        progress: Optional[ProgressFn] = None,
    ) -> list[tuple[Path, "MarkovModel"]]:
        """Train one model per discovered text file.

        Returning the source path with each model lets callers preserve useful
        names when writing a model directory and avoids losing provenance.
        Progress is apportioned by file size.
        """
        files = discover_corpus_files(inputs, recursive=recursive)
        sizes = [max(path.stat().st_size, 1) for path in files]
        total = sum(sizes)
        done = 0
        trained: list[tuple[Path, MarkovModel]] = []
        for path, size in zip(files, sizes):
            scoped = stage(progress, done / total, (done + size) / total)
            if scoped is not None:
                inner = scoped
                scoped = lambda fraction, message, inner=inner, label=path.stem: inner(fraction, f"{label}：{message}")
            model = cls.train_from_files(
                [path], vocabulary_size=vocabulary_size, smoothing=smoothing, name=path.stem, progress=scoped
            )
            trained.append((path, model))
            done += size
        return trained

    def _validate(self) -> None:
        unknown_global = set(self.global_counts) - self._vocabulary_set
        if unknown_global:
            raise ValueError(f"global counts contain unknown symbols: {unknown_global!r}")
        for context, counts in self.transitions.items():
            if context != _BOS and context not in self._vocabulary_set:
                raise ValueError(f"transition context {context!r} is outside vocabulary")
            unknown = set(counts) - self._vocabulary_set
            if unknown:
                raise ValueError(f"transition table contains unknown symbols: {unknown!r}")
            if any(value < 0 for value in counts.values()):
                raise ValueError("transition counts cannot be negative")

    def table(self, previous: str) -> tuple[array, int]:
        """Cumulative frequencies over :attr:`vocabulary` after ``previous``, and their total.

        Witten-Bell smoothing backs off to unigram frequencies:
        ``P(b|a) = (c(a,b) + λ·P(b)) / (c(a) + λ)`` with ``λ = smoothing × distinct
        successors of a``. Unseen pairs therefore favour common characters
        instead of being uniform, which keeps the text readable. Every symbol
        keeps a weight of at least 1 so that any hanzi string stays decodable.
        Weights are scaled to the coder precision; tables are built on demand.
        """
        table = self._tables.get(previous)
        if table is None:
            unigram = self.global_counts
            counts = self.transitions.get(previous) or {}
            unigram_total = self._unigram_total
            backoff = max(1, round(self.smoothing * max(len(counts), 1)))
            # Exact rational weights c·G + λ·g, then scaled into the coder's range.
            raw = [counts.get(symbol, 0) * unigram_total + backoff * unigram.get(symbol, 0) for symbol in self.vocabulary]
            raw_total = sum(raw) or 1
            scale = _TABLE_RESOLUTION
            weights = [weight * scale // raw_total + 1 for weight in raw]
            cumulative = array("Q", accumulate(weights, initial=0))
            table = self._tables[previous] = (cumulative, cumulative[-1])
        return table

    @property
    def fingerprint(self) -> bytes:
        """32-byte digest of everything that affects coding (not the metadata).

        The codec keys the hidden frame's check code with this value, so a cover
        text identifies its model without carrying a readable model ID. Renaming
        a model or editing its metadata therefore keeps old cover texts decodable.
        """
        if self._fingerprint is None:
            canonical = json.dumps(
                {
                    "smoothing": self.smoothing,
                    "vocabulary": list(self.vocabulary),
                    "global_counts": self.global_counts,
                    "transitions": self.transitions,
                    "text": self.text.to_dict() if self.text is not None else None,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self._fingerprint = hashlib.blake2s(canonical.encode("utf-8"), person=b"mkcmodel").digest()
        return self._fingerprint

    @property
    def model_id(self) -> str:
        """Short printable form of :attr:`fingerprint`."""
        return self.fingerprint.hex()[:12]

    @property
    def name(self) -> str:
        return str(self.metadata.get("name") or "")

    @property
    def training_tokens(self) -> int:
        """Corpus characters that remained after vocabulary filtering."""
        return sum(self.global_counts.values())

    @property
    def context_count(self) -> int:
        return sum(1 for context in self.transitions if context != _BOS)

    @property
    def transition_pairs(self) -> int:
        return sum(len(counts) for counts in self.transitions.values())

    def render(
        self, hanzi: str, rng: random.Random | None = None, progress: Optional[ProgressFn] = None
    ) -> str:
        """Add corpus-style punctuation and paragraph breaks to a bare hanzi string."""
        if self.punctuation is None:
            return hanzi
        return self.punctuation.render(hanzi, self.transitions, rng, progress)

    def to_dict(self) -> dict:
        payload = {
            "format": MODEL_FORMAT,
            "smoothing": self.smoothing,
            "vocabulary": list(self.vocabulary),
            "global_counts": self.global_counts,
            "transitions": self.transitions,
            "metadata": self.metadata,
        }
        if self.punctuation is not None:
            payload["punctuation"] = self.punctuation.to_dict()
        if self.text is not None:
            payload["text"] = self.text.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "MarkovModel":
        if payload.get("format") != MODEL_FORMAT:
            raise ValueError("unsupported model format; retrain the model with this version")
        punctuation = payload.get("punctuation")
        text = payload.get("text")
        return cls(
            vocabulary=payload["vocabulary"],
            global_counts=payload["global_counts"],
            transitions=payload["transitions"],
            smoothing=float(payload["smoothing"]),
            punctuation=PunctuationModel.from_dict(punctuation) if punctuation else None,
            text=TextModel.from_dict(text) if text else None,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "MarkovModel":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload)
