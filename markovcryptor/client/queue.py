"""Pictures to hide ciphertext in, drawn from folders the user points at.

The user does not hand us a stack of pictures; they point at a folder of the
photos they already have, and we take one fresh picture per message. That keeps
us from ever shipping generated images (a risk-control red flag on QQ) and from
asking the user to prepare anything special.

Three rules that are easy to get wrong, so they are stated once here:

**Never delete the user's files.** Dequeuing removes the *picture from our
queue*, not the file. The next session may pick the same picture again; that is
fine and documented in the config, because the alternative -- tracking what was
used -- means writing state to disk, which this client refuses to do.

**Refuse a picture at enqueue, not at send.** A picture too small to carry even
a handshake is useless. Discovering that after the user has queued it and waited
is worse than skipping it with one log line.

**Bounded scanning.** A folder of three thousand stickers must not be read into
memory. Names are listed first (cheap); bytes are read only for the picture
actually being enqueued.
"""

from __future__ import annotations

import logging
import os
import random
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..observability import event

#: Extensions we will even try. Anything else is skipped before opening, so a
#: folder full of `.mp4` costs nothing.
_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
})

#: Size of the control message a queue entry must be able to carry. A handshake
#: is 38 bytes; anything smaller than this could never start a conversation, so
#: there is no reason to keep it in the queue.
_MIN_USEFUL_BYTES = 38


@dataclass
class QueuedImage:
    """One picture waiting to be sent."""

    path: Path
    raw: bytes
    #: Real capacity in bytes, measured with the same function the terminal uses.
    capacity: int = 0
    #: Pixel size. 容量估算要按尺寸算每张能装多少 —— 用户看到的是
    #: "这张 512×512 能装 408 字"，而不是一个抽象字节数。
    size: tuple[int, int] = (0, 0)
    #: 来源：``"folder"``（图库）或 ``"upload"``（用户拖入/粘贴的临时图）。
    #:
    #: 临时图只在内存里、退出即丢，也不参与轮转记账 —— 它们是"这次就要用
    #: 的"，不该因为轮转策略又被跳过。
    origin: str = "folder"

    @property
    def can_carry_handshake(self) -> bool:
        return self.capacity >= _MIN_USEFUL_BYTES


def _measure(raw: bytes) -> tuple[int, tuple[int, int]]:
    """Return ``(capacity_bytes, pixel_size)`` for one picture.

    尺寸也要返回：容量估算得按尺寸算每张能装多少，用户看到的是
    "这张 512×512 能装 408 字"，不是一个抽象字节数。
    """
    import io

    from PIL import Image

    from .. import gif_stego, image_stego

    try:
        with Image.open(io.BytesIO(raw)) as image:
            size = tuple(image.size)
            if getattr(image, "n_frames", 1) > 1 or image.format == "GIF":
                return gif_stego.capacity(image)["max_payload_bytes"], size
            return image_stego.max_payload_bytes(*size), size
    except Exception:                              # noqa: BLE001 - not a picture
        return 0, (0, 0)


def _capacity(raw: bytes) -> int:
    """Byte capacity of one picture, matching what the terminal will accept.

    Uses the same modules the send path uses (``image_stego`` for stills,
    ``gif_stego`` for animations). Any divergence here would let the queue admit
    a picture that :meth:`Terminal.send_covert_in_images` then rejects.
    """
    import io

    import io

    from PIL import Image

    from .. import gif_stego, image_stego

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if getattr(image, "n_frames", 1) > 1 or image.format == "GIF":
                return gif_stego.capacity(image)["max_payload_bytes"]
            return image_stego.max_payload_bytes(*image.size)
    except Exception:                              # noqa: BLE001 - not a picture
        return 0


def _list_candidates(folders: list[Path]) -> list[Path]:
    """Every image file under ``folders``, without reading any bytes.

    ``os.scandir`` and name checks only. Recursion is one level deep on purpose:
    a user's photo folder is flat, and walking an arbitrary tree could hit a
    huge directory by accident.
    """
    found: list[Path] = []
    for folder in folders:
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    if Path(entry.name).suffix.lower() in _IMAGE_EXTS:
                        found.append(Path(entry.path))
        except OSError as exc:
            event("queue", "folder_unreadable", level=logging.WARNING,
                  folder=str(folder), error=exc)
    return found


