"""The ``Channel`` protocol: one shape for every steganography algorithm.

Why a Protocol and not a base class? The three existing channels are wildly
different (a text codec with a trained model, a DCT embedder, a GIF frame
re-encoder) and they already work. Inheritance would tempt us into hoisting
shared behaviour that does not exist; a Protocol just states what a channel
*is*, and the adapters keep calling the existing, tested functions.

Design rule from docs/ARCHITECTURE.md: abstract the **semantics** (probe /
embed / extract) and nothing else. Options stay channel-specific and are
declared by ``options_schema``, so the UI can generate a form per channel
without knowing what a QIM step is.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..core.progress import ProgressFn
from .base import Carrier, Envelope, Feasibility


@runtime_checkable
class Channel(Protocol):
    """Metadata + three operations. That is the whole contract."""

    # --- metadata: everything the outside world needs to describe a channel

    #: Stable machine name used in URLs and CLI flags: ``"image"``, ``"gif"``.
    id: str
    #: Human-facing name shown in menus.
    display_name: str
    #: One line explaining the trade-off, shown as a form hint.
    summary: str
    #: Accepted input media types; empty means "any".
    mime_in: tuple[str, ...]
    #: Media type of what ``embed`` returns.
    mime_out: str
    #: Pydantic model class declaring the per-channel options. ``None`` means
    #: the channel takes no options at all.
    options_schema: type | None

    def probe(self, cover: Carrier, payload_bytes: int) -> Feasibility:
        """Can ``cover`` hold ``payload_bytes``? Never raises for a bad fit.

        Must not load the carrier when the answer is already knowable from
        metadata, and must report the true cause in ``reasons`` when ``ok`` is
        false -- never a generic "cannot carry".
        """
        ...

    def embed(self, cover: Carrier, payload: bytes, key: bytes,
              options: Any = None,
              progress: ProgressFn | None = None) -> Envelope:
        """Hide ``payload`` in ``cover`` and return the artifact to send.

        ``payload`` is raw bytes: message *encoding* (compression, arithmetic
        coding, model choice) belongs to ``Engine``, not to a channel. That
        split is what lets a new channel reuse the whole message pipeline.
        """
        ...

    def extract(self, carrier: Carrier, key: bytes,
                options: Any = None,
                progress: ProgressFn | None = None) -> bytes:
        """Recover the payload hidden in ``carrier``.

        Must work *blindly* where the channel allows it: no original carrier,
        no payload length, no side channel.
        """
        ...


def describe(channel: Channel) -> dict[str, Any]:
    """Serialise a channel for ``GET /channels`` and for the CLI's ``channels``.

    Returns JSON-safe data only, so the same payload feeds the web UI's form
    generator and a terminal listing.
    """
    schema: dict[str, Any] | None = None
    if channel.options_schema is not None:
        builder = getattr(channel.options_schema, "model_json_schema", None)
        if builder is not None:                      # Pydantic v2
            schema = builder()
    return {
        "id": channel.id,
        "display_name": channel.display_name,
        "summary": channel.summary,
        "mime_in": list(channel.mime_in),
        "mime_out": channel.mime_out,
        "options_schema": schema,
    }


__all__ = ["Channel", "describe"]
