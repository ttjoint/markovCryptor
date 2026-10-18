"""Key material: normalisation, domain separation, deterministic RNG.

These three functions were the bottom of ``image_stego``'s dependency graph and
are used by every channel, so they live in ``core`` rather than in any one of
them.  They are pure: same inputs, same outputs, no IO, no globals.

The ``mkx1-`` prefixes are part of the on-the-wire format.  Changing either
label breaks extraction of every carrier written by an earlier version, so they
are frozen even though the name no longer reflects the whole platform.
"""

from __future__ import annotations

import hashlib

import numpy as np

KEY_DOMAIN = b"mkx1-key\x00"
DERIVE_DOMAIN = b"mkx1-dom\x00"


def normalise_key(key: str | bytes) -> bytes:
    """Accept a passphrase or raw bytes and return a fixed 32-byte key.

    Hashing rather than padding means any-length human input works while every
    channel sees the same length.
    """
    raw = key.encode("utf-8") if isinstance(key, str) else key
    if not raw:
        raise ValueError("共享密钥不能为空")
    return hashlib.sha256(KEY_DOMAIN + raw).digest()


def derive(key: bytes, label: str, length: int = 32) -> bytes:
    """Derive an independent sub-key so one secret cannot leak across uses.

    Used to separate block ordering, nonces and whitening masks derived from the
    same master key.
    """
    return hashlib.shake_256(DERIVE_DOMAIN + label.encode() + b"\x00" + key).digest(length)


def rng(*parts: bytes) -> np.random.Generator:
    """Deterministic NumPy generator seeded from a hash of ``parts``.

    Deterministic is the point: the receiver must reproduce the same block
    permutation from the same key, without transmitting it.
    """
    seed = int.from_bytes(hashlib.sha256(b"".join(parts)).digest(), "big")
    return np.random.Generator(np.random.PCG64(seed))


__all__ = ["normalise_key", "derive", "rng", "KEY_DOMAIN", "DERIVE_DOMAIN"]
