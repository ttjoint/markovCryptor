"""Adapter: the Chinese-text channel (Markov chain + arithmetic coding).

This channel is genuinely different from the two image ones, and the adapter's
job is to *say so* rather than paper over it:

* **It generates the carrier instead of modifying one.** There is no input
  cover; the arithmetic decoder *samples* the Markov chain, and the sampling
  decisions are the hidden bits. So ``probe`` cannot measure an existing
  carrier -- it reports what the *model* can carry.
* **Capacity is a property of the model, not the medium.** A richer corpus means
  more bits per hanzi. ``capacity_bytes`` here is therefore an *estimate* for a
  reference cover length, and the failure mode is "text too short to hold the
  payload", not "image too small".
* **A trained model is required.** The model is a construction parameter, not an
  option, because swapping it changes the crypto frame (the fingerprint keys the
  whitening mask). ``Engine`` owns model caching for exactly this reason.

Passing a text carrier to ``embed`` is still meaningful: it is used as a *seed*
for punctuation placement and to size the output, so callers who already have
text do not lose it.
"""

from __future__ import annotations

from typing import Any

from ..codec import MarkovStegoCodec
from ..core.errors import BadKey, InvalidInput, NoHiddenFrame
from .base import Carrier, CarrierKind, EmbedReport, Envelope, Feasibility
from .options import ChineseOptions

#: Reference cover length used to express this channel's capacity in bytes.
#: 4 000 hanzi is a comfortable chat-message length; the number is a yardstick,
#: not a limit, because the encoder simply emits as many hanzi as it needs.
REFERENCE_COVER_HANZI = 4_000


class ChineseChannel:
    id = "chinese"
    display_name = "中文伪装文本（马尔可夫+算术编码）"
    summary = "把数据编码成一段像自然中文的文章；无需底图，容量取决于已训练的模型"
    mime_in = ("text/plain",)
    mime_out = "text/plain"
    options_schema = ChineseOptions

    def __init__(self, codec: MarkovStegoCodec) -> None:
        self._codec = codec

    @property
    def codec(self) -> MarkovStegoCodec:
        """The trained codec this channel wraps (shared, cheap to read)."""
        return self._codec

    @staticmethod
    def _options(options: Any) -> ChineseOptions:
        return options or ChineseOptions()

    @staticmethod
    def _bits_per_hanzi(codec: MarkovStegoCodec) -> float:
        """Bits each hanzi carries, measured once from a real encode.

        Measuring beats guessing: the value depends on the model's entropy, and
        a small corpus can carry as little as 2 bits/hanzi. The probe is cheap
        because the payload is one byte.
        """
        try:
            stats = codec.encode_hanzi_with_stats(b"\x00")[1]
        except Exception:                       # pragma: no cover - degenerate model
            return 4.0
        return max(stats.bits_per_hanzi, 0.5)

    # -------------------------------------------------------------------- probe

    def probe(self, cover: Carrier, payload_bytes: int) -> Feasibility:
        """Report whether the *model* can express a payload this size.

        No carrier is loaded: for this channel the carrier is output, not input.
        A supplied text carrier only raises the estimate when it is longer than
        the reference length.
        """
        if payload_bytes < 0:
            raise ValueError("payload_bytes 不能为负")

        bits_per_hanzi = self._bits_per_hanzi(self._codec)
        hanzi_budget = REFERENCE_COVER_HANZI
        if cover.kind is CarrierKind.TEXT:
            # Existing text just tells us how long the output is expected to be.
            try:
                text = cover.load()
            except Exception:
                text = ""
            if isinstance(text, str) and text.strip():
                hanzi_budget = max(len(text), REFERENCE_COVER_HANZI // 4)

        # Frame overhead is ~10 bytes plus RS-free AEAD/GCM framing; 32 bytes is
        # a safe allowance and keeps the estimate pessimistic (never optimistic).
        usable_bits = hanzi_budget * bits_per_hanzi - 32 * 8
        capacity = max(0, int(usable_bits // 8))
        ok = capacity >= payload_bytes

        reasons: tuple[str, ...] = ()
        hints: list[str] = []
        if not ok:
            reasons = (
                f"模型每字平均只承载 {bits_per_hanzi:.1f} 比特，"
                f"{hanzi_budget} 字最多容纳 {capacity} 字节，需要 {payload_bytes} 字节",
            )
            hints.append("换用更大的语料重新训练模型，或缩短载荷")

        return Feasibility(
            ok=ok, capacity_bytes=capacity, required_bytes=payload_bytes,
            reasons=reasons, hints=tuple(hints),
            cost={"bits_per_hanzi": round(bits_per_hanzi, 2)},
        )

    # -------------------------------------------------------------------- embed

    def embed(self, cover: Carrier, payload: bytes, key: bytes,
              options: Any = None, progress=None) -> Envelope:
        """Encode ``payload`` as pseudo-Chinese prose.

        ``key`` is the passphrase for the AES-GCM layer, matching the image
        channels' contract. An empty key means "encode only, do not encrypt",
        which is what ``--no-encrypt`` expresses in the CLI.
        """
        opts = self._options(options)
        passphrase: str | bytes | None = opts.password if opts.password else None
        if key and not passphrase:
            passphrase = key

        cover_text, stats = self._codec.encode_with_stats(
            payload, passphrase, punctuate=opts.add_punctuation, progress=progress
        )

        report = EmbedReport(
            channel=self.id,
            payload_bytes=len(payload),
            carrier_bytes=len(cover_text.encode("utf-8")),
            capacity_bytes=REFERENCE_COVER_HANZI * stats.bits_per_hanzi // 8,
            options_used={
                "punctuate": opts.add_punctuation,
                "encrypted": passphrase is not None,
                "method": stats.method,
            },
            distortion={
                "bits_per_hanzi": round(stats.bits_per_hanzi, 3),
                "expansion": round(stats.expansion_ratio, 2),
            },
        )
        return Envelope(primary=cover_text, report=report)

    # ------------------------------------------------------------------ extract

    def extract(self, carrier: Carrier, key: bytes,
                options: Any = None, progress=None) -> bytes:
        opts = self._options(options)
        passphrase: str | bytes | None = opts.password if opts.password else None
        if key and not passphrase:
            passphrase = key

        if carrier.kind is not CarrierKind.TEXT:
            raise InvalidInput(
                f"中文信道需要文本载体，收到 {carrier.kind.value}"
            )
        text = carrier.load()
        if not isinstance(text, str):
            raise InvalidInput(f"中文信道需要 str，收到 {type(text).__name__}")

        try:
            return self._codec.decode(text, passphrase, progress=progress)
        except BadKey:
            # A password failure is not "there is no frame here" -- the frame was
            # found and would decode with the right passphrase. Keeping the two
            # apart is what lets a caller say "wrong password" instead of
            # "this text is empty", and maps to 401 rather than 404 over HTTP.
            raise
        except ValueError as exc:
            # ``MarkovStegoCodec.decode`` raises ValueError for "not a frame" and
            # "wrong model" alike; the message distinguishes them, so pass it
            # through unchanged rather than flattening it.
            raise NoHiddenFrame(str(exc)) from exc


__all__ = ["ChineseChannel", "REFERENCE_COVER_HANZI"]
