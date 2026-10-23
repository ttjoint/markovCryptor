"""One conversation per friend, created the first time they say anything.

Why lazy matters
----------------
A QQ account has ~30 friends. Building a :class:`Terminal` for all of them at
startup would create 30 independent key pairs, 30 inboxes and 30 picture
queues, of which at most two or three are ever used. Worse, it would make the
"who am I talking to" state ambiguous: a terminal exists, so it looks like a
conversation, but nothing was ever said.

So a conversation is created on first contact -- an inbound message, or the user
opening it. That also gives a natural definition of unread: created and not yet
opened.

The routing guarantee
---------------------
Each conversation's terminal is registered with ``peer=<uin>``, so the transport
delivers only that friend's packets to it. This is the whole reason
``OneBotNetwork.register`` grew a ``peer`` filter: without it, friend A's picture
would also reach friend B's session, fail authentication there, and surface in
B's window as a mysterious "解不开".

Ordering note, relied upon below: ``OneBotNetwork._handle_event`` calls the raw
``on_event`` callbacks *before* it looks up per-peer sinks. That is what lets a
first message from a stranger create the conversation in time for the sink to
exist when the packet is handed out. Reordering those two loops would silently
break first contact.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field

from ..observability import event
from ..terminal.terminal import Inbound, Packet, Terminal
from .directory import Directory

#: Guards every mutable structure in this module.
#:
#: Two threads genuinely touch them: the OneBot reader thread delivers inbound
#: messages, and the uvicorn thread serves HTTP. Without a lock the failures are
#: not theoretical -- `entries[:] = merged + existing` (history merge) can drop a
#: message that arrived during the assignment, and iterating `_items` while
#: `ensure()` inserts raises "dictionary changed size during iteration".
#:
#: One module-wide lock rather than one per conversation: contention is trivial
#: (a handful of messages per second) and a single lock removes the ordering
#: questions that per-object locks invite.
_STATE_LOCK = threading.RLock()

#: Arrival counter shared by every conversation. Ordering needs one globally
#: monotonic value: a per-conversation counter would make two chats' latest
#: messages compare equal, and the list would fall back to name order.
_ARRIVAL = itertools.count()
from . import messages as message_parser


@dataclass
class Entry:
    """One line in a conversation timeline.

    Both directions live in one list so the UI can render a single thread. Which
    side an entry came from is ``mine``, not a separate list -- two lists would
    have to be merged by timestamp at render time and would interleave wrongly on
    a clock skew.
    """

    at: float = field(default_factory=time.time)
    #: Global arrival index. Wall-clock ``at`` is not enough for ordering: two
    #: messages arriving in the same millisecond compare equal, and the chat list
    #: would then fall back to name order -- which looks like the wrong person
    #: jumping to the top.
    seq: int = 0
    mine: bool = False
    #: Visible text (what a normal messenger shows).
    text: str = ""
    #: Image bytes, ready to display.
    images: tuple[bytes, ...] = ()
    #: Decrypted covert messages, in arrival order.
    covert: tuple[str, ...] = ()
    #: One short line per thing we declined to render ("对方发送了 语音").
    unsupported: tuple[str, ...] = ()
    #: Diagnostic lines from the stego layer (a corrupt frame, a missing slice).
    notes: tuple[str, ...] = ()

    #: QQ's own message id, when we have one.
    #:
    #: Used to recognise "this is the same message we already showed". Without
    #: it, a message that arrived while the conversation was closed got logged
    #: live, then appeared *again* from history when the user opened it -- the
    #: same line twice in the timeline.
    message_id: str | None = None

    def is_empty(self) -> bool:
        return not (self.text.strip() or self.images or self.covert
                    or self.unsupported or self.notes)


def _fingerprint(entry: "Entry") -> tuple:
    """Identity of a message without a message id.

    Only used for entries that came from history and have no id -- a best-effort
    fallback so a live-logged message is not repeated when its history copy
    arrives. Deliberately includes the timestamp: two messages saying "嗯" at
    different times are different messages and must both survive.
    """
    return (bool(entry.mine), entry.text.strip(), round(entry.at or 0.0, 3),
            len(entry.images))


class Conversation:
    """One friend: their terminal, their timeline, their unread count."""

    def __init__(self, peer: str, terminal: Terminal, name: str = "") -> None:
        self.peer = str(peer)
        self.terminal = terminal
        self.name = name or self.peer
        self.entries: list[Entry] = []
        #: Messages that arrived while this conversation was not open.
        #: Reset by :meth:`mark_read`.
        self.unread = 0
        self._open = False
        self._history_merged = False

    # ------------------------------------------------------------------ state

    @property
    def established(self) -> bool:
        return self.terminal.established

    @property
    def state_label(self) -> str:
        return self.terminal.state.label

    @property
    def last_at(self) -> float:
        with _STATE_LOCK:
            return self.entries[-1].at if self.entries else 0.0

    @property
    def last_seq(self) -> int:
        """Ordering key that is stable even for same-millisecond arrivals.

        -1 for a conversation with no messages, so it sorts below any that has
        one -- opening a silent friend should not jump them to the top.
        """
        with _STATE_LOCK:
            return self.entries[-1].seq if self.entries else -1

    def snapshot(self) -> list[Entry]:
        """A copy of the timeline, safe to iterate outside the lock.

        Callers must not hold the lock while serialising to JSON, and must not
        iterate the live list -- a history merge prepends to it and an in-flight
        message appends, either of which makes a bare iterator repeat or skip
        entries.
        """
        with _STATE_LOCK:
            return list(self.entries)

    def mark_open(self, is_open: bool = True) -> None:
        """Tell the conversation whether the user is looking at it.

        Unread only increments while closed. Counting while the user watches the
        window would leave a red dot on a conversation they just read.
        """
        with _STATE_LOCK:
            self._open = is_open
            if is_open:
                self.unread = 0

    def mark_read(self) -> None:
        with _STATE_LOCK:
            self.unread = 0

    def prepend_history(self, entries: list[Entry]) -> bool:
        """Put older messages in front, once.

        Returns False when history was already merged. The check and the set
        happen under the same lock: two concurrent "open conversation" requests
        would otherwise both pass the check and prepend the same history twice.
        """
        with _STATE_LOCK:
            if self._history_merged:
                return False
            self._history_merged = True
            # 历史条目必须拿到 seq，否则前端增量渲染会把它们当成"已渲染过"
            # 全部跳过，界面上一片空白。
            #
            # 但**不能给已有条目重新编号**。实际踩过的坑：这里把已有条目
            # 重排成 base..base+n（base = len(history)），而 _append 用的是
            # 全局 _ARRIVAL 计数器 —— 两者互不知情。结果新发的消息拿到
            # 6、7 这样的小号，而重新编号后的老消息是 20..25，seq 出现回退，
            # 前端 `seq <= lastSeq` 全跳过：**新消息发出去了但界面不显示**。
            #
            # 正确做法：历史用**负数**，天然排在所有实时消息之前，且永不与
            # _ARRIVAL 分配的正数冲突。已有条目一个都不动。
            # 去重：未打开会话时收到的消息已经实时记过一条，随后拉历史又
            # 会来一次。没有这一步，用户点进去会看到同一条消息重复两遍。
            #
            # 优先用 QQ 的 message_id（精确）。没有 id 时退回"内容 + 时间戳"
            # 指纹：历史里偶尔会有没有 id 的条目，宁可少去重一条，也不要
            # 把两条确实不同的相同内容消息（"嗯"、"好"）错误合并。
            seen_ids = {e.message_id for e in self.entries if e.message_id}
            seen_fingerprints = {_fingerprint(e) for e in self.entries}

            fresh: list[Entry] = []
            for entry in entries:
                if entry.message_id and entry.message_id in seen_ids:
                    continue
                if not entry.message_id and _fingerprint(entry) in seen_fingerprints:
                    continue
                fresh.append(entry)

            for index, entry in enumerate(fresh):
                entry.seq = -(len(fresh) - index)
            self.entries[:] = list(fresh) + self.entries
            return True

    # ---------------------------------------------------------------- inbound

    def _append(self, entry: Entry) -> Entry:
        if entry.is_empty():
            # 空白条目不进时间线：它会让界面出现一个空气泡，且不携带任何信息。
            return entry
        with _STATE_LOCK:
            entry.seq = next(_ARRIVAL)
            self.entries.append(entry)
            if not self._open:
                self.unread += 1
        return entry

    def record_visible(self, parsed: message_parser.ParsedMessage,
                       images: tuple[bytes, ...] = ()) -> Entry:
        """Log what a normal messenger would show for an inbound message."""
        return self._append(Entry(
            mine=False,
            text=parsed.text,
            images=images,
            unsupported=parsed.unsupported,
            at=getattr(parsed, "at", 0.0) or 0.0,
            # 带上 id，合并历史时才能认出"这条已经显示过了"。
            message_id=getattr(parsed, "message_id", None),
        ))

    def record_inbound(self, inbound: Inbound) -> None:
        """Log what the stego layer recovered from one packet.

        Called *in addition* to :meth:`record_visible` when the same packet also
        had recoverable content. Keeping them separate means a message that is
        entirely ordinary still produces one visible entry and no covert entry,
        rather than one entry that has to be interpreted by the renderer.
        """
        if not (inbound.covert or inbound.notes or inbound.handshake):
            return
        self._append(Entry(
            mine=False,
            covert=inbound.covert,
            notes=inbound.notes,
        ))

    def record_outbound(self, text: str = "", images: tuple[bytes, ...] = (),
                        covert: tuple[str, ...] = ()) -> Entry:
        return self._append(Entry(mine=True, text=text, images=images,
                                  covert=covert))


def _fill_images(cache, entry, urls) -> None:
    """Attach downloaded pictures to an entry that is already on screen.

    Called from the prefetch thread. Mutating ``entry.images`` in place (rather
    than replacing the entry) keeps its ``seq``, so the browser appends the
    picture instead of re-rendering the whole thread.
    """
    raws = tuple(raw for raw in (cache.get(url) for url in urls) if raw)
    if not raws:
        return
    with _STATE_LOCK:
        entry.images = raws


class Conversations:
    """Every conversation with one account, keyed by the friend's number.

    Owns the single ``on_event`` registration, so new conversations can be
    created from an inbound message even if the user never opened that friend.
    """

    def __init__(self, network, directory: Directory, *, psk: str = "pbx1",
                 text_channel=None, cover_for=None, tasks=None) -> None:
        self.network = network
        self.directory = directory
        #: 接收入闸的来源。解码一张 1600² 要 3.3 秒，而 deliver() 跑在读
        #: 线程上 —— 对方连发十张大图就能把客户端堵死。闸门由 TaskManager
        #: 提供：它知道当前积压多少，本类只负责问一句。
        self.tasks = tasks
        self.psk = psk
        self.text_channel = text_channel
        #: ``cover_for(peer) -> bytes | None``. A handshake reply needs one of
        #: *our* pictures; there is no browser queue on a native client, so the
        #: answer comes from that friend's picture queue via this callback.
        self._cover_for = cover_for
        self._items: dict[str, Conversation] = {}
        #: 本机 QQ 号。用来判断某条消息是不是自己发的 —— QQ 会把我们
        #: 发的消息回推，历史里也两边都有。由 :meth:`attach_self` 填入。
        self.uin: str = ""

    # ------------------------------------------------------------- accessors

    def __len__(self) -> int:
        with _STATE_LOCK:
            return len(self._items)

    def __contains__(self, peer) -> bool:
        with _STATE_LOCK:
            return str(peer) in self._items

    def __iter__(self):
        return iter(self.ordered())

    def get(self, peer) -> Conversation | None:
        with _STATE_LOCK:
            return self._items.get(str(peer))

    def ordered(self) -> list[Conversation]:
        """Most recent first -- a chat list is ordered by activity, not alphabet.

        Never-spoken-to conversations (created by opening a friend) sort to the
        bottom rather than the top, otherwise clicking a silent friend would jump
        them above the person you are actually talking to.
        """
        with _STATE_LOCK:
            items = list(self._items.values())
        # 排序放在锁外：last_seq 各自取锁，不会持有全局锁做 O(n log n)。
        return sorted(items, key=lambda c: (c.last_seq, c.name), reverse=True)

    def unread_total(self) -> int:
        with _STATE_LOCK:
            items = list(self._items.values())
        return sum(c.unread for c in items)

    # -------------------------------------------------------------- lifecycle

    def ensure(self, peer) -> Conversation:
        """Return the conversation for ``peer``, creating it if needed.

        Creating one builds a :class:`Terminal` and registers it with the
        transport scoped to that peer, so this is also the point where a new
        friend starts receiving covert traffic.
        """
        key = str(peer)
        # 整个创建过程持锁：check-then-create 若分两次拿锁，两个线程会为同一
        # peer 各建一个 Terminal，后注册的 sink 覆盖前者，而 _items 里留下的
        # 可能是另一个 —— 会话对象和实际收包的 Terminal 不是同一个。
        with _STATE_LOCK:
            existing = self._items.get(key)
            if existing is not None:
                return existing
            conversation = self._create_locked(key)
            return conversation

    def _create_locked(self, key: str) -> Conversation:
        """Build and register one conversation. Caller holds ``_STATE_LOCK``."""
        terminal = Terminal(key, self.network, psk=self.psk)
        if self.text_channel is not None:
            terminal.use_text_channel(self.text_channel)
        if self._cover_for is not None:
            terminal.take_cover = (lambda p=key: self._cover_for(p))
        terminal.attach(peer=key)
        terminal.subscribe(lambda inbound, p=key: self._on_inbound(p, inbound))
        if self.tasks is not None:
            terminal.admit = self.tasks.inbound_admit
            terminal.release = self.tasks.inbound_done

        conversation = Conversation(key, terminal,
                                    name=self.directory.name_of(key))
        self._items[key] = conversation
        event("client", "conversation_created", peer=key)
        return conversation

    def forget(self, peer) -> None:
        """Drop a conversation and everything in it.

        Used on logout and by tests. The transport keeps its sink until replaced;
        a stale sink that fires later is harmless because the terminal object is
        no longer referenced by the UI.
        """
        with _STATE_LOCK:
            self._items.pop(str(peer), None)

    def clear(self) -> None:
        with _STATE_LOCK:
            self._items.clear()

    # ---------------------------------------------------------------- routing

    def attach(self) -> "Conversations":
        """Start listening for raw messages so stranger contact creates a chat."""
        on_event = getattr(self.network, "on_event", None)
        if on_event is None:
            raise TypeError(
                f"{type(self.network).__name__} 不支持 on_event；"
                "会话层需要它才能在陌生人发消息时建会话"
            )
        on_event(self._on_raw)
        return self

    def attach_images(self, cache) -> None:
        """Give the conversation layer somewhere to download pictures from."""
        self.images = cache

    def attach_self(self, uin: str) -> None:
        """Tell the layer our own QQ number.

        Needed to label un-renderable segments correctly: QQ echoes our own
        messages back, and "对方发送了语音" would be wrong -- and confusing,
        because the bubble sits on the right.
        """
        self.uin = str(uin or "")

    def _on_raw(self, sender: str, payload: dict) -> None:
        """Raw QQ message, before any stego inspection.

        Creating the conversation *here* rather than in the sink is what makes
        first contact work: the transport calls this before it looks up per-peer
        sinks, so by the time the packet is routed, the destination exists.
        """
        try:
            parsed = message_parser.parse(payload, self_uin=getattr(self, "uin", None))
        except Exception as exc:                    # noqa: BLE001 - reader thread
            event("client", "parse_failed", level=logging.WARNING,
                  sender=sender, error=exc)
            return

        conversation = self.ensure(sender)
        if not (parsed.text or parsed.unsupported or parsed.images):
            return

        # 图片要显示在左侧聊天栏里 —— 这是用户明确要求的功能。
        # 下载放后台：读线程是维持握手和密文流动的那根线，不能被 CDN 卡住。
        urls = [ref.locate() for ref in parsed.images]
        urls = [u for u in urls if u]
        cache = getattr(self, "images", None)
        if cache is None or not urls:
            conversation.record_visible(parsed)
            return

        # 先记一条不带图的，界面立刻有内容（文字/占位行），图随后补上。
        entry = conversation.record_visible(parsed)
        cache.prefetch(urls, on_ready=lambda: _fill_images(cache, entry, urls))

    def _on_inbound(self, peer: str, inbound: Inbound) -> None:
        conversation = self.get(peer)
        if conversation is None:
            return
        conversation.record_inbound(inbound)

    # ----------------------------------------------------------------- sending

    def send_text(self, peer, text: str) -> Conversation:
        """Plain, unencrypted text -- what a normal messenger does.

        Kept separate from :meth:`send_covert` because most messages are ordinary
        and should not touch the stego layer at all.
        """
        conversation = self.ensure(peer)
        if not text.strip():
            raise ValueError("消息是空的")
        conversation.terminal.send_packet(conversation.peer, Packet(text=text))
        conversation.record_outbound(text=text)
        return conversation
