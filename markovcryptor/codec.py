from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import random
import secrets
from typing import Iterable, Optional

from .core import crypto
from .core.arith import ArithmeticDecoder, ArithmeticEncoder, BitReader
from .compression import METHOD_NAMES, compress, decompress, read_varint, write_varint
from .model import MarkovModel
from .core.progress import REPORT_EVERY, ProgressFn, report, stage
from .punctuation import extract_hanzi

PROTOCOL_VERSION = 5
NONCE_BYTES = 3
CHECK_BYTES = 4
DIGEST_BYTES = 4
SALT_BYTES = crypto.SALT_BYTES
TAG_BYTES = crypto.TAG_BYTES
FLAG_AES_GCM = 0x01
METHOD_SHIFT = 1
METHOD_MASK = 0x0E
PBKDF2_ITERATIONS = crypto.PBKDF2_ITERATIONS
_PERSON = b"mkc%d" % PROTOCOL_VERSION
# A header is at most flags + 10-byte varint + check; identification never reads further.
_MAX_HEADER = NONCE_BYTES + 1 + 10 + CHECK_BYTES


def _xor(data: bytes, mask: bytes) -> bytes:
    return (int.from_bytes(data, "big") ^ int.from_bytes(mask[: len(data)], "big")).to_bytes(len(data), "big")


@dataclass(frozen=True)
class CodecStats:
    payload_bytes: int
    payload_characters: int | None  # characters when the payload is UTF-8 text
    compressed_bytes: int
    method: str
    stream_bytes: int
    hanzi_characters: int
    cover_characters: int

    @property
    def bits_per_hanzi(self) -> float:
        """Hidden bits carried per hanzi (the frame included)."""
        return self.stream_bytes * 8 / max(self.hanzi_characters, 1)

    @property
    def expansion_ratio(self) -> float:
        """Visible cover characters per input character (per byte for binary input)."""
        return self.cover_characters / max(self.payload_characters or self.payload_bytes, 1)


