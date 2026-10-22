"""One QQ account, wired together: transport, friends, chats, pictures.

This is the seam the UI talks to. It owns the four pieces and the few rules that
span them:

- a friend list fetched from QQ at startup,
- a conversation per friend, created on first contact,
- one picture queue shared by all conversations of this account,
- handshake replies draw a cover from that queue.

Why one queue, not one per friend
---------------------------------
The user points at *a folder of photos they own*, not at "photos for Bob". Any
conversation can take any cover. Per-friend queues would mean pointing at the
same folder once per friend for no benefit, and would strand pictures queued for
a friend that then goes quiet.

Nothing here writes to disk. The friend list, every transcript and the picture
queue live in memory and are gone when the process exits. That is a product
decision, not an oversight -- see ``docs/CLIENT.md`` §5.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..core.errors import InvalidInput
from ..observability import event
from ..terminal.terminal import Packet
from . import messages as message_parser
from .conversations import Conversations
from .directory import Directory
from .queue import PictureQueue


class Account:
    """A logged-in QQ account and everything hanging off it."""

    def __init__(self, network, *, psk: str = "pbx1", text_channel=None,
                 queue: PictureQueue | None = None) -> None:
        #: A started :class:`~markovcryptor.terminal.onebot.OneBotNetwork`.
        self.network = network
        self.directory = Directory()
        self.queue = queue if queue is not None else PictureQueue()
        self.conversations = Conversations(
            network, self.directory,
            psk=psk, text_channel=text_channel,
            cover_for=self._cover_for,
        )
        self._ready = False
        #: This account's own QQ number, once known.
        self.uin = str(getattr(network, "self_id", "") or "")

    # ------------------------------------------------------------------ startup

    def start(self, *, wait: float = 20.0) -> "Account":
        """Wait for the transport, then fetch friends and start routing.

        ``OneBotNetwork.start()`` only spawns a thread; the socket is not up yet
        when it returns. Calling ``login_info()`` immediately therefore fails
        with "还没连上" -- which looks like a broken config but is really just a
        race. So poll until the connection answers, up to ``wait`` seconds.

        Raises whatever the transport raises once the wait is over, because by
        then "cannot connect" is the truth and the caller should say so.
        """
        self._wait_until_connected(wait)
        info = self.network.login_info()
        self.uin = str(info.get("user_id") or self.uin)
        self.directory.refresh(self.network.friend_list(), owner=self.uin)
        self.conversations.attach()
        self._ready = True
        event("client", "account_ready", uin=self.uin, friends=len(self.directory))
        return self

    def _wait_until_connected(self, wait: float) -> None:
        """Block until OneBot answers, or give up after ``wait`` seconds.

        Polls rather than inspecting the transport's internals: "can I ask it
        something" is exactly the property we care about, and it stays correct if
        the transport changes how it connects.
        """
        import time

        deadline = time.monotonic() + max(0.0, wait)
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if self.network.login_info().get("user_id"):
                    return
            except Exception as exc:               # noqa: BLE001 - retry until deadline
                last = exc
            time.sleep(0.4)
        if last is not None:
            raise last
        raise RuntimeError(
            f"等了 {wait:.0f} 秒，OneBot 仍没有回复登录信息")

    @property
    def ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------ covers

    def _cover_for(self, peer: str) -> bytes | None:
        """One of our pictures, for answering that peer's handshake.

        Drawn from the shared queue. Returns ``None`` when the queue is dry, which
        tells the terminal to hold the request until :meth:`retry_pending` -- far
        better than inventing a cover, which is a risk-control red flag.
        """
        item = self.queue.take()
        if item is None:
            event("client", "no_cover_for_reply", level=logging.WARNING,
                  peer=peer, reason="图片队列为空，握手回复被压后")
            return None
        return item.raw

    def retry_handshakes(self) -> int:
        """Retry any handshake left pending for lack of a cover.

        Call after the queue is refilled. Returns how many were answered.
        """
        answered = 0
        for conversation in self.conversations:
            if conversation.terminal.retry_pending():
                answered += 1
        return answered

    # ------------------------------------------------------------------ sending

    def send_text(self, peer, text: str):
        """Plain text, unencrypted -- what a normal messenger does."""
        return self.conversations.send_text(peer, text)

    def send_pictures(self, peer, images: tuple[bytes, ...],
                      text: str | None = None):
        """Send pictures in the clear (no hidden payload).

        Used when the user just wants to send a photo. Kept distinct from the
        covert send so ordinary use never touches the stego layer.
        """
        conversation = self.conversations.ensure(peer)
        conversation.terminal.send_packet(
            conversation.peer, Packet(text=text, images=images))
        conversation.record_outbound(text=text or "", images=images)
        return conversation

    def send_covert(self, peer, text: str,
                    images: tuple[bytes, ...] | None = None):
        """Hide ``text`` in pictures and send. Falls back to the queue if needed.

        The ciphertext rides in the images; the visible text (if any) goes out in
        the clear as a normal messenger would show it. Requires an established
        session -- call :meth:`start_handshake` first.
        """
        conversation = self.conversations.ensure(peer)
        if images is None:
            item = self.queue.take()
            if item is None:
                raise InvalidInput(
                    "没有可用的图片：图片队列为空",
                    hints=("在配置文件里指定一个图片文件夹，或在界面上点「补图」",),
                )
            images = (item.raw,)
        conversation.terminal.send_covert_in_images(conversation.peer, text, images)
        conversation.record_outbound(covert=(text,))
        return conversation

    def start_handshake(self, peer):
        """Begin a key exchange with ``peer``, hiding our key in one picture."""
        conversation = self.conversations.ensure(peer)
        item = self.queue.take()
        if item is None:
            raise InvalidInput(
                "密钥交换需要一张图片，但图片队列是空的",
                hints=("在配置文件里指定一个图片文件夹，或在界面上点「补图」",),
            )
        conversation.terminal.send_handshake(conversation.peer, (item.raw,))
        conversation.record_outbound(text="（已发起密钥交换）")
        return conversation

    # ------------------------------------------------------------------ history

    def load_history(self, peer, count: int = 20) -> list[message_parser.ParsedMessage]:
        """Pull recent messages with ``peer`` from QQ.

        Called when the user opens a conversation, not at startup: prefetching
        thirty friends' histories would be slow and is more data than we need.
        Returns parsed messages newest-last, mirroring QQ's own ordering.
        """
        raw = self.network.friend_history(peer, count=count)
        parsed: list[message_parser.ParsedMessage] = []
        for payload in raw:
            try:
                parsed.append(message_parser.parse(payload))
            except Exception as exc:                # noqa: BLE001
                event("client", "history_parse_failed", level=logging.WARNING,
                      peer=peer, error=exc)
        return parsed

    # ------------------------------------------------------------------ shutdown

    def close(self) -> None:
        """Stop the transport and drop all in-memory state.

        Stops the network too, not just the data. Leaving the OneBot reader
        thread running after "close" is a leak the caller cannot see: the process
        keeps a websocket open and keeps receiving messages into structures that
        were just cleared. Nothing was ever on disk to clean up.
        """
        stop = getattr(self.network, "stop", None)
        if stop is not None:
            try:
                stop()
            except Exception as exc:               # noqa: BLE001 - shutdown path
                event("client", "stop_failed", level=logging.WARNING, error=exc)
        self.conversations.clear()
        self.directory.clear()
        self.queue.clear()
        self._ready = False
