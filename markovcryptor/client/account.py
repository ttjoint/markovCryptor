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
from .images import ImageCache
from .models import ModelLibrary, MultiModelTextChannel
from .queue import PictureQueue


#: ``MAGIC | 'H' | pub(32) | mode`` —— 握手报文固定 38 字节。
#: 从协议实现里量出来的，协议改了这里自动跟着改。
_HANDSHAKE_BYTES = 38


def _picture_capacity(raw: bytes) -> int:
    """Bytes a picture can carry, using the same rules as the terminal.

    GIF and still images have different carriers, and the terminal already knows
    how to measure both -- reuse it instead of guessing, or the UI would promise
    capacity the sender cannot deliver.
    """
    import io

    from PIL import Image

    from .. import gif_stego, image_stego

    if raw[:6] in (b"GIF87a", b"GIF89a"):
        with Image.open(io.BytesIO(raw)) as image:
            return int(gif_stego.capacity(image)["max_payload_bytes"])
    with Image.open(io.BytesIO(raw)) as image:
        return int(image_stego.max_payload_bytes(image))


class Account:
    """A logged-in QQ account and everything hanging off it."""

    def __init__(self, network, *, psk: str = "pbx1", text_channel=None,
                 queue: PictureQueue | None = None,
                 models: ModelLibrary | None = None,
                 images: ImageCache | None = None,
                 tasks=None,
                 peers_path: Path | None = None) -> None:
        #: A started :class:`~markovcryptor.terminal.onebot.OneBotNetwork`.
        self.network = network
        self.directory = Directory()
        self.queue = queue if queue is not None else PictureQueue()
        #: Text-channel models, loaded lazily. ``None`` disables the text
        #: channel entirely, which is the right default for a client that has
        #: not been pointed at a model folder.
        self.models = models
        #: 下载并缓存 QQ 图片，供左侧聊天栏显示。界面上看到的图全在这里，
        #: 有上限、LRU 淘汰，退出即销毁。
        self.images = images if images is not None else ImageCache()
        if text_channel is None and models is not None:
            text_channel = MultiModelTextChannel(models)
        #: 发送/接收任务的调度器。放在 Account 上（而不是 app 层），因为
        #: 接收入闸必须由 terminal 在读线程里问 —— 那条线不经 HTTP。
        from .tasks import TaskManager
        self.tasks = tasks if tasks is not None else TaskManager()
        #: TOFU 指纹册。**这是除用户配置外唯一持久化的东西** ——
        #: TOFU 的全部价值就在"记住上次见到的是谁"，内存里存等于每次重启
        #: 都重新信任，中间人只需等你重启。
        #:
        #: 存的是公钥指纹（公开信息），不含任何消息内容。
        from ..core.identity import KnownPeers
        self.peers = KnownPeers(peers_path)
        self.conversations = Conversations(
            network, self.directory,
            psk=psk, text_channel=text_channel,
            cover_for=self._cover_for,
            tasks=self.tasks,
        )
        self._ready = False
        #: True/False when the bridge told us, None when it does not support
        #: ``get_status``. The UI shows "未知" for None instead of guessing.
        self.online: bool | None = None
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
        # 本机昵称和头像：界面顶部要显示真实身份，而不是"本机"两个字。
        self.directory.set_self(self.uin,
                                nickname=str(info.get("nickname") or ""))
        self.directory.refresh(self.network.friend_list(), owner=self.uin)
        self.conversations.attach()
        self.conversations.attach_images(self.images)
        # 告诉会话层本机账号，否则未渲染的消息会把自己的语音说成"对方发的"。
        self.conversations.attach_self(self.uin)
        self._load_models()
        self._refresh_online()
        self._ready = True
        event("client", "account_ready", uin=self.uin, friends=len(self.directory))
        return self

    def _refresh_online(self) -> None:
        """Ask whether we are online. Failure means "unknown", not "offline".

        ``get_status`` is not implemented by every bridge, and a missing answer
        must not make the UI claim we are offline -- that would be a lie the
        user cannot act on.
        """
        try:
            status = self.network.online_status()
        except Exception as exc:                    # noqa: BLE001 - optional
            event("client", "status_unavailable", level=logging.DEBUG, error=exc)
            self.online = None
            return
        self.online = bool(status.get("online", True))

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

    def _load_models(self) -> None:
        """Load one model now, the rest in the background.

        One model is enough to *send*, and costs ~0.1-0.5s. Identification on the
        receive side needs every model, so the rest load in the background --
        the window appears immediately, and a message arriving in the first
        second is simply treated as ordinary text, which is the same thing that
        happens when the peer uses a model we do not have.
        """
        if self.models is None:
            return
        if self.models.scan() == 0:
            event("client", "no_models", level=logging.INFO,
                  folder=str(self.models.folder))
            return
        first = self.models.load_first()
        if first is not None:
            event("client", "model_ready", model_name=first.name,
                  seconds=round(first.load_seconds, 3))
        self.models.load_in_background()

    def covert_capacity(self, *, text_chars: int = 0,
                        need_handshake: bool = False) -> dict:
        """Can the queued pictures carry what we want to hide?

        The UI asks this before enabling the send button, so "图片数量不足" is
        shown *before* the user types a paragraph and hits enter -- not after.

        ``need_handshake`` is 38 bytes. A body message needs whatever the sealed
        envelope weighs, which depends on the text length; we estimate with a
        margin rather than sealing here (sealing needs a live session).
        """
        if need_handshake:
            needed = _HANDSHAKE_BYTES
        else:
            # 明文信封：帧头 + nonce + 密文 + 校验。中文按 UTF-8 最多 3 字节，
            # 再留 96 字节余量（帧头、校验、填充）。
            needed = max(_HANDSHAKE_BYTES, text_chars * 3 + 96)

        items = self.queue.preview()
        total = 0
        usable = 0
        for item in items:
            try:
                capacity = _picture_capacity(item.raw)
            except Exception:                       # noqa: BLE001 - user file
                continue
            total += capacity
            if capacity >= _HANDSHAKE_BYTES:
                usable += 1

        return {
            "need_bytes": needed,
            "queued": len(items),
            "usable": usable,
            "capacity": total,
            "enough": total >= needed and usable > 0,
        }

    def send_covert_text(self, peer, text: str):
        """Hide ``text`` in generated Chinese prose instead of a picture.

        Costs no picture from the queue, which is why the UI offers it as a
        toggle: a user with one photo left may prefer to spend the text channel
        on a short message and keep the photo for a handshake.
        """
        conversation = self.conversations.ensure(peer)
        if not self.text_channel_usable:
            raise InvalidInput(
                "没有可用的文本模型",
                hints=("在设置里指定模型目录，或训练/导入一个模型",),
            )
        covertext = conversation.terminal.send_covert_text(
            conversation.peer, text)
        # 线上真正传的是这段中文，所以它出现在明文栏；秘密只进密文栏。
        conversation.record_outbound(text=covertext, covert=(text,))
        return conversation

    @property
    def text_channel_usable(self) -> bool:
        """Whether covert *text* sending is possible right now."""
        return self.models is not None and self.models.usable

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

    def accept_handshake(self, peer: str) -> bool:
        """用户点了「接受」：回应对方挂起的握手请求。

        收方**不会自动回复** —— 自动回复在 QQ 侧看就是机器行为，用户也失去
        核对指纹的机会，而且客户端挂着时任何人都能自动建立连接。

        Returns True when the reply actually went out. False means it is still
        held (no usable picture, or none big enough) -- the caller tells the
        user instead of pretending it worked.
        """
        conversation = self.conversations.get(peer)
        if conversation is None:
            return False
        pending_public = conversation.terminal.pending_request
        sent = conversation.terminal.respond_handshake()
        if sent:
            # 用户点了「接受」才记录指纹 —— 忽略的人不该被当成"已经信任过"。
            if pending_public is not None and self.peers is not None:
                status, shown = self.peers.check(peer, pending_public)
                event("security", "handshake_accepted", peer=peer,
                      fingerprint=shown, first_time=(status == "first"))
            conversation.record_outbound(text="（已回应密钥交换）")
        return sent

    def pending_handshake(self, peer: str) -> bytes | None:
        """对方挂起的请求（对端公钥），没有则 None。"""
        conversation = self.conversations.get(peer)
        if conversation is None:
            return None
        return conversation.terminal.pending_request

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
        # 图也要留在聊天栏里：用户发出去的就是这张照片，看不到会以为没发成功。
        conversation.record_outbound(text="", images=images, covert=(text,))
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
        conversation.record_outbound(text="（已发起密钥交换）",
                                     images=(item.raw,))
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
                # 传 self_uin：历史里两边都有，要能分清谁发的。
                parsed.append(message_parser.parse(
                    payload, self_uin=self.uin))
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
