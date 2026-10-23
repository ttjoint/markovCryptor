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
    #: 上一次扫描时文件夹的 (mtime, 文件数)。用来快速判断"要不要重新扫"，
    #: 避免每 2.5 秒轮询都去 scandir 一遍几十张图。
    _folder_stamp: tuple | None = None
    #: 累计发送过的图片数。用户在状态栏看"运行中已发 N 张"。
    sent_total: int = 0
    #: 图库里能用的图片总数（每次扫描更新）。
    total_candidates: int = 0
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

    def note_sent(self, count: int = 1) -> None:
        """记一笔已发送。状态栏显示累计量，让用户知道这次会话发过多少图。"""
        with self._lock:
            self.sent_total += max(0, int(count))

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

    def _folders_stamp(self) -> tuple:
        """(mtime, 文件数) 的快速指纹。

        比 scandir 全量便宜得多：用户几小时不动文件夹时，这里几乎零成本。
        删文件、加文件都会改文件夹 mtime，所以这个信号够用。

        注意 mtime 精度：某些文件系统只到秒，两次操作落在同一秒可能漏检。
        所以还带上文件数做补充 —— 增删都会改变文件数。
        """
        stamp = []
        for folder in self.folders:
            try:
                st = folder.stat()
                count = sum(1 for _ in folder.iterdir())
                stamp.append((str(folder), st.st_mtime, count))
            except OSError:
                stamp.append((str(folder), 0.0, -1))
        return tuple(stamp)

    def sync_with_disk(self) -> dict:
        """让队列跟着磁盘走：删掉的图移出队列，新图补进来。

        没有这个，用户删了文件夹里的图、界面上还在，点了发送却失败；
        而在别处存了新图、界面又看不见，只能手点「补图」。

        返回 ``{"added", "removed", "changed"}``；``changed`` 为 False 时
        调用方可以跳过刷新界面。
        """
        with self._lock:
            stamp = self._folders_stamp()
            if stamp == self._folder_stamp:
                return {"added": 0, "removed": 0, "changed": False}
            self._folder_stamp = stamp

            candidates = _list_candidates(self.folders)
            self.total_candidates = len(candidates)
            live = {str(p) for p in candidates}

            # 1) 移除已经不存在的**图库**图片。临时图（upload）没有磁盘对应，
            #    必须留着 —— 用户刚拖进来的图不该因为文件夹没变就被清掉。
            before = len(self.items)
            self.items = [
                item for item in self.items
                if getattr(item, "origin", "folder") != "folder"
                or str(item.path) in live
            ]
            removed = before - len(self.items)
            if removed:
                event("queue", "sync_removed", count=removed)

            # 2) 记账也要清：已删除的文件不该继续占着"已用过"，
            #    否则图库看起来"全用过了"，随机模式会立刻重置一轮。
            self._used &= live

            # 3) 补新图。
            added = self._replenish_locked()

            return {"added": added, "removed": removed,
                    "changed": bool(added or removed)}

    def _next_candidate(self, candidates: list[Path],
                        skip: set[str] | None = None) -> Path | None:
        """Pick the next picture by the configured strategy.

        ``skip`` 是队列里已经装着的文件 —— 它们不能再被选一次，否则同一张
        图会在队列里出现两遍。
        """
        skip = skip or set()
        fresh = [p for p in candidates if str(p) not in skip]
        if not fresh:
            return None
        if self.pick == "sequential":
            for path in fresh:
                if str(path) not in self._used:
                    return path
            # 全用过了：从头再来（复用是设计好的行为，不是兜底）。
            self._used.clear()
            return fresh[0]
        unseen = [p for p in fresh if str(p) not in self._used]
        if not unseen:
            self._used.clear()
            unseen = list(fresh)
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
        self.total_candidates = len(candidates)
        if not candidates:
            event("queue", "no_candidates", level=logging.DEBUG,
                  folders=[str(f) for f in self.folders])
            return 0

        added = 0
        # 队列里已经有的图库图片。补图时必须跳过它们，否则同一张图会被
        # 重复装进队列（用户会看到两个一样的缩略图，发出去也重复）。
        held = {str(item.path) for item in self.items
                if getattr(item, "origin", "folder") == "folder"}
        # 上限是候选数，避免"所有图都装不下"时无限循环。
        for _ in range(len(candidates)):
            if self.full:
                break
            path = self._next_candidate(candidates, skip=held)
            if path is None:
                break
            # 加进 held：同一个文件在一次补图里只能进队列一次。
            held.add(str(path))
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
            # 唯一的发送出口：在这里记数，状态栏才能显示"已发 N 张"。
            self.sent_total += 1
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
        with self._lock:
            return {
                "queued": len(self.items),
                "max": self.max_items,
                "folders": len(self.folders),
                "folder_names": [Path(f).name or str(f) for f in self.folders],
                "pick": self.pick,
                # 图库里能用的图片总数 vs 队列里已装的数量 —— 用户要能看出
                # "文件夹里还有 40 张没轮上"。
                "total": self.total_candidates,
                "sent": self.sent_total,
            }