class MarkovStegoCodec:
    """Arithmetic-coding steganography over the model's hanzi chain.

    Pipeline: payload -> compression (corpus text model / LZMA / ...) ->
    optional AES-256-GCM -> frame -> bits -> hanzi -> punctuation.

    The hanzi are produced by running an arithmetic *decoder* on the frame's
    bits with the Markov chain as its distribution, so the cover text is a
    faithful sample of the chain and every hanzi carries ``-log2 p`` bits.
    The receiver re-*encodes* the hanzi to get the bits back. Punctuation and
    whitespace never carry data, so they may be edited freely.

    Frame (whitened, ~10 bytes of overhead)::

        nonce[3] | mask(flags[1] | varint(body length) | check[4] | body)

    ``mask`` is SHAKE-256 keyed with the model fingerprint and the nonce, which
    makes the bits uniform (so the text looks like ordinary chain output) and
    makes ``check`` the implicit model identifier: a decoder holding many
    models finds the writer from the first dozen hanzi.
    """

    def __init__(self, model: MarkovModel, max_cover_characters: int = 200_000_000) -> None:
        self.model = model
        self.max_cover_characters = max_cover_characters
        self._indexes = {char: index for index, char in enumerate(model.vocabulary)}

    # ------------------------------------------------------------ framing

    def _mask(self, nonce: bytes, length: int) -> bytes:
        return hashlib.shake_256(_PERSON + self.model.fingerprint + nonce).digest(length)

    def _check(self, nonce: bytes, header: bytes) -> bytes:
        return hashlib.blake2s(
            nonce + header, digest_size=CHECK_BYTES, key=self.model.fingerprint, person=_PERSON
        ).digest()

    def _digest(self, payload: bytes) -> bytes:
        return hashlib.blake2s(payload, digest_size=DIGEST_BYTES, key=self.model.fingerprint).digest()

    def _frame(self, flags: int, body: bytes) -> bytes:
        nonce = secrets.token_bytes(NONCE_BYTES)
        header = bytes([flags]) + write_varint(len(body))
        plain = header + self._check(nonce, header) + body
        return nonce + _xor(plain, self._mask(nonce, len(plain)))

    def _parse_header(self, data: bytes) -> tuple[int, int, int] | None:
        """Return ``(flags, body_length, body_offset)``; None if more bytes are needed.

        Raises ValueError when the bytes cannot be a frame from this model.
        """
        if len(data) < NONCE_BYTES + 1:
            return None
        nonce = data[:NONCE_BYTES]
        available = min(len(data), _MAX_HEADER) - NONCE_BYTES
        plain = _xor(data[NONCE_BYTES : NONCE_BYTES + available], self._mask(nonce, available))
        flags = plain[0]
        parsed = read_varint(plain, 1)
        if parsed is None:
            return None
        body_length, end = parsed
        if len(plain) < end + CHECK_BYTES:
            return None
        if plain[end : end + CHECK_BYTES] != self._check(nonce, plain[:end]):
            raise ValueError("hidden frame does not match this model")
        if flags & ~(FLAG_AES_GCM | METHOD_MASK):
            raise ValueError("unsupported frame flags")
        return flags, body_length, NONCE_BYTES + end + CHECK_BYTES

    # ---------------------------------------------------------- bits <-> hanzi

    def _bits_to_hanzi(self, stream: bytes, progress: Optional[ProgressFn] = None) -> str:
        decoder = ArithmeticDecoder(BitReader(stream, random_padding=True))
        target, consume = decoder.target, decoder.consume
        table, vocabulary = self.model.table, self.model.vocabulary
        needed = len(stream) * 8
        output: list[str] = []
        previous = "<BOS>"
        while decoder.settled_bits < needed:
            cumulative, total = table(previous)
            index = bisect_right(cumulative, target(total)) - 1
            consume(cumulative[index], cumulative[index + 1], total)
            previous = vocabulary[index]
            output.append(previous)
            if len(output) % REPORT_EVERY == 0:
                if len(output) > self.max_cover_characters:
                    raise ValueError("cover text exceeds configured limit")
                report(progress, decoder.settled_bits / needed, "生成汉字")
        return "".join(output)

    def _hanzi_to_bits(
        self, hanzi: str, *, header_only: bool = False, progress: Optional[ProgressFn] = None
    ) -> tuple[bytes, tuple[int, int, int]]:
        """Recover the frame bytes (or only enough for the header) from bare hanzi."""
        encoder = ArithmeticEncoder()
        encode, writer = encoder.encode, encoder.writer
        table, indexes = self.model.table, self._indexes
        header: tuple[int, int, int] | None = None
        needed = _MAX_HEADER * 8
        previous = "<BOS>"
        for consumed, char in enumerate(hanzi, 1):
            index = indexes.get(char)
            if index is None:
                raise ValueError(f"character {char!r} is outside the selected model")
            cumulative, total = table(previous)
            encode(cumulative[index], cumulative[index + 1], total)
            previous = char
            if header is None:
                if writer.count >= 8 * (NONCE_BYTES + 1):
                    header = self._parse_header(writer.complete_bytes())
                    if header is not None:
                        if header_only:
                            return b"", header
                        needed = 8 * (header[2] + header[1])
                        # Each hanzi carries at most ~30 bits; reject absurd lengths early.
                        if needed > 32 * len(hanzi):
                            raise ValueError("cover text is truncated")
                elif consumed > 4 * _MAX_HEADER * 8:
                    break
            elif consumed % REPORT_EVERY == 0:
                report(progress, writer.count / needed, "还原比特")
            if header is not None and writer.count >= needed:
                return writer.complete_bytes()[: needed // 8], header
        raise ValueError("cover text is too short or truncated")

    # ------------------------------------------------------------ public API

    def encode_hanzi_with_stats(
        self,
        payload: bytes,
        password: str | bytes | None = None,
        progress: Optional[ProgressFn] = None,
    ) -> tuple[str, CodecStats]:
        """Encode to the bare hanzi carrier (no punctuation) and report sizes."""
        method, data = compress(payload, self.model.text, stage(progress, 0.0, 0.35))
        flags = method << METHOD_SHIFT
        if password is not None:
            flags |= FLAG_AES_GCM
            body = crypto.encrypt_with_password(data, password, bytes([PROTOCOL_VERSION, flags]))
        else:
            body = data + self._digest(payload)
        stream = self._frame(flags, body)
        hanzi = self._bits_to_hanzi(stream, stage(progress, 0.35, 1.0))
        try:
            characters: int | None = len(payload.decode("utf-8"))
        except UnicodeDecodeError:
            characters = None
        stats = CodecStats(len(payload), characters, len(data), METHOD_NAMES[method], len(stream), len(hanzi), len(hanzi))
        return hanzi, stats

    def encode_hanzi(self, payload: bytes, password: str | bytes | None = None) -> str:
        return self.encode_hanzi_with_stats(payload, password)[0]

    def encode_with_stats(
        self,
        payload: bytes,
        password: str | bytes | None = None,
        *,
        punctuate: bool = True,
        rng: random.Random | None = None,
        progress: Optional[ProgressFn] = None,
    ) -> tuple[str, CodecStats]:
        hanzi, stats = self.encode_hanzi_with_stats(payload, password, stage(progress, 0.0, 0.85 if punctuate else 1.0))
        cover = self.model.render(hanzi, rng, stage(progress, 0.85, 1.0)) if punctuate else hanzi
        report(progress, 1.0, "完成")
        visible = sum(1 for char in cover if not char.isspace()) if punctuate else len(cover)
        return cover, CodecStats(**{**stats.__dict__, "cover_characters": visible})

    def encode(
        self,
        payload: bytes,
        password: str | bytes | None = None,
        *,
        punctuate: bool = True,
        rng: random.Random | None = None,
        progress: Optional[ProgressFn] = None,
    ) -> str:
        return self.encode_with_stats(payload, password, punctuate=punctuate, rng=rng, progress=progress)[0]

    def identifies(self, covertext: str) -> bool:
        """Whether this codec's model wrote ``covertext``, judged from the frame alone."""
        return self._identifies_hanzi(extract_hanzi(covertext))

    def _identifies_hanzi(self, hanzi: str) -> bool:
        try:
            self._hanzi_to_bits(hanzi[: 4 * _MAX_HEADER * 8], header_only=True)
        except ValueError:
            return False
        return True

    def decode(
        self,
        covertext: str,
        password: str | bytes | None = None,
        progress: Optional[ProgressFn] = None,
    ) -> bytes:
        return self._decode_hanzi(extract_hanzi(covertext), password, progress)

    def _decode_hanzi(
        self, hanzi: str, password: str | bytes | None, progress: Optional[ProgressFn] = None
    ) -> bytes:
        stream, (flags, body_length, offset) = self._hanzi_to_bits(hanzi, progress=stage(progress, 0.0, 0.6))
        nonce, masked = stream[:NONCE_BYTES], stream[NONCE_BYTES:]
        plain = _xor(masked, self._mask(nonce, len(masked)))
        body = plain[offset - NONCE_BYTES : offset - NONCE_BYTES + body_length]
        method = (flags & METHOD_MASK) >> METHOD_SHIFT
        if flags & FLAG_AES_GCM:
            if password is None:
                raise ValueError("this text requires an AES password")
            data = crypto.decrypt_with_password(body, password, bytes([PROTOCOL_VERSION, flags]))
        else:
            if len(body) < DIGEST_BYTES:
                raise ValueError("hidden body is truncated")
            data = body[:-DIGEST_BYTES]
        payload = decompress(method, data, self.model.text, stage(progress, 0.6, 1.0))
        if not flags & FLAG_AES_GCM and self._digest(payload) != body[-DIGEST_BYTES:]:
            raise ValueError("body verification failed")
        report(progress, 1.0, "完成")
        return payload


def identify_model(covertext: str, models: Iterable[MarkovModel]) -> MarkovModel | None:
    """Return the model whose keyed frame check verifies, without decoding the body."""
    hanzi = extract_hanzi(covertext)
    return next((model for model in models if MarkovStegoCodec(model)._identifies_hanzi(hanzi)), None)


def decode_with_models(
    covertext: str,
    models: Iterable[MarkovModel],
    password: str | bytes | None = None,
    progress: Optional[ProgressFn] = None,
) -> tuple[bytes, MarkovModel]:
    """Identify the writing model from the frame, then decode with it."""
    hanzi = extract_hanzi(covertext)
    model = next((model for model in models if MarkovStegoCodec(model)._identifies_hanzi(hanzi)), None)
    if model is None:
        raise ValueError("no supplied model matches this cover text")
    try:
        return MarkovStegoCodec(model)._decode_hanzi(hanzi, password, progress), model
    except ValueError as exc:
        raise ValueError(f"identified model {model.name or model.model_id}, but decoding failed: {exc}") from exc