@dataclass
class PictureQueue:
    """The pending pictures, plus the folders they come from.

    One queue per *account* in practice: the user picked a folder of covers, and
    any conversation can draw from it. Per-friend queues would mean pointing at
    the same folder N times to no benefit.
    """

    folders: list[Path] = field(default_factory=list)
    max_items: int = 8
    pick: str = "random"
    #: Pictures currently in the queue, oldest first.
    items: list[QueuedImage] = field(default_factory=list)
    #: Names already taken this session, so "random" does not repeat while there
    #: are still fresh pictures. In memory only, by design.
    _used: set[str] = field(default_factory=set)
    _rng: random.Random = field(default_factory=random.Random)
    #: Guards ``items``/``_used``. Two threads draw from this queue: the HTTP
    #: handler (user pressed "send") and the OneBot reader thread (auto-replying
    #: to a handshake with a cover). Without it, ``take()`` on a one-item queue
    #: races between the empty-check and ``pop(0)`` and raises IndexError -- and
    #: ``replenish()`` can enqueue the same file twice.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ------------------------------------------------------------------ config

    @classmethod
    def from_config(cls, config: dict) -> "PictureQueue":
        """Build from the ``queue`` block of the client config file."""
        block = config.get("queue") or {}
        folders = [Path(str(p)).expanduser() for p in block.get("folders", ())]
        pick = str(block.get("pick", "random")).lower()
        if pick not in ("random", "sequential"):
            event("queue", "bad_pick_mode", level=logging.WARNING,
                  got=pick, using="random")
            pick = "random"
        try:
            max_items = max(1, int(block.get("max_items", 8)))
        except (TypeError, ValueError):
            max_items = 8
        return cls(folders=folders, max_items=max_items, pick=pick)

    # ------------------------------------------------------------------- state

    def __len__(self) -> int:
        with self._lock:
            return len(self.items)

    @property
    def full(self) -> bool:
        with self._lock:
            return len(self.items) >= self.max_items

    def peek(self) -> QueuedImage | None:
        with self._lock:
            return self.items[0] if self.items else None

    # ------------------------------------------------------------------ filling

    def insert_front(self, raw: bytes, *, name: str = "") -> QueuedImage | None:
        """Put a user-supplied picture at the head of the queue.

        Returns the entry, or ``None`` when the bytes are not a usable picture.

        **插到最前而不是队尾**：用户刚拖进来的图就是"这次想用的"。放到队尾
        会让人选了半天的图排在第 20 位，实际上永远轮不到 —— 那比不允许
        导入更让人困惑。

        临时图**不写磁盘**（零持久化），也不加进 ``_used``：轮转记账是给
        图库用的，不该因为导入过一张就让某张图库图片被跳过。
        """
        capacity, size = _measure(raw)
        if capacity <= 0 or not size[0] or not size[1]:
            event("queue", "insert_rejected", level=logging.INFO,
                  source=name, reason="not a usable picture")
            return None
        entry = QueuedImage(path=Path(name or "上传的图片"), raw=raw,
                            capacity=capacity, size=size, origin="upload")
        with self._lock:
            self.items.insert(0, entry)
            # 超出上限时挤掉**队尾的图库图片**，而不是刚插进来的这张。
            while len(self.items) > self.max_items:
                for index in range(len(self.items) - 1, -1, -1):
                    if self.items[index].origin == "folder":
                        self.items.pop(index)
                        break
                else:
                    self.items.pop()
        event("queue", "inserted", source=name, size=size, capacity=capacity)
        return entry

    def _next_candidate(self, candidates: list[Path]) -> Path | None:
        """Pick the next unseen picture by the configured strategy."""
        if self.pick == "sequential":
            for path in candidates:
                if str(path) not in self._used:
                    return path
            # All seen: start over, rather than stall forever.
            self._used.clear()
            return candidates[0] if candidates else None
        unseen = [p for p in candidates if str(p) not in self._used]
        if not unseen:
            self._used.clear()
            unseen = list(candidates)
        return self._rng.choice(unseen) if unseen else None

    def replenish(self) -> int:
        """Top the queue up from the folders. Returns how many were added.

        Skips pictures that cannot carry even a handshake, so a folder of 16x16
        emoji simply yields nothing rather than filling the queue with unusable
        entries.
        """
        with self._lock:
            return self._replenish_locked()

    def _replenish_locked(self) -> int:
        """Top up. Caller holds ``_lock``."""
        if len(self.items) >= self.max_items:
            return 0
        candidates = _list_candidates(self.folders)
        if not candidates:
            event("queue", "no_candidates", level=logging.DEBUG,
                  folders=[str(f) for f in self.folders])
            return 0

        added = 0
        # 上限是候选数，避免"所有图都装不下"时无限循环。
        for _ in range(len(candidates)):
            if self.full:
                break
            path = self._next_candidate(candidates)
            if path is None:
                break
            self._used.add(str(path))
            try:
                raw = path.read_bytes()
            except OSError as exc:
                event("queue", "read_failed", level=logging.WARNING,
                      path=str(path), error=exc)
                continue
            capacity, size = _measure(raw)
            if capacity < _MIN_USEFUL_BYTES:
                # 装不下握手报文，进队也白占位置。
                event("queue", "too_small", level=logging.DEBUG,
                      path=str(path), capacity=capacity, need=_MIN_USEFUL_BYTES)
                continue
            self.items.append(QueuedImage(path=path, raw=raw,
                                          capacity=capacity, size=size))
            added += 1
        return added

    # ------------------------------------------------------------------- dequeuing

    def take(self) -> QueuedImage | None:
        """Remove and return the front picture, replenishing in the background.

        Does **not** delete the file. The picture simply leaves *our* queue; the
        user's folder is untouched.
        """
        with self._lock:
            if not self.items:
                self._replenish_locked()
            if not self.items:
                return None
            # 判空与 pop 必须同一个临界区：中间放开锁时，另一个线程会把唯一
            # 那张取走，这里再 pop(0) 就是 IndexError。
            item = self.items.pop(0)
            self._replenish_locked()
            return item

    def preview(self) -> list[QueuedImage]:
        """A copy of the pending pictures, oldest first.

        The UI shows these as thumbnails so the user can see *which* pictures are
        about to go out. Returns a copy: the caller iterates it on the HTTP
        thread while the reader thread may be taking from the real list.
        """
        with self._lock:
            return list(self.items)

    def remove(self, index: int) -> bool:
        """Drop the picture at ``index``. Returns False if there is none.

        Only removes it from *our* queue -- the file on disk is never touched.
        """
        with self._lock:
            if not 0 <= index < len(self.items):
                return False
            self.items.pop(index)
            self._replenish_locked()
            return True

    def clear(self) -> None:
        """Drop everything. Called on exit -- the queue is never persisted."""
        with self._lock:
            self.items.clear()
            self._used.clear()

    def stats(self) -> dict:
        with self._lock:
            queued = len(self.items)
        return {"queued": queued, "max": self.max_items,
                "folders": len(self.folders), "pick": self.pick}
