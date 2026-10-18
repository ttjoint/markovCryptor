"""Error hierarchy shared by every channel and interface.

Design goal: the *caller* should be able to decide what to do without parsing
message text, and the transport layer should be able to map an error to an HTTP
status without a lookup table maintained in parallel.

Migration note (phase 1): this module is introduced without changing behaviour.
Existing code still raises ``ValueError`` / ``StegoError`` / ``GifStegoError``;
the classes below are the target vocabulary and are adopted as each layer is
moved.  ``GifStegoError`` and ``StegoError`` already subclass the right node so
``except StegoError`` keeps working during the transition.
"""

from __future__ import annotations


class StegoError(Exception):
    """Root of everything this project raises on purpose.

    ``code`` is stable and meant for machines; ``http_status`` is the default
    mapping used by the future Web API (docs/ARCHITECTURE.md §4.2).
    """

    code: str = "internal"
    http_status: int = 500

    def __init__(self, message: str, *,
                 reasons: tuple[str, ...] = (), hints: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        #: True causes, verbatim.  Existing code is careful to keep these
        #: specific ("GIF 太小：155×60 只能容纳 28 字节，需要 32 字节…"), so
        #: they must never be collapsed into a generic phrase.
        self.reasons = reasons
        #: Actionable next steps for a human ("换一张更大的动图").
        self.hints = hints


class InvalidInput(StegoError, ValueError):
    """Caller passed something malformed (bad options, empty key, bad file).

    Also a ``ValueError``: long-standing callers (the CLI, the Chinese codec)
    catch that, and a bad argument genuinely *is* a value error.  Keeping both
    bases lets old ``except ValueError`` blocks keep working while new code can
    discriminate on the class.
    """

    code = "invalid_input"
    http_status = 422


class UnsupportedCarrier(StegoError):
    """Carrier type this channel cannot accept at all (wrong format/kind)."""

    code = "unsupported_carrier"
    http_status = 415


class CapacityExceeded(StegoError):
    """Carrier is understood, just too small for the payload."""

    code = "capacity_exceeded"
    http_status = 413


class BadKey(StegoError, ValueError):
    """Key material is wrong, or the password does not authenticate.

    A ``ValueError`` for the same compatibility reason as :class:`InvalidInput`.
    """

    code = "bad_key"
    http_status = 401


class NoHiddenFrame(StegoError):
    """Carrier is valid but carries nothing we can read as a frame."""

    code = "no_hidden_frame"
    http_status = 404


class CarrierDamaged(StegoError):
    """A frame was found but does not survive integrity checks (cropped,
    recompressed past recovery, truncated)."""

    code = "carrier_damaged"
    http_status = 422


class ChannelUnavailable(StegoError):
    """Channel exists but a runtime dependency is missing (e.g. no ffmpeg)."""

    code = "channel_unavailable"
    http_status = 503


# NOTE: there is deliberately no ``Cancelled`` here.  ``progress.Cancelled``
# is already raised from progress callbacks across eight modules, and having two
# classes with that name would make ``except Cancelled`` depend on the import.
# See ``progress.Cancelled`` for the single source of truth.


__all__ = [
    "StegoError",
    "InvalidInput",
    "UnsupportedCarrier",
    "CapacityExceeded",
    "BadKey",
    "NoHiddenFrame",
    "CarrierDamaged",
    "ChannelUnavailable",
]
