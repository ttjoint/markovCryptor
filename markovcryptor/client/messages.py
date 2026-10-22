"""Reading a QQ message: what we understand, and what we merely acknowledge.

QQ's message format is enormous -- stickers, cards, nested forwards, voice,
shakes, red packets, group invites. We parse exactly two kinds properly, image
and text, because those are the only ones the covert channel travels in. For
everything else the requirement is explicit: do not try to render it, show one
short line saying what arrived. Guessing at a card's contents and getting it
wrong is worse than admitting we do not handle it.

That "one short line" decision lives here rather than in the UI, because the
same fallback is needed by the web layer, the tests and any future front end.
Putting it in the template would mean writing the mapping three times and
letting them drift.

Two fields matter and are easy to confuse:

``text``
    The *visible* text, concatenated from all text segments. This is what a
    normal messenger would show.

``images``
    Image records, still undownloaded. Downloading is the network layer's job
    (it has the token and the timeouts); this module only says which ones exist
    and in what order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: How each segment type is described when we cannot render it.
#:
#: Grouped by wording rather than by protocol name: the user is reading a
#: sentence, not a spec. "对方发送了 语音" is right; "对方发送了 record" is what
#: happens when a template decides to print ``seg["type"]`` and we are too lazy
#: to override it.
_NOT_RENDERED = {
    "record": "语音",
    "video": "视频",
    "file": "文件",
    "json": "卡片消息",
    "xml": "卡片消息",
    "forward": "聊天记录",
    "node": "聊天记录",
    "mface": "表情包",
    "face": "表情",
    "markdown": "富文本消息",
    "keyboard": "按钮消息",
    "music": "音乐分享",
    "dice": "骰子",
    "rps": "猜拳",
    "poke": "戳一戳",
    "shake": "窗口抖动",
    "contact": "名片",
    "location": "位置",
    "reply": "回复",
    "at": "提到某人",
}

#: Segments that carry no user-visible meaning at all. A ``reply`` marker sits
#: next to the text it quotes, and announcing it as "对方发送了 回复" would be
#: noise on a message the user can already read.
_SILENT = frozenset({"reply", "at", "source", "node"})


@dataclass(frozen=True)
class ImageRef:
    """One image segment, before download.

    ``summary`` is QQ's own caption (``[图片]``, ``[动画表情]``); ``sub_type``
    is 0 for a real picture and non-zero for a sticker, which matters because a
    sticker is usually too small to carry a handshake.
    """

    url: str | None = None
    file: str | None = None
    summary: str | None = None
    sub_type: int = 0

    @property
    def is_sticker(self) -> bool:
        return self.sub_type != 0

    def locate(self) -> str | None:
        """Best handle for fetching: a real URL, else a URL-ish ``file``."""
        for value in (self.url, self.file):
            if isinstance(value, str) and value.startswith(
                    ("http://", "https://", "file://")):
                return value
        return None


@dataclass(frozen=True)
class ParsedMessage:
    """What we could make of one inbound QQ message."""

    sender: str = ""
    text: str = ""
    images: tuple[ImageRef, ...] = ()
    #: One short line per segment we deliberately did not render, in order.
    #: Empty for a message that was entirely text and pictures.
    unsupported: tuple[str, ...] = ()
    message_id: str | None = None
    #: Unix seconds from QQ's own ``time`` field. 0 when the payload did not
    #: carry one (a synthetic event, or an older bridge), which the UI renders
    #: as "no time" rather than inventing one.
    at: float = 0.0

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.images)

    @property
    def only_unsupported(self) -> bool:
        """True when the whole message was things we decline to render.

        The UI uses this to decide between appending to a conversation and
        showing a placeholder line in the list; a message that is only a shake
        should not open an empty conversation pane.
        """
        return bool(self.unsupported) and not self.has_content


def _truthy_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def segments_of(payload: dict) -> list[dict]:
    """Message segments from an OneBot event, tolerating the string form.

    ``messageFormat=array`` is what we configure, but a hand-edited config could
    be ``string``, and quietly returning nothing there would look like the
    message vanished.
    """
    message = payload.get("message")
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    raw = payload.get("raw_message")
    if not raw and isinstance(message, str):
        raw = message
    return [{"type": "text", "data": {"text": raw}}] if raw else []


def parse(payload: dict) -> ParsedMessage:
    """Turn one OneBot private-message event into something renderable.

    Never raises. A malformed segment is skipped, not turned into an exception:
    this runs on the socket reader, and one odd message must not take down the
    connection.
    """
    texts: list[str] = []
    images: list[ImageRef] = []
    unsupported: list[str] = []

    for seg in segments_of(payload):
        kind = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if kind == "text":
            texts.append(str(data.get("text") or ""))
        elif kind == "image":
            images.append(ImageRef(
                url=data.get("url") if isinstance(data.get("url"), str) else None,
                file=data.get("file") if isinstance(data.get("file"), str) else None,
                summary=data.get("summary") if isinstance(data.get("summary"), str) else None,
                sub_type=_truthy_int(data.get("sub_type")),
            ))
        elif kind in _SILENT:
            continue
        else:
            label = _NOT_RENDERED.get(kind) or kind or "未知消息"
            # 同一类连续出现（三个表情）说一次就够，不要刷三行。
            line = f"对方发送了 {label}"
            if not unsupported or unsupported[-1] != line:
                unsupported.append(line)

    # ``sender.user_id`` is the authoritative author for history entries, while
    # a live private-message event only carries a top-level ``user_id`` (the
    # friend). Prefer the nested one so history can tell who said what.
    sender = payload.get("user_id")
    sender_info = payload.get("sender")
    if isinstance(sender_info, dict) and sender_info.get("user_id") is not None:
        sender = sender_info["user_id"]

    return ParsedMessage(
        sender=str(sender) if sender is not None else "",
        text="".join(texts),
        images=tuple(images),
        unsupported=tuple(unsupported),
        message_id=str(payload.get("message_id")) if payload.get("message_id") is not None else None,
        at=_timestamp(payload),
    )


def _timestamp(payload: dict) -> float:
    """QQ's ``time`` field as a float, or 0.0 when absent/unparsable.

    QQ sends seconds. Anything wildly out of range is treated as absent rather
    than shown as a date in 1970 or 55000 -- a wrong timestamp is worse than
    none.
    """
    for key in ("time", "timestamp"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        # 2001-09-09 .. 2286: anything outside is not a real chat timestamp.
        if 1_000_000_000 <= seconds <= 10_000_000_000:
            return seconds
    return 0.0


def describe_unsupported(kind: str) -> str:
    """One short line for a segment we do not render.

    Exposed so the UI can label a history entry it received as a bare type
    string, without duplicating the wording table.
    """
    return f"对方发送了 {_NOT_RENDERED.get(kind) or kind or '未知消息'}"
