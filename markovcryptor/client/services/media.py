"""媒体转换：data URL、缩略图、头像地址。

全部是纯函数（除了 model_folder 读 account），不依赖 FastAPI。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from ..account import Account

def decode_data_url(value: str) -> bytes:
    """Turn ``data:image/png;base64,...`` into bytes.

    The browser hands pasted and dropped pictures over as data URLs, so the
    server needs to undo that before it has an image at all.
    """
    if value.startswith("data:"):
        header, _, body = value.partition(",")
        if ";base64" not in header:
            raise HTTPException(status_code=422, detail="只支持 base64 编码的图片")
        try:
            raw = base64.b64decode(body, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail="图片数据不是合法 base64") from None
    else:
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail="图片数据不是合法 base64") from None
    if not raw:
        raise HTTPException(status_code=422, detail="图片是空的")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单张图片不能超过 8MB")
    return raw


def peer_avatar(account: Account, peer: str) -> str:
    """Avatar URL for a conversation peer.

    Prefers the friend record (which may carry a cached URL) and falls back to
    building one from the QQ number -- a stranger can message you, and their row
    still needs a picture.
    """
    from ..directory import avatar_url

    friend = account.directory.get(peer)
    if friend is not None and friend.avatar:
        return friend.avatar
    return avatar_url(peer)


def thumbnail(raw: bytes) -> str | None:
    """A small JPEG data URL for the queue list."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            image.thumbnail((_THUMB_SIZE, _THUMB_SIZE))
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(
            buffer.getvalue()).decode("ascii")
    except Exception:                               # noqa: BLE001 - cosmetic
        return None


def queue_items(account: Account) -> list[dict]:
    """Pending pictures as ``{name, thumb}``, oldest first."""
    items = []
    for item in account.queue.preview():
        size = getattr(item, "size", (0, 0))
        items.append({
            "name": Path(item.path).name if getattr(item, "path", None) else "",
            "thumb": thumbnail(item.raw),
            # 来源与容量：界面要标出"这张是刚拖进来的"以及"能装 N 字"。
            # 只有缩略图的话，用户没法判断哪张够用。
            "origin": getattr(item, "origin", "folder"),
            "size": [size[0], size[1]],
            "capacity": getattr(item, "capacity", 0),
        })
    return items


def model_folder(account: Account) -> Path | None:
    """The configured model folder, if it exists."""
    lib = account.models
    if lib is None or lib.folder is None:
        return None
    folder = Path(lib.folder)
    return folder if folder.is_dir() else None
