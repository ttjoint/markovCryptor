"""Domain core: pure algorithms with no IO and no interface knowledge.

Everything here can be imported by any channel and by any interface, and nothing
here imports ``channels`` or ``interfaces``.  Kept deliberately free of file
paths, HTTP and Tk so the same code runs inside a test, a CLI call and a worker.

Modules
-------
``arith``     arithmetic coder (bit-exact, used by the Chinese channel's compression)
``progress``  progress callback type and the ``Cancelled`` escape hatch
``errors``    the exception hierarchy every layer raises
``keys``      key normalisation, domain separation, deterministic RNG
``crypto``    AES-GCM, PBKDF2, X25519 handshake and the HKDF session chain
``framing``   MKX1 frame layout and the two lead profiles
``ecc``       Reed-Solomon encode/decode over that frame
"""

from __future__ import annotations

__all__ = ["arith", "progress", "errors", "keys", "crypto", "framing", "ecc"]
