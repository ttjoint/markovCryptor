"""把 QQ 历史消息合并进会话，并按需下载图片。

历史只合并一次：重复点开同一个好友不会重复灌入。
历史条目的 seq 用**负数**，实时消息是全局递增的正数 —— 两者不能混，
混过之后前端会把新消息当成旧消息全部跳过。
"""

from __future__ import annotations

import base64
import logging
import threading

from ...observability import event
from ..account import Account

def merge_history(account: Account, conversation, peer: str, count: int) -> None:
    """Prepend QQ's own history as visible entries, oldest first.

    Only messages that carry something renderable are added. A history full of
    stickers would otherwise push a wall of "对方发送了 表情包" into the window
    the moment the user opens it.
    """
    from ..conversations import Entry

    try:
        parsed = account.load_history(peer, count=count)
    except Exception as exc:                        # noqa: BLE001
        event("client", "history_merge_failed", level=logging.WARNING,
              peer=peer, error=exc)
        return

    merged: list = []
    #: (entry, urls) —— 插入之后再下载，界面先出内容、图随后补上。
    pending_images: list = []

    for item in parsed:
        placeholders = list(item.unsupported)
        urls = [ref.locate() for ref in item.images]
        urls = [u for u in urls if u]

        # 归属由 QQ 自己的 sender 决定，不能一律当"对方"。
        # 硬编码 mine=False 会让用户自己发过的话也显示在左边、和对方一模一样,
        # 界面上根本分不出谁说的 —— 这正是实际反馈的问题。
        mine = bool(account.uin) and item.sender == account.uin

        # **图片能下载时不要加占位文字。**
        # 之前对纯图片消息无条件加一行"对方发送了 图片"，等图片真的下载
        # 出来之后，就变成一条带着图的消息旁边还写着"对方发送了图片"——
        # 重复且荒谬。只有拿不到 URL（真的显示不了）时才留一行说明。
        if item.images and not urls and not item.text.strip():
            who = "我发送了" if mine else "对方发送了"
            placeholders.append(f"{who} 图片（无法加载）")

        if not (item.text.strip() or placeholders or urls):
            continue

        entry = Entry(
            mine=mine,
            text=item.text,
            unsupported=tuple(placeholders),
            at=item.at or 0.0,
            # 历史条目也要带 id：未打开会话时收到的消息已经实时记过一条，
            # 合并历史时靠它认出重复。
            message_id=getattr(item, "message_id", None),
        )
        merged.append(entry)
        if urls:
            pending_images.append((entry, urls))

    # 去重与插入在会话的锁内一次完成：先快照再切片赋值会抹掉期间到达的
    # 新消息，而"检查标志位 + 插入"分开做会让并发打开灌两遍历史。
    if conversation.prepend_history(merged) and pending_images:
        download_history_images(account, pending_images)


def download_history_images(account: Account, pending: list) -> None:
    """Fetch history pictures in the background and attach them to entries.

    Mutating ``entry.images`` in place keeps each entry's ``seq``, so the browser
    appends the picture instead of rebuilding the whole thread (and losing the
    user's scroll position).
    """
    cache = getattr(account, "images", None)
    if cache is None:
        return

    def worker():
        for entry, urls in pending:
            raws = tuple(raw for raw in (cache.get(u) for u in urls) if raw)
            if raws:
                entry.images = raws

    import threading
    threading.Thread(target=worker, name="history-images", daemon=True).start()
