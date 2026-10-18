"""Shared carrier access for the test suite.

Tests must run on a clean checkout with no ``data/``, ``corpus/`` or
``D:/Pictures``.  Everything comes from ``tests/fixtures/`` (versioned, see
``tools/make_fixtures.py``) or is generated here from a fixed seed.

Nothing in this module touches the network or the user's home directory.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The general-purpose photographic cover (160x160, ~162 B capacity).
PHOTO = FIXTURES / "photo.png"
#: Smallest still cover that works at all (120x120, ~74 B).
SMALL = FIXTURES / "photo-small.png"
#: Non-square, to catch code that assumes ``width == height``.
TALL = FIXTURES / "photo-tall.png"
#: JPEG-compressed source, for the "survives recompression" tests.
JPEG = FIXTURES / "photo.jpg"
#: Three-frame animation, 120x120.
GIF = FIXTURES / "anim.gif"
#: Chinese training corpus.
CORPUS = FIXTURES / "corpus.txt"


def require(path: Path) -> Path:
    """Fail loudly if a fixture is missing.

    Silently skipping is how the old ``data/``-dependent tests hid the fact
    that a clean checkout could not run them at all.
    """
    if not path.exists():
        raise AssertionError(
            f"缺少测试夹具 {path.name}。运行 `python -m tools.make_fixtures` 重新生成；"
            "夹具本应随仓库分发，缺失说明检出损坏。"
        )
    return path


@lru_cache(maxsize=None)
def _load(path: Path, convert: str = "RGB") -> Image.Image:
    """Decode fully and release the file handle (see ``animation_image``)."""
    with Image.open(require(path)) as image:
        image.load()
        return image.convert(convert)


@lru_cache(maxsize=None)
def photo(size: tuple[int, int] | None = None) -> Image.Image:
    """The photo cover, optionally resized.  Cached: decoding dominates runtime."""
    image = _load(PHOTO).copy()
    return image if size is None else image.resize(size)


@lru_cache(maxsize=None)
def small() -> Image.Image:
    return _load(SMALL).copy()


@lru_cache(maxsize=None)
def tall() -> Image.Image:
    return _load(TALL).copy()


@lru_cache(maxsize=None)
def jpeg_source() -> Image.Image:
    return _load(JPEG).copy()


@lru_cache(maxsize=None)
def animation() -> bytes:
    return require(GIF).read_bytes()


@lru_cache(maxsize=None)
def animation_image() -> Image.Image:
    """The fixture animation, open and parked on frame 0.

    Re-opened from memory rather than ``copy()``-ing a cached handle: ``copy()``
    collapses an animation to a single frame and drops ``format``, and
    ``gif_stego`` reads both to decide whether a carrier is animated.  Keeping
    the stream in memory also avoids holding a file descriptor.
    """
    image = Image.open(BytesIO(animation()))
    image.seek(0)
    return image


@lru_cache(maxsize=None)
def corpus_text() -> str:
    return require(CORPUS).read_text(encoding="utf-8")


def varied_photo(index: int, size: tuple[int, int] = (160, 160)) -> Image.Image:
    """Distinct covers for multi-carrier tests.

    Derived from the photo fixture rather than regenerating noise, so the
    channel sees realistic statistics every time.
    """
    base = photo(size)
    array = np.asarray(base, dtype=np.int16)
    rng = np.random.default_rng(index)
    shift = rng.integers(-40, 41, array.shape, dtype=np.int16)
    shifted = np.clip(array + shift, 0, 255).astype(np.uint8)
    return Image.fromarray(shifted, "RGB")


def flat_cover(value: int = 128, size: tuple[int, int] = (256, 256)) -> Image.Image:
    """Deliberately featureless cover — used to assert rejection, not success."""
    return Image.new("RGB", size, (value, value, value))
