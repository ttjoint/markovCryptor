"""Payload compression applied before (optional) encryption and steganography.

Several methods are tried and the smallest output wins:

* ``TEXT``: a character model for UTF-8 text primed with corpus statistics. An
  adaptive order-2 PPM layer learns the message itself; on escape it falls back
  to a static order-1 table, then a static order-0 table (both trained on the
  corpus, stored in the model file), then a raw code point. Because the static
  layers already know the language, even a 20-character message compresses to
  roughly a byte per hanzi, where general-purpose compressors barely break even.
* ``DEFLATE`` / ``BZ2`` / ``LZMA`` from the standard library, for binary data
  and very repetitive input.
* ``STORE`` when nothing helps (e.g. already-compressed files).

Compression must happen before AES: ciphertext is indistinguishable from random
and cannot be compressed afterwards.
"""

from __future__ import annotations

import bz2
from bisect import bisect_right
from collections import Counter
from itertools import accumulate
import lzma
from typing import Any, Iterable, Mapping, Optional
import zlib

from .arith import MAX_TOTAL, ArithmeticDecoder, ArithmeticEncoder, BitReader
from .progress import REPORT_EVERY, ProgressFn, report, stage

STORE, TEXT, DEFLATE, BZ2, LZMA = range(5)
METHOD_NAMES = {STORE: "不压缩", TEXT: "语料文本模型", DEFLATE: "Deflate", BZ2: "BZip2", LZMA: "LZMA"}

_LZMA_FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 9}]
_MIN_COUNT = 2  # corpus characters / pairs seen fewer times are dropped from the static tables
_MAX_ALPHABET = 16000
_ALWAYS = "\n\t" + "".join(chr(code) for code in range(32, 127))  # ASCII is always codable
_CODE_POINTS = 0x110000
_TEXT_CRLF = 1  # TEXT stream flag: input used CRLF line endings throughout


def write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(data: bytes, offset: int = 0) -> tuple[int, int] | None:
    """Return ``(value, next_offset)``, or None if ``data`` ends mid-varint."""
    value = shift = 0
    for index in range(offset, min(len(data), offset + 10)):
        byte = data[index]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index + 1
        shift += 7
    if len(data) - offset >= 10:
        raise ValueError("varint is too long")
    return None


