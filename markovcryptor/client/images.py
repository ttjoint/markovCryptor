"""Fetching QQ images so the chat window can show them.

QQ sends an image as a URL, not as bytes. Everything downstream (the stego layer,
the picture queue, the chat window) wants bytes, so something has to download
them. This module is that something, with three deliberate constraints:

**Never block the reader thread for long.** Downloads happen on a small worker
pool. A slow CDN must not stall incoming messages -- the reader thread is what
keeps handshakes and covert messages flowing.

**Never grow without bound.** The client keeps no history on disk, so every
decoded image lives in memory. A hard cap on total bytes plus per-image limits
keeps a long session from turning into a memory leak.

**Never fail loudly.** A picture that will not download is an ordinary event (the
CDN link expired, the network blipped). The chat shows the placeholder line it
already had; the user is not told about an HTTP error they cannot act on.
"""

from __future__ import annotations

import io
import logging
import threading
import urllib.request
from collections import OrderedDict

from ..observability import event

#: QQ's image CDN is normally fast; anything slower than this is not worth
#: holding up a message for.
DEFAULT_TIMEOUT = 8.0

#: Per-image ceiling. QQ compresses chat pictures well below this; anything
#: larger is either not what we think it is, or too big to keep in RAM.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: Total decoded bytes kept. History merges can pull twenty pictures at once,
#: and the queue holds a handful more.
MAX_TOTAL_BYTES = 64 * 1024 * 1024

#: Decoded size ceiling -- a small file can still decode into a huge bitmap.
MAX_PIXELS = 4096 * 4096


class ImageCache:
    """Downloads and remembers images, bounded by total bytes.

    Keyed by URL, so the same picture referenced by a live message and again by a
    history merge is fetched once. Least-recently-used entries are dropped first.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT,
                 max_bytes: int = MAX_TOTAL_BYTES) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._total = 0
        self._lock = threading.RLock()
        self._inflight: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------ public

    def get(self, url: str | None) -> bytes | None:
        """Bytes for ``url``, downloading if needed. ``None`` if unavailable."""
        if not url:
            return None
        with self._lock:
            hit = self._items.get(url)
            if hit is not None:
                self._items.move_to_end(url)
                return hit
            pending = self._inflight.get(url)

        if pending is not None:
            # 另一个线程正在下同一张图：等它，别重复下载。
            pending.wait(timeout=self.timeout + 2)
            with self._lock:
                return self._items.get(url)

        done = threading.Event()
        with self._lock:
            self._inflight[url] = done
        try:
            raw = self._download(url)
            if raw is not None:
                self._store(url, raw)
            return raw
        finally:
            with self._lock:
                self._inflight.pop(url, None)
            done.set()

    def prefetch(self, urls, *, on_ready=None) -> threading.Thread:
        """Download several images in the background.

        ``on_ready`` is called once after the batch finishes, not per image: the
        chat window re-renders per conversation, so notifying per picture would
        cause a burst of identical re-renders.
        """
        targets = [u for u in urls if u]

        def worker():
            for url in targets:
                try:
                    self.get(url)
                except Exception as exc:            # noqa: BLE001 - background
                    event("images", "prefetch_failed", level=logging.DEBUG,
                          url=url, error=exc)
            if on_ready is not None:
                try:
                    on_ready()
                except Exception as exc:            # noqa: BLE001
                    event("images", "prefetch_callback_failed",
                          level=logging.WARNING, error=exc)

        thread = threading.Thread(target=worker, name="image-prefetch",
                                  daemon=True)
        thread.start()
        return thread

    def stats(self) -> dict:
        with self._lock:
            return {"count": len(self._items),
                    "bytes": self._total,
                    "max_bytes": self.max_bytes}

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._total = 0

    # ----------------------------------------------------------------- internal

    def _store(self, url: str, raw: bytes) -> None:
        with self._lock:
            if url in self._items:
                return
            self._items[url] = raw
            self._total += len(raw)
            # 超上限就从头丢 —— OrderedDict 的头部是最久没用的。
            while self._total > self.max_bytes and len(self._items) > 1:
                _old, dropped = self._items.popitem(last=False)
                self._total -= len(dropped)

    def _download(self, url: str) -> bytes | None:
        if not url.startswith(("http://", "https://")):
            return None
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; mkx-client)"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_IMAGE_BYTES + 1)
        except Exception as exc:                    # noqa: BLE001 - CDN/network
            event("images", "download_failed", level=logging.DEBUG,
                  url=url, error=exc)
            return None

        if len(raw) > MAX_IMAGE_BYTES:
            event("images", "download_too_large", level=logging.WARNING,
                  url=url, bytes=len(raw))
            return None
        if not raw:
            return None
        if not _looks_like_image(raw):
            # 不是图片就别往界面里塞，否则浏览器会显示破图图标。
            event("images", "download_not_an_image", level=logging.DEBUG, url=url)
            return None
        if not _within_pixel_budget(raw):
            event("images", "download_too_many_pixels", level=logging.WARNING,
                  url=url)
            return None
        return raw


def _looks_like_image(raw: bytes) -> bool:
    """Cheap magic-number check. Avoids handing HTML error pages to <img>."""
    head = raw[:12]
    return (head.startswith(b"\xff\xd8\xff")            # JPEG
            or head.startswith(b"\x89PNG\r\n\x1a\n")    # PNG
            or head.startswith(b"GIF87a")
            or head.startswith(b"GIF89a")
            or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
            or head.startswith(b"BM"))                   # BMP


def _within_pixel_budget(raw: bytes) -> bool:
    """Reject images whose decoded bitmap would be enormous.

    A 200 KB PNG can decode to a 20000x20000 bitmap; the file-size cap alone does
    not protect memory.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
        return width * height <= MAX_PIXELS
    except Exception:                               # noqa: BLE001 - not decodable
        return False
