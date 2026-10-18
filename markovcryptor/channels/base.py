"""The value objects every channel speaks in.

Design rule (docs/ARCHITECTURE.md §3.3): these are **plain data**. No file IO,
no PIL, no model loading. A :class:`Carrier` holds a *thunk* that loads the
actual bytes only when a channel asks for it, so probing a 500-entry batch does
not decode 500 images first.

Everything here is frozen: a ``Feasibility`` or ``Envelope`` is a report of what
happened, and a report that can be mutated after the fact is a bug waiting to
happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..core.errors import InvalidInput


class CarrierKind(str, Enum):
    """What *shape* of thing a carrier is.

    Deliberately coarse: it drives UI affordances ("pick a file" vs "paste
    text") and pre-flight checks, not algorithm choice. Adding ``VIDEO``/
    ``STREAM`` later is a matter of adding a member.
    """

    TEXT = "text"
    IMAGE = "image"
    ANIMATION = "animation"

    @classmethod
    def of(cls, value: "CarrierKind | str") -> "CarrierKind":
        """Accept the member or its value, rejecting anything else here.

        Without this a stray ``kind="image"`` travels all the way into the first
        channel and fails as ``'str' object has no attribute 'value'`` -- an
        error that says nothing about what the caller did wrong. Failing at the
        constructor names the argument and lists the legal values.
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            legal = ", ".join(member.value for member in cls)
            raise ValueError(
                f"kind 必须是 {legal} 之一，收到 {value!r}"
            ) from None


@dataclass(frozen=True)
class Carrier:
    """A carrier, referenced lazily.

    ``load`` is called at most once per channel operation. Channels are allowed
    to call it zero times (e.g. ``probe`` on an obviously wrong ``kind``), which
    is what makes batch pre-flight cheap.
    """

    kind: CarrierKind
    load: Callable[[], Any]
    #: Rough size in bytes, when the caller knows it without loading. Used only
    #: for capacity *estimates* and cost reporting -- never for correctness.
    size_hint: int | None = None
    media_type: str | None = None
    #: Human-facing label, e.g. the original file name. Purely for reports.
    label: str = ""

    def __post_init__(self) -> None:
        # Normalise here so every channel can trust ``kind`` to be a member.
        if not isinstance(self.kind, CarrierKind):
            object.__setattr__(self, "kind", CarrierKind.of(self.kind))

    @classmethod
    def of_bytes(cls, data: bytes, *, kind: CarrierKind | str,
                 media_type: str | None = None, label: str = "") -> "Carrier":
        return cls(kind=CarrierKind.of(kind), load=lambda: data, size_hint=len(data),
                   media_type=media_type, label=label)

    @classmethod
    def of_text(cls, text: str, *, label: str = "") -> "Carrier":
        return cls(kind=CarrierKind.TEXT, load=lambda: text,
                   size_hint=len(text.encode("utf-8")), media_type="text/plain",
                   label=label)

    def __repr__(self) -> str:  # keep the thunk out of tracebacks and logs
        return (f"Carrier(kind={self.kind.value!r}, size_hint={self.size_hint!r}, "
                f"media_type={self.media_type!r}, label={self.label!r})")


@dataclass(frozen=True)
class Feasibility:
    """Can this carrier hold this payload, and what will it cost?

    ``reasons`` carries the **verbatim** underlying cause. This project has a
    standing rule that a failed fit must never be reported as a generic
    "cannot carry data" -- the caller needs "155×60 only fits 28 of 32 bytes"
    to decide what to do.
    """

    ok: bool
    capacity_bytes: int
    required_bytes: int
    reasons: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    #: Expected cost, for UI warnings: ``{"upscale": 1.4, "growth": 2.1}``.
    cost: dict[str, float] = field(default_factory=dict)

    @property
    def utilization(self) -> float:
        """Fraction of capacity the payload would occupy (0..1)."""
        if self.capacity_bytes <= 0:
            return 1.0 if self.required_bytes > 0 else 0.0
        return self.required_bytes / self.capacity_bytes

    def __str__(self) -> str:
        if self.ok:
            return (f"可承载 {self.required_bytes}/{self.capacity_bytes} 字节"
                    f"（{self.utilization:.0%}）")
        return "；".join(self.reasons) or "不可承载"


@dataclass(frozen=True)
class EmbedReport:
    """What actually happened during an embed."""

    channel: str
    payload_bytes: int
    carrier_bytes: int | None = None
    capacity_bytes: int = 0
    #: Options that actually took effect -- e.g. the step after auto-escalation,
    #: not the one requested. Reading this back is how a caller learns that the
    #: algorithm had to widen the lattice.
    options_used: dict[str, Any] = field(default_factory=dict)
    #: Distortion metrics, e.g. ``{"max_block_change": 12.0}``.
    distortion: dict[str, float] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def utilization(self) -> float:
        if self.capacity_bytes <= 0:
            return 0.0
        return min(1.0, self.payload_bytes / self.capacity_bytes)


@dataclass(frozen=True)
class Envelope:
    """An embed result: one primary output plus optional alternates.

    ``primary`` is what the channel recommends sending. ``alternatives`` exists
    because carrier formats trade size against robustness (PNG is lossless and
    is the default for chat; a JPEG variant may be offered for thumbnails), and
    a caller should not have to re-run the embed to get it.
    """

    primary: bytes | str
    report: EmbedReport
    alternatives: dict[str, bytes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.primary, (bytes, str)) or not self.primary:
            raise InvalidInput("Envelope.primary 必须是非空 bytes 或 str")

    def as_carrier(self, *, media_type: str | None = None) -> Carrier:
        """Wrap our own output so it can be fed straight back into ``extract``."""
        if isinstance(self.primary, str):
            return Carrier.of_text(self.primary)
        return Carrier.of_bytes(self.primary, kind=CarrierKind.IMAGE,
                                media_type=media_type or "application/octet-stream")


__all__ = [
    "CarrierKind", "Carrier", "Feasibility", "EmbedReport", "Envelope",
]
