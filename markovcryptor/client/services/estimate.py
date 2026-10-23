"""容量估算：这条消息会占几张图、能装多少字。

## 为什么必须在后端算

前端算不出来：容量取决于 8×8 块的布局（``blocks_total``）、是否放大、
放大后的尺寸、握手帧开销。用「平均容量」猜也不准 —— 队列里可能混着
256×256 和 1600×1600，平均值毫无意义。

所以这里**逐张按待选顺序累加**，返回真实占用张数和每张的实际容量。
界面显示的「将会发送 4/9 张图片」就是这里的 ``images_needed``。

## 为什么每条消息都要重算

队列是滑动/循环的。用户换图、删图、改文本，占用张数就变。前端 300ms 防抖
调一次，代价很小（纯计算，不碰网络和 IO）。
"""

from __future__ import annotations

import math
from typing import Any

from ... import image_stego
from ..account import Account

#: 一条消息最多几张图。QQ 通常允许 10，这里留一张余量。
IMAGE_LIMIT = 9

#: 文本信道单条上限（字符）。
#:
#: 文本隐写会膨胀：实测 800 汉字 → 约 1540 字符（1.92×），短文本更高。
TEXT_LIMIT = 2000


def _cover_capacity(size: tuple[int, int], payload: int) -> dict[str, Any]:
    """One cover's real capacity after clamping/upscaling."""
    w, h = size
    need = image_stego.blocks_needed(payload)
    ratio = math.sqrt(need / max(1, image_stego.blocks_total(w, h)))

    if image_stego.blocks_total(w, h) >= need:
        return {"size": [w, h], "final": [w, h], "scale": 1.0, "fits": True}

    if ratio > image_stego.MAX_UPSCALE_RATIO:
        # 放大上限也不够：这张图对本条消息没有贡献。
        return {"size": [w, h], "final": [w, h], "scale": 1.0, "fits": False}

    side_w = max(1, math.ceil(w * ratio))
    side_h = max(1, math.ceil(h * ratio))
    while image_stego.blocks_total(side_w, side_h) < need:
        side_w += image_stego.BLOCK // 2
        side_h += image_stego.BLOCK // 2
    return {"size": [w, h], "final": [side_w, side_h],
            "scale": round(side_w / w, 3), "fits": True}


def estimate(account: Account, *, text: str, channel: str = "image") -> dict:
    """Work out what one message costs.

    Returns ``{"ok", "channel", "bytes", "slots", "images_needed", "limit",
    "items", "reason"}``. ``ok`` is False when the message cannot be sent at
    all, with ``reason`` explaining what to change.
    """
    payload = text.encode("utf-8")

    if channel == "text":
        # 文本信道的瓶颈是 QQ 单条文本长度，与图片无关。
        return {
            "ok": len(text) <= TEXT_LIMIT,
            "channel": "text",
            "chars": len(text),
            "limit": TEXT_LIMIT,
            "bytes": len(payload),
            "slots": 0,
            "images_needed": 0,
            "image_limit": 0,
            "items": [],
            "reason": "" if len(text) <= TEXT_LIMIT else
                      f"文本隐写最多 {TEXT_LIMIT} 字符，当前 {len(text)} 字符",
        }

    # 图片信道：按队列顺序逐个槽位累加。
    #
    # 队列不足 9 张时**循环**填充 —— 同一张源图可以反复用作载体（每次都
    # 嵌入不同内容，所以产生的图片互不相同）。这样只有 3 张图也能发满
    # 9 个槽位，界面需要说明「其中 N 张重复使用」。
    queued = list(account.queue.preview())
    slots = IMAGE_LIMIT
    if not queued:
        return {
            "ok": False, "channel": "image", "chars": len(text),
            "bytes": len(payload), "slots": 0, "images_needed": 0,
            "image_limit": IMAGE_LIMIT, "items": [],
            "reason": "队列里没有图片。先在设置里指定文件夹，或拖入几张。",
        }

    # 每张图能装多少字节：用 blocks → payload 的反函数逐个求。
    remaining = len(payload) + image_stego.PUBLIC_KEY_BYTES  # 预留握手余量
    items: list[dict] = []
    used = 0
    repeats = 0
    for slot in range(slots):
        source = queued[slot % len(queued)]
        if slot >= len(queued):
            repeats += 1
        # 这张图的纯容量（不放大时的上限），用于"能装多少字"的展示。
        cap = int(source.bytes) if hasattr(source, "bytes") else 0
        if remaining <= 0:
            break

        # 逐个字节问 blocks：容量函数单调，二分即可。
        take = _max_fit(source, remaining)
        capacity = _capacity_bytes(source)
        if capacity <= 0:
            continue                          # 这张太小，跳过
        take = min(remaining, capacity)
        remaining -= take
        used += 1
        items.append({
            "slot": slot + 1,
            "size": list(getattr(source, "size", (0, 0))),
            "capacity": capacity,
            "takes": take,
            "repeated": slot >= len(queued),
            **_cover_capacity(getattr(source, "size", (0, 0)), take),
        })
        if remaining <= 0:
            break

    ok = remaining <= 0
    reason = ""
    if not ok:
        reason = (f"当前 {min(len(queued), IMAGE_LIMIT)} 张图最多装 "
                  f"{len(payload) - remaining} 字节，还差 {remaining} 字节")

    return {
        "ok": ok,
        "channel": "image",
        "chars": len(text),
        "bytes": len(payload),
        "slots": slots,
        "images_needed": used,
        "image_limit": IMAGE_LIMIT,
        "repeated": repeats,
        "queued": len(queued),
        "items": items,
        "reason": reason,
    }


def _capacity_bytes(item) -> int:
    """Usable payload bytes for one queued cover (after clamping)."""
    size = getattr(item, "size", None)
    if not size:
        return 0
    return image_stego.max_payload_bytes(*size)


def _max_fit(item, budget: int) -> int:
    size = getattr(item, "size", None)
    if not size:
        return 0
    return min(budget, image_stego.max_payload_bytes(*size))
