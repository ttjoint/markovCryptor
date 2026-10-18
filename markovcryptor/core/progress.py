"""Progress reporting shared by training, encoding and decoding.

A progress callback receives ``(fraction, message)`` with ``fraction`` in
``[0, 1]``. It may raise :class:`Cancelled` to abort the running operation.
"""

from __future__ import annotations

from typing import Callable, Optional

ProgressFn = Callable[[float, str], None]
# Long loops report about this often (in iterations), keeping callback overhead negligible.
REPORT_EVERY = 4096


class Cancelled(Exception):
    """Raised from a progress callback to abort the operation."""


def report(progress: Optional[ProgressFn], fraction: float, message: str) -> None:
    if progress is not None:
        progress(min(max(fraction, 0.0), 1.0), message)


def stage(progress: Optional[ProgressFn], start: float, end: float) -> Optional[ProgressFn]:
    """Map a sub-task's 0..1 progress onto ``start..end`` of the parent callback."""
    if progress is None:
        return None
    width = end - start

    def scoped(fraction: float, message: str) -> None:
        progress(start + width * min(max(fraction, 0.0), 1.0), message)

    return scoped
