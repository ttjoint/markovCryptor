"""Finite-precision binary arithmetic coding (Witten-Neal-Cleary with batched renormalisation).

The same interval arithmetic serves two purposes:

* compression: :class:`ArithmeticEncoder` turns symbols into bits and
  :class:`ArithmeticDecoder` turns them back;
* steganography: the *decoder* is driven by payload bits and its chosen symbols
  become the cover text; the receiver runs the *encoder* over that text and the
  bits it emits are exactly the payload bits (every emitted bit is a bit that
  the decoder's code window had already settled).

Both directions update ``low``/``high`` identically from the symbol frequencies,
so ``settled_bits`` on the decoder side equals ``bits_emitted`` on the encoder
side after the same symbols. Frequency totals must not exceed :data:`MAX_TOTAL`.
"""

from __future__ import annotations

import secrets

PRECISION = 32
MASK = (1 << PRECISION) - 1
HALF = 1 << (PRECISION - 1)
QUARTER = 1 << (PRECISION - 2)
LOW_MASK = HALF - 1
MAX_TOTAL = QUARTER


def _renormalise(low: int, high: int) -> tuple[int, int, int, int, int]:
    """Shift out settled leading bits, then underflow (E3) bits.

    Returns ``(low, high, n, top, m)``: ``n`` settled bits whose value is ``top``,
    followed by ``m`` underflow steps. After E1/E2 the top bits of low/high
    differ, and E3 preserves that, so one pass of each is enough.
    """
    n = PRECISION - (low ^ high).bit_length()
    top = 0
    if n:
        top = low >> (PRECISION - n)
        low = (low << n) & MASK
        high = ((high << n) & MASK) | ((1 << n) - 1)
    # E3 applies while low = 01... and high = 10...; count the common run.
    ones = (PRECISION - 1) - (~low & LOW_MASK).bit_length()
    zeros = (PRECISION - 1) - (high & LOW_MASK).bit_length()
    m = min(ones, zeros)
    if m:
        shift = ((1 << m) - 1) * HALF
        low = (low << m) - shift
        high = (high << m) - shift + ((1 << m) - 1)
    return low, high, n, top, m


class BitWriter:
    def __init__(self) -> None:
        self.out = bytearray()
        self._acc = 0
        self._bits = 0
        self.count = 0

    def write(self, value: int, n: int) -> None:
        if n <= 0:
            return
        self._acc = (self._acc << n) | value
        self._bits += n
        self.count += n
        if self._bits >= 64:
            whole = self._bits >> 3
            rest = self._bits - (whole << 3)
            self.out += (self._acc >> rest).to_bytes(whole, "big")
            self._acc &= (1 << rest) - 1
            self._bits = rest

    def write_run(self, bit: int, n: int) -> None:
        while n > 0:
            step = min(n, 4096)
            self.write(((1 << step) - 1) if bit else 0, step)
            n -= step

    def getvalue(self) -> bytes:
        """All complete bytes plus the trailing partial byte padded with zeros."""
        tail = b""
        if self._bits:
            pad = (-self._bits) % 8
            tail = (self._acc << pad).to_bytes((self._bits + pad) >> 3, "big")
        return bytes(self.out) + tail

    def complete_bytes(self) -> bytes:
        """Only fully written bytes (used while the stream is still growing)."""
        return bytes(self.out) + (self._acc >> (self._bits & 7)).to_bytes(self._bits >> 3, "big")


class BitReader:
    """Reads big-endian bits; past the end it yields zeros or random padding."""

    def __init__(self, data: bytes, *, random_padding: bool = False) -> None:
        self._data = bytearray(data)
        self._limit = len(data) * 8
        self._random = random_padding
        self.position = 0

    def read(self, n: int) -> int:
        if n <= 0:
            return 0
        start = self.position
        end = start + n
        self.position = end
        need = (end + 7) >> 3
        if need > len(self._data):
            extra = need - len(self._data) + 64
            self._data += secrets.token_bytes(extra) if self._random else bytes(extra)
        first = start >> 3
        chunk = int.from_bytes(self._data[first:need], "big")
        return (chunk >> ((need << 3) - end)) & ((1 << n) - 1)


class ArithmeticEncoder:
    """Symbols -> bits. ``bits_emitted`` counts only bits that are final."""

    def __init__(self) -> None:
        self.low = 0
        self.high = MASK
        self.pending = 0
        self.writer = BitWriter()

    @property
    def bits_emitted(self) -> int:
        return self.writer.count

    def encode(self, cum_low: int, cum_high: int, total: int) -> None:
        span = self.high - self.low + 1
        self.high = self.low + span * cum_high // total - 1
        self.low = self.low + span * cum_low // total
        self.low, self.high, n, top, m = _renormalise(self.low, self.high)
        if n:
            first = top >> (n - 1)
            self.writer.write(first, 1)
            if self.pending:
                self.writer.write_run(first ^ 1, self.pending)
                self.pending = 0
            self.writer.write(top & ((1 << (n - 1)) - 1), n - 1)
        self.pending += m

    def finish(self) -> bytes:
        """Flush enough bits that a zero-padded decoder lands inside the final interval."""
        self.pending += 1
        bit = 0 if self.low < QUARTER else 1
        self.writer.write(bit, 1)
        self.writer.write_run(bit ^ 1, self.pending)
        self.pending = 0
        return self.writer.getvalue()


class ArithmeticDecoder:
    """Bits -> symbols. ``settled_bits`` counts input bits fixed by the symbols so far."""

    def __init__(self, reader: BitReader) -> None:
        self.reader = reader
        self.low = 0
        self.high = MASK
        self.value = reader.read(PRECISION)
        self.shifted = 0
        self.pending = 0

    @property
    def settled_bits(self) -> int:
        return self.shifted - self.pending

    def target(self, total: int) -> int:
        span = self.high - self.low + 1
        return ((self.value - self.low + 1) * total - 1) // span

    def consume(self, cum_low: int, cum_high: int, total: int) -> None:
        span = self.high - self.low + 1
        self.high = self.low + span * cum_high // total - 1
        self.low = self.low + span * cum_low // total
        self.low, self.high, n, _, m = _renormalise(self.low, self.high)
        if n:
            self.value = ((self.value << n) & MASK) | self.reader.read(n)
            self.pending = 0
        if m:
            self.value = (self.value << m) - ((1 << m) - 1) * HALF + self.reader.read(m)
            self.pending += m
        self.shifted += n + m
