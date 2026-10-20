"""Bundled cover photos for the QQ transport.

Why ship pictures at all: an automatic handshake reply must travel in a picture
the replying side owns (``Terminal._auto_respond``), and on QQ there is no
browser picture queue to take one from. Without a built-in cover, a peer's
handshake request would sit in ``pending_request`` forever with nothing on screen
to explain why nothing happened.

Why *these* pictures: QQ runs risk control on what an account sends, and a
synthetic gradient-noise image is exactly the shape a filter flags -- it looks
like something a program produced, because it is. A picture that a person would
plausibly send does not. The two files here were made from the project's own
test photos (``data/``, resized to 512px and re-encoded as JPEG), so they look
like ordinary chat pictures while still carrying enough capacity for a handshake
or a short covert message.

Capacity, measured against the real QIM planner:

===========  ==========  ==========================
file         size        ``max_payload_bytes``
===========  ==========  ==========================
qq-cover-1   512x512     1863
qq-cover-2   512x317     1128
===========  ==========  ==========================

A handshake is 38 bytes, so either is ample. If a caller needs to hide a long
message it should pass its own photos via ``--cover`` rather than rely on these.
"""

from __future__ import annotations

from pathlib import Path

COVER_DIR = Path(__file__).resolve().parent / "covers"

#: Filenames in :data:`COVER_DIR`, in a fixed order so runs are reproducible.
COVER_FILES = ("qq-cover-1.jpg", "qq-cover-2.jpg")


def paths() -> list[Path]:
    """Existing cover files, in order. Missing entries are skipped, not fatal.

    A wheel built without package-data would otherwise crash at import time on a
    machine that only needed the local two-window mode.
    """
    return [COVER_DIR / name for name in COVER_FILES if (COVER_DIR / name).is_file()]


def load() -> list[bytes]:
    """The covers as bytes, PNG-free (they are sent as-is; stego re-encodes)."""
    out: list[bytes] = []
    for path in paths():
        try:
            out.append(path.read_bytes())
        except OSError:
            continue
    return out


def load_or_none(count: int = 2) -> list[bytes]:
    """At most ``count`` covers; empty list when none are available."""
    return load()[:count]


__all__ = ["COVER_DIR", "COVER_FILES", "load", "load_or_none", "paths"]