def _scaled(counts: list[int], limit: int) -> list[int]:
    """Deterministically shrink counts so their sum stays below ``limit``."""
    total = sum(counts)
    if total < limit:
        return counts
    return [max(1, count * (limit // 2) // total) for count in counts]


class _StaticRow:
    __slots__ = ("index", "cumulative", "total")

    def __init__(self, symbols: str, counts: list[int], escape: int) -> None:
        counts = _scaled([*counts, escape], MAX_TOTAL)
        self.index = {symbol: position for position, symbol in enumerate(symbols)}
        self.cumulative = [0, *accumulate(counts)]
        self.total = self.cumulative[-1]


class TextModel:
    """Static corpus character statistics used to prime text compression."""

    def __init__(self, alphabet: str, unigram: list[int], bigram: Mapping[str, Iterable[Any]]) -> None:
        if len(alphabet) != len(unigram) or len(set(alphabet)) != len(alphabet):
            raise ValueError("text model alphabet is inconsistent")
        self.alphabet = alphabet
        self.unigram = [int(count) for count in unigram]
        self.bigram: dict[str, tuple[str, list[int]]] = {}
        for previous, (symbols, counts) in bigram.items():
            if len(symbols) != len(counts):
                raise ValueError("text model bigram row is inconsistent")
            self.bigram[previous] = (symbols, [int(count) for count in counts])
        self._order0 = _StaticRow(alphabet, self.unigram, max(1, len(alphabet) // 8))
        self._rows: dict[str, _StaticRow | None] = {}

    @classmethod
    def train(cls, text: str) -> "TextModel":
        counts = Counter(text)
        counts.pop("\r", None)
        frequent = [char for char, count in counts.most_common(_MAX_ALPHABET) if count >= _MIN_COUNT]
        alphabet = "".join(dict.fromkeys(frequent + list(_ALWAYS)))
        keep = set(alphabet)
        unigram = [counts.get(char, 0) + 1 for char in alphabet]
        rows: dict[str, dict[str, int]] = {}
        for (a, b), count in Counter(zip(text, text[1:])).items():
            if count >= _MIN_COUNT and a in keep and b in keep:
                rows.setdefault(a, {})[b] = count
        bigram = {
            a: ("".join(row), list(row.values()))
            for a, row in ((a, dict(sorted(row.items(), key=lambda item: -item[1]))) for a, row in rows.items())
        }
        return cls(alphabet, unigram, bigram)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alphabet": self.alphabet,
            "unigram": self.unigram,
            "bigram": {a: [symbols, counts] for a, (symbols, counts) in self.bigram.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TextModel":
        return cls(payload["alphabet"], list(payload["unigram"]), payload["bigram"])

    @property
    def pair_count(self) -> int:
        return sum(len(symbols) for symbols, _ in self.bigram.values())

    def _row(self, previous: str) -> _StaticRow | None:
        try:
            return self._rows[previous]
        except KeyError:
            entry = self.bigram.get(previous)
            row = _StaticRow(entry[0], entry[1], 2 * len(entry[0])) if entry else None
            self._rows[previous] = row
            return row

    # ------------------------------------------------------------ coding

    def compress(self, text: str, limit: Optional[int] = None, progress: Optional[ProgressFn] = None) -> bytes | None:
        """Compress ``text``; returns None as soon as the output would exceed ``limit`` bytes."""
        flags = 0
        if "\r\n" in text and "\r" not in text.replace("\r\n", "") and "\n" not in text.replace("\r\n", ""):
            text, flags = text.replace("\r\n", "\n"), _TEXT_CRLF
        header = bytes([flags]) + write_varint(len(text))
        encoder = ArithmeticEncoder()
        encode = encoder.encode
        contexts: dict[str, list] = {}
        order0 = self._order0
        limit_bits = None if limit is None else (limit - len(header)) * 8
        p2 = p1 = "\n"
        for position, char in enumerate(text):
            if position % REPORT_EVERY == 0:
                if limit_bits is not None and encoder.bits_emitted > limit_bits:
                    return None
                report(progress, position / max(len(text), 1), "压缩文本")
            key = p2 + p1
            context = contexts.get(key)
            coded = False
            if context is not None:
                index, counts, total, symbols = context
                slot = index.get(char)
                distinct = len(counts)
                if slot is not None:
                    low = sum(counts[:slot])
                    encode(low, low + counts[slot], total + distinct)
                    counts[slot] += 1
                    coded = True
                else:
                    encode(total, total + distinct, total + distinct)
                    index[char] = distinct
                    counts.append(1)
                    symbols.append(char)
                context[2] = total + 1
            else:
                contexts[key] = [{char: 0}, [1], 1, [char]]
            if not coded:
                row = self._row(p1)
                if row is not None:
                    slot = row.index.get(char)
                    if slot is not None:
                        encode(row.cumulative[slot], row.cumulative[slot + 1], row.total)
                        coded = True
                    else:
                        encode(row.cumulative[-2], row.total, row.total)
            if not coded:
                slot = order0.index.get(char)
                if slot is not None:
                    encode(order0.cumulative[slot], order0.cumulative[slot + 1], order0.total)
                else:
                    encode(order0.cumulative[-2], order0.total, order0.total)
                    code = ord(char)
                    encode(code, code + 1, _CODE_POINTS)
            p2, p1 = p1, char
        data = header + encoder.finish()
        return None if limit is not None and len(data) > limit else data

    def decompress(self, data: bytes, progress: Optional[ProgressFn] = None) -> str:
        if not data:
            raise ValueError("compressed text is empty")
        flags = data[0]
        parsed = read_varint(data, 1)
        if parsed is None or flags & ~_TEXT_CRLF:
            raise ValueError("compressed text header is corrupt")
        length, offset = parsed
        decoder = ArithmeticDecoder(BitReader(data[offset:]))
        target, consume = decoder.target, decoder.consume
        contexts: dict[str, list] = {}
        order0 = self._order0
        out: list[str] = []
        p2 = p1 = "\n"
        for position in range(length):
            if position % REPORT_EVERY == 0:
                report(progress, position / max(length, 1), "解压文本")
            key = p2 + p1
            context = contexts.get(key)
            char = None
            if context is not None:
                index, counts, total, symbols = context
                distinct = len(counts)
                value = target(total + distinct)
                if value < total:
                    cumulative = list(accumulate(counts))
                    slot = bisect_right(cumulative, value)
                    high = cumulative[slot]
                    consume(high - counts[slot], high, total + distinct)
                    counts[slot] += 1
                    char = symbols[slot]
                else:
                    consume(total, total + distinct, total + distinct)
                context[2] = total + 1
            if char is None:
                row = self._row(p1)
                if row is not None:
                    value = target(row.total)
                    slot = bisect_right(row.cumulative, value) - 1
                    consume(row.cumulative[slot], row.cumulative[slot + 1], row.total)
                    if slot < len(row.index):
                        char = self.bigram[p1][0][slot]
            if char is None:
                value = target(order0.total)
                slot = bisect_right(order0.cumulative, value) - 1
                consume(order0.cumulative[slot], order0.cumulative[slot + 1], order0.total)
                if slot < len(self.alphabet):
                    char = self.alphabet[slot]
                else:
                    code = target(_CODE_POINTS)
                    consume(code, code + 1, _CODE_POINTS)
                    char = chr(code)
            if context is not None:
                if char not in context[0]:
                    context[0][char] = len(context[1])
                    context[1].append(1)
                    context[3].append(char)
            else:
                contexts[key] = [{char: 0}, [1], 1, [char]]
            out.append(char)
            p2, p1 = p1, char
        text = "".join(out)
        return text.replace("\n", "\r\n") if flags & _TEXT_CRLF else text


def compress(
    payload: bytes,
    text_model: Optional[TextModel] = None,
    progress: Optional[ProgressFn] = None,
) -> tuple[int, bytes]:
    """Return ``(method, data)`` for the smallest available encoding of ``payload``."""
    text = None
    if text_model is not None and payload:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            pass
    # The pure-Python TEXT pass dominates the run time, so it gets most of the bar.
    general_end = 0.25 if text is not None else 1.0
    candidates: list[tuple[int, bytes]] = [(STORE, payload)]
    if payload:
        report(progress, 0.0, "压缩（Deflate）")
        deflater = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
        candidates.append((DEFLATE, deflater.compress(payload) + deflater.flush()))
        report(progress, general_end * 0.2, "压缩（BZip2）")
        candidates.append((BZ2, bz2.compress(payload, 9)))
        report(progress, general_end * 0.45, "压缩（LZMA）")
        candidates.append((LZMA, lzma.compress(payload, format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)))
    if text_model is not None and text is not None:
        best = min(len(data) for _, data in candidates)
        data = text_model.compress(text, limit=best, progress=stage(progress, general_end, 1.0))
        if data is not None:
            candidates.append((TEXT, data))
    method, data = min(candidates, key=lambda item: (len(item[1]), item[0]))
    report(progress, 1.0, "压缩完成")
    return method, data


def decompress(
    method: int,
    data: bytes,
    text_model: Optional[TextModel] = None,
    progress: Optional[ProgressFn] = None,
) -> bytes:
    report(progress, 0.0, "解压")
    try:
        if method == STORE:
            result = data
        elif method == DEFLATE:
            result = zlib.decompress(data, -15)
        elif method == BZ2:
            result = bz2.decompress(data)
        elif method == LZMA:
            result = lzma.decompress(data, format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)
        elif method == TEXT:
            if text_model is None:
                raise ValueError("this payload needs the model's text statistics")
            result = text_model.decompress(data, progress).encode("utf-8")
        else:
            raise ValueError(f"unknown compression method {method}")
    except (zlib.error, OSError, lzma.LZMAError, EOFError) as exc:
        raise ValueError(f"payload decompression failed: {exc}") from exc
    report(progress, 1.0, "解压完成")
    return result
