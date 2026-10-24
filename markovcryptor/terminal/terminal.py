"""The terminal: send packets, inspect whatever arrives, route the results.

This is where the behaviour the whole feature exists for lives:

1. an incoming packet is **inspected**, not displayed;
2. for each image and for the text, decide whether it is genuinely stego;
3. a handshake found in an image is **answered automatically**;
4. a body message found anywhere is decrypted and goes to the **covert**
   window; the host content goes to the **chat** window regardless;
5. anything unrecognised, undecryptable or malformed is **ignored** -- host
   content still shows, the covert window does not move, the reason is logged.

The guiding rule, stated once: *if it cannot be proven to be ours, it is not
ours*. Every "is this stego?" decision ends in an AES-GCM tag check, so ordinary
messages can never leak into the covert window.

Failure to prove is not an error the user must handle; it is the normal case for
every message the peer sends in the clear. So this module never raises for
incoming data -- it logs and moves on. Only *outgoing* calls raise, because
those are the caller's own mistakes and should be visible.
"""

from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image

from .. import gif_stego, image_stego
from ..core.errors import (
    BadKey,
    CapacityExceeded,
    CarrierDamaged,
    InvalidInput,
    NoHiddenFrame,
    StegoError,
    UnsupportedCarrier,
)
from ..observability import event
from .protocol import (
    HANDSHAKE_MAC_BYTES,
    MAGIC,
    HEADER_BYTES,
    Kind,
    Mode,
    build_handshake,
    build_slice,
    parse_handshake,
    parse_slice,
    peek_kind,
    PUBLIC_KEY_BYTES,
    wrap,
)
from .session import Session, State
from .transport import Network

@dataclass(frozen=True)
class Packet:
    """One message: 0..n images plus 0..m bytes of text."""

    text: str | None = None
    images: tuple[bytes, ...] = ()

    def is_empty(self) -> bool:
        return not self.images and not (self.text or "").strip()


@dataclass(frozen=True)
class Inbound:
    """One received packet after inspection -- what the UI renders.

    ``chat_*`` fields always hold the host content (what a normal messenger would
    show). ``covert_*`` hold what was recovered from steganography, and are empty
    for ordinary traffic. Keeping them in one immutable record makes it hard to
    accidentally display a decrypted message in the chat window.
    """

    sender: str
    #: Text as it arrived (plaintext), or None if the packet carried none.
    chat_text: str | None = None
    #: Decoded images, ready to display.
    chat_images: tuple[Image.Image, ...] = ()
    #: Decrypted covert messages, in arrival order.
    covert: tuple[str, ...] = ()
    #: One line per inspection decision that did not lead to a covert message.
    notes: tuple[str, ...] = ()
    #: True if this packet advanced or started the handshake.
    handshake: str | None = None

    @property
    def has_covert(self) -> bool:
        return bool(self.covert)


@dataclass
class _Inspected:
    """Result of looking at a single carrier."""

    covert: str | None = None
    note: str | None = None
    handshake: str | None = None
    #: Set when this carrier holds one slice of a split body: ``(index, count)``.
    #: The bytes themselves are in ``slice_bytes``; the pair belongs together so a
    #: misplaced slice can never be joined as if it were in position.
    piece: tuple[int, int] | None = None
    slice_bytes: bytes = b""
    #: True when ``note`` merely says "nothing here" -- routine, log-only.
    unremarkable: bool = False


class Terminal:
    """A named endpoint. Owns one session and one outbox of pending handshakes."""

    def __init__(self, terminal_id: str, network: Network, *,
                 psk: str = "pbx1", on_inbound: Callable[[Inbound], None] | None = None):
        self.id = terminal_id
        self.network = network
        #: Pre-shared key for the *image stego layer* (the MKX1 key), not the
        #: session. It authenticates the carrier; the session authenticates the
        #: content. Both are needed: without a psk anyone could write a frame.
        self.psk = image_stego.normalise_key(psk)
        self.session = Session()
        self._on_inbound = on_inbound
        #: Called with (sender, raw_packet) before inspection, exactly once per
        #: deliver(). Together with ``inbox.append`` (also once per deliver) that
        #: gives history lists a stable 1:1 index correspondence, which the web
        #: layer relies on to find media bytes. UI code should prefer
        #: ``on_inbound`` (decoded), not this.
        self.on_packet: Callable[[str, bytes], None] | None = None
        self.inbox: list[Inbound] = []
        #: After a successful exchange, remember who to reply to.
        self.peer_id: str | None = None
        #: Optional text carrier (Chinese channel). See :meth:`use_text_channel`.
        self.text_channel = None
        #: 接收入闸：返回 False 就跳过这张图的解码。
        #:
        #: 解码一张 1600×1600 要 3.3 秒，而 deliver() 跑在读线程上 —— 那是
        #: 维持握手和密文流动的那根线。对方连发十张大图就能把客户端堵死。
        #: 这里不自己实现限流（terminal 层不该知道队列策略），而是让上层
        #: 注入一个闸门，本层只负责"问了才解码"。
        self.admit: Callable[[], bool] | None = None
        #: 每处理完一张图调一次，用于归还闸门计数。
        self.release: Callable[[], None] | None = None
        #: How to get a cover for an automatic handshake reply. Injected by the
        #: caller (the web layer takes it from that client's own picture queue).
        #: Returns ``None`` when this side has no usable picture -- then the
        #: reply is held until :meth:`retry_pending` is called.
        self.take_cover: Callable[[], bytes | None] | None = None
        #: A REQUEST that arrived while this side had no picture to answer with.
        self.pending_request: bytes | None = None
        #: 这个挂起请求是什么时候收到的（``time.monotonic()``）。
        #: 界面要显示"N 秒前的请求" —— 对方反复发时，用户需要知道
        #: 自己点的是最新那个还是半小时前的。
        self.pending_since: float = 0.0
        #: 最近见过的对端公钥（环形，只留几十个）。
        #:
        #: **重放防护**：X25519 公钥每次握手都是随机的，所以同一个公钥
        #: 再次出现几乎只可能是重放。攻击者把一张合法的握手图反复丢过来，
        #: 用户会被"一堆相同的请求"淹没，很容易误点其中一个。
        self._seen_requests: list[bytes] = []

    # ------------------------------------------------------------------ config

    @property
    def established(self) -> bool:
        return self.session.established

    @property
    def state(self) -> State:
        return self.session.state

    def subscribe(self, callback: Callable[[Inbound], None]) -> None:
        self._on_inbound = callback

    def attach(self, address: str | None = None, *, peer: str | None = None) -> "Terminal":
        """Register on the network so incoming packets reach :meth:`deliver`.

        Returns ``self`` so a terminal can be created and attached in one
        expression. The sink signature is ``(sender, payload)`` because only the
        network knows who sent a packet.

        ``peer`` scopes this terminal to one remote address, for transports that
        carry many conversations over one connection (a single QQ account with
        several friends). It is forwarded only when the network understands it --
        the loopback transport has one sink per address already and would reject
        the keyword.
        """
        network = self.network
        register = getattr(network, "register", None)
        if register is None:
            raise InvalidInput(
                f"{type(network).__name__} 不支持注册；请手动把包交给 deliver(sender, packet)"
            )
        sink = lambda sender, data: self.deliver(sender, data)  # noqa: E731
        if peer is not None:
            try:
                register(address or self.id, sink, peer=peer)
                return self
            except TypeError:
                # 传输层不认识 peer（单地址直连），退回普通注册。
                pass
        register(address or self.id, sink)
        return self

    # ---------------------------------------------------------------- sending

    def send_packet(self, target: str, packet: Packet) -> None:
        """Send text and/or images in the clear, as one packet."""
        if packet.is_empty():
            raise InvalidInput("消息是空的：至少要有文本或一张图片")
        self.network.send(self.id, target, _encode_packet(packet))

    def send_handshake(self, target: str, images: tuple[bytes, ...],
                       *, mode: Mode | None = None) -> None:
        """Start (or answer) a handshake, hiding the key in the user's images.

        The requirement is explicit that a handshake message *must* carry at
        least one image, because that is the only carrier the exchange travels
        in. We therefore fail loudly -- naming the required capacity -- rather
        than silently downgrading to a text carrier or inventing a cover.

        The key goes into the *first* image only. A handshake is a 38-byte
        control message, so a multi-image handshake is refused rather than
        padded: splitting it would mean writing padding into the other pictures
        just to even out the sizes.
        """
        if not images:
            raise InvalidInput(
                "密钥交换必须至少附带一张图片：X25519 公钥藏在图片里，文本载体装不下",
                hints=("在待发送队列里至少勾选一张图片",),
            )
        if len(images) > 1:
            raise InvalidInput(
                f"密钥交换一次只用一张图片，收到 {len(images)} 张",
                hints=("握手是 38 字节的控制报文，只勾选一张图片即可",),
            )
        if mode is Mode.RESPONSE:
            public, mode = self.session.answer()
        else:
            public, mode = self.session.begin()

        envelope = build_handshake(public, mode, mac=self._handshake_mac(public))
        stego = self._embed_into_all((envelope,), images, what="密钥交换报文")
        self.peer_id = target
        event("terminal", "handshake_out", id=self.id, peer=target,
              mode=mode.value)
        self.send_packet(target, Packet(images=stego))

    def send_covert_text(self, target: str, text: str) -> str:
        """Encrypt ``text`` and hide it in the text channel.

        Returns the **covertext** that actually crossed the wire. That string is
        ordinary-looking Chinese prose, not the secret -- it is what an observer
        on the network would see, so it belongs in the chat window as the visible
        message. The secret only ever appears in the covert window.

        The text carrier is the Chinese channel, which needs a trained model.
        Rather than import a model here (and make the whole terminal depend on
        user data), the encoder is injected with :meth:`use_text_channel`. A
        terminal without one can still do everything else -- text covert sending
        is simply unavailable, and says so.
        """
        if not self.session.established:
            raise InvalidInput(
                f"会话{self.session.state.label}，不能发送密文",
                hints=("先做密钥交换：从图片队列取一张图发起握手",),
            )
        if not text:
            raise InvalidInput("密文内容为空",
                           hints=("在密文输入框里写下要隐藏的内容",))
        if self.text_channel is None:
            raise InvalidInput(
                "文本隐写信道不可用：需要先训练并加载中文信道模型",
                hints=("用 markovcryptor train 训练模型，或改用密文图片发送",),
            )
        envelope = self.session.seal(Kind.BODY_TEXT, text.encode("utf-8"))
        covertext, _stats = self.text_channel.embed_text(envelope)
        self.network.send(self.id, target, _encode_packet(Packet(text=covertext)))
        event("terminal", "covert_text_out", id=self.id, peer=target,
              bytes=len(envelope))
        return covertext

    def use_text_channel(self, channel) -> None:
        """Attach the text carrier. ``channel`` must have ``embed_text``/``extract_text``."""
        self.text_channel = channel

    def images_needed(self, text: str, images: tuple[bytes, ...]) -> int:
        """How many of ``images`` this message actually needs, front to back.

        One short sentence fits in one picture; sending the other eleven the user
        lined up would be pure waste -- they stay in the queue for the next
        message instead. The count comes from the real envelope size against each
        picture's real capacity, so it agrees with what ``embed`` will accept.

        Raises the same refusals as :meth:`send_covert_in_images` (no session, no
        text, no pictures) because the caller is about to send anyway.
        """
        if not self.session.established:
            raise InvalidInput(
                f"会话{self.session.state.label}，不能发送密文",
                hints=("先做密钥交换：从图片队列取一张图发起握手",),
            )
        if not text:
            raise InvalidInput("密文内容为空",
                           hints=("在密文输入框里写下要隐藏的内容",))
        if not images:
            raise InvalidInput(
                "图片混淆发送需要至少一张图片：密文只能藏在图片里，不会自动生成",
                hints=("在待发送预览栏里准备图片", "或改用文本混淆发送（不消耗图片）"),
            )
        # 对称密钥下的信封长度与内容无关（GCM 是流式认证加密），所以先用
        # 真实载荷量一次，再看第几张刚好装得下。
        envelope = self.session.seal(Kind.BODY_IMAGE, text.encode("utf-8"))
        used = 0
        for raw in images:
            used += 1
            # 分给 n 张后每张的份额；最后一张可能略小，用最大值判够不够。
            share = -(-len(envelope) // used)
            if self._capacity_of(raw) >= share:
                return used
        # 全用上也不够：交给 send 去逐张说明原因，比在这里编一个数字好。
        return len(images)

    def _capacity_of(self, raw: bytes) -> int:
        """Byte capacity of one picture, whatever its format.

        GIF carries the payload in its first frame and ``gif_stego`` may scale it
        up, so ask that module for the real number instead of guessing from the
        file's nominal size.
        """
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if getattr(image, "n_frames", 1) > 1 or image.format == "GIF":
                    return gif_stego.capacity(image)["max_payload_bytes"]
                return image_stego.max_payload_bytes(*image.size)
        except Exception:                      # noqa: BLE001 - not a picture
            return 0

    def send_covert_in_images(self, target: str, text: str,
                              images: tuple[bytes, ...],
                              *, chat_text: str | None = None) -> None:
        """Encrypt ``text`` and hide it in ``images``, one slice per image.

        The payload is cut into ``len(images)`` consecutive pieces and picture
        ``i`` carries piece ``i``, so the user's sequence is preserved and every
        picture they chose is actually used. The peer reassembles by index.

        A single picture gets a plain ``BODY_IMAGE`` envelope rather than a
        one-of-one slice: a slice header would be two bytes of pure overhead on
        the common case, and the receiver handles both.

        The *visible* text (``chat_text``) still goes out as plaintext: the user
        asked to keep the plaintext side unchanged. Only the images are stego.
        """
        if not self.session.established:
            raise InvalidInput(
                f"会话{self.session.state.label}，不能发送密文",
                hints=("先做密钥交换：从图片队列取一张图发起握手",),
            )
        if not text:
            raise InvalidInput("密文内容为空",
                           hints=("在密文输入框里写下要隐藏的内容",))
        if not images:
            raise InvalidInput(
                "图片混淆发送需要至少一张图片：密文只能藏在图片里，不会自动生成",
                hints=("在待发送预览栏里准备图片", "或改用文本混淆发送（不消耗图片）"),
            )
        envelope = self.session.seal(Kind.BODY_IMAGE, text.encode("utf-8"))
        pieces = (envelope,) if len(images) == 1 else split_payload(
            envelope, len(images),
            capacities=[self._cover_capacity(raw) for raw in images])
        stego = self._embed_into_all(pieces, images, what="正文密文")
        event("terminal", "covert_out", id=self.id, peer=target,
              images=len(stego), bytes=len(envelope))
        self.send_packet(target, Packet(text=chat_text, images=stego))

    # --------------------------------------------------------------- embedding

    def _cover_capacity(self, raw: bytes) -> int:
        """How many payload bytes one cover can carry (after clamping).

        Returns 0 when the bytes are not a picture we can read -- the caller
        then leaves that slice unpadded rather than guessing a size.
        """
        try:
            image = _try_open(raw)
            if image is None:
                return 0
            if getattr(image, "n_frames", 1) > 1 or image.format == "GIF":
                return int(gif_stego.capacity(image)["max_payload_bytes"])
            return image_stego.max_payload_bytes(*image.size)
        except Exception:                          # noqa: BLE001 - optional
            return 0

    def _embed_into_all(self, envelopes: tuple[bytes | None, ...],
                        images: tuple[bytes, ...],
                        *, what: str) -> tuple[bytes, ...]:
        """Hide one envelope per image, in the order given.

        Three rules, all of them user-visible requirements:

        1. **No substitution.** Picture ``i`` carries ``envelopes[i]`` or the
           send fails. Never move an envelope to a roomier picture and let the
           original ride along as filler -- the sequence is the user's.
        2. **No leftover plaintext.** A picture with nothing hidden in it would
           be the one thing an observer could separate out, so we never send
           one. Callers pass ``None`` only for a picture that is *already*
           stego (the automatic handshake response hands its own image back).
        3. **No invention.** If there are no images we refuse. Fabricating a
           cover would put a picture on the wire that the user never chose.
        """
        if not images:
            raise InvalidInput(
                f"{what}需要至少一张图片：隐写必须藏在你上传的图里，不会自动生成",
                hints=("在待发送队列里至少勾选一张图片",),
            )
        if len(envelopes) != len(images):
            raise ValueError(
                f"内部错误：{len(envelopes)} 个信封对 {len(images)} 张图"
            )
        stego: list[bytes] = []
        reasons: list[str] = []
        for index, (envelope, raw) in enumerate(zip(envelopes, images)):
            if envelope is None:
                stego.append(raw)
                continue
            try:
                stego.append(self._embed_bytes(envelope, raw))
            except (CapacityExceeded, UnsupportedCarrier, InvalidInput) as exc:
                reasons.append(f"第 {index + 1} 张：{exc}")
        if reasons:
            total = sum(len(e) for e in envelopes if e is not None)
            raise CapacityExceeded(
                f"{what}装不进这 {len(images)} 张图片里（共 {total} 字节，逐张分摊）",
                reasons=tuple(reasons),
                hints=(
                    "每张图只装自己那一份；只要有一张装不下，整条消息就不发",
                    "少勾几张图（分母变小）或多勾几张大图",
                    "容量随图面积增长：160×160 约 162 字节，256×256 约 441 字节",
                ),
            )
        return tuple(stego)

    def _embed_bytes(self, envelope: bytes, raw: bytes) -> bytes:
        payload = wrap(envelope)
        is_gif = raw[:6] in (b"GIF87a", b"GIF89a")
        if is_gif:
            image = Image.open(io.BytesIO(raw))
            image.seek(0)
            data, _report = gif_stego.embed_gif(image, payload, self.psk)
            return data
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        stego, _report = image_stego.embed(image, payload, self.psk)
        buffer = io.BytesIO()
        stego.save(buffer, "PNG")
        return buffer.getvalue()

    # --------------------------------------------------------------- receiving

    def deliver(self, sender: str, packet: bytes) -> Inbound:
        """Inspect and route one incoming packet. Never raises for bad content.

        ``sender`` comes from the transport (it is the only party that knows),
        and is remembered so an automatic handshake response goes back the right
        way.
        """
        if self.on_packet is not None:
            self.on_packet(sender, packet)
        self.peer_id = sender
        text, raws = _decode_packet(packet)
        covert: list[str] = []
        notes: list[str] = []
        handshake: str | None = None
        shown: list[Image.Image] = []
        #: 正文分片：同一包内的图片各自携带一段，收齐后按序号拼接。
        #: 每片自带 (index, count)，所以丢哪一张、漏哪一张都能说清楚。
        pieces: dict[int, bytes] = {}
        want = 0

        for index, raw in enumerate(raws):
            # 闸门：积压太多就直接跳过这张（不解码、不显示），由上层提示用户。
            if self.admit is not None and not self.admit():
                event("terminal", "image_dropped", level=logging.WARNING,
                      id=self.id, index=index + 1, reason="inbound backlog")
                continue
            try:
                result, display = self._inspect_image(raw)
            except Exception as exc:            # noqa: BLE001 - never surface carrier bugs
                # 绝不把 reedsolo / PIL 的原始报错甩到聊天窗口里。
                # 实际踩过：对方发了个收藏的 GIF 表情包，界面显示
                # "step=96.0: Y: 引导头解码失败…Too many (or few) errors
                # found by Chien Search…"。用户看到的是我们内部的纠错算法
                # 细节，既看不懂、又泄露了实现。
                event("terminal", "image_failed", level=logging.DEBUG,
                      id=self.id, index=index + 1, error=exc)

                # 要区分两件事，它们的处理方式相反：
                #
                # 1. **图能正常打开**（表情包、截图、动图）—— 只是里面没有
                #    我们的隐藏帧。这是绝大多数情况，也是完全正常的：
                #    别人发图不是来跟我们通信的。静默，照常显示。
                # 2. **图根本打不开**（数据损坏、截断、不是图片）—— 这值得
                #    告诉用户，否则一张坏图会无声无息地消失。
                opened = _try_open(raw)
                if opened is not None:
                    shown.append(opened)
                else:
                    notes.append(
                        f"图片 {index + 1}：无法读取（文件可能已损坏）")
                if self.release is not None:
                    self.release()
                continue
            if display is not None:
                shown.append(display)
            if result.covert is not None:
                covert.append(result.covert)
            if result.piece is not None:
                position, want = result.piece
                pieces[position] = result.slice_bytes
            if result.note:
                # "No hidden frame" is the normal case for an ordinary photo, so
                # it goes to the log only. Notes are reserved for things the user
                # may actually want to know (an envelope in the wrong carrier, a
                # corrupt frame), otherwise every picture would raise an alarm.
                if result.unremarkable:
                    event("terminal", "image_plain", level=logging.DEBUG,
                          id=self.id, index=index + 1, detail=result.note)
                else:
                    notes.append(f"图片 {index + 1}：{result.note}")
            if result.handshake:
                handshake = result.handshake
            # 无论成败都归还闸门名额，否则一次解码异常就永久占掉一个槽位。
            if self.release is not None:
                self.release()

        if pieces:
            missing = [i for i in range(want) if i not in pieces]
            if missing:
                # 缺片不猜：拼出半截密文只会得到认证失败，不如直接说缺第几片。
                notes.append(
                    f"正文分片不齐：缺第 {', '.join(str(i + 1) for i in missing)} 片"
                    f"（共应 {want} 片），已丢弃"
                )
                event("terminal", "covert_incomplete", level=logging.WARNING,
                      id=self.id, missing=missing, want=want)
            else:
                joined = b"".join(pieces[i] for i in range(want))
                message = self._decrypt(Kind.BODY_IMAGE, joined)
                if message:
                    covert.append(message)

        if text is not None:
            result = self._inspect_text(text)
            if result.covert:
                covert.append(result.covert)
            if result.note:
                if result.unremarkable:
                    event("terminal", "text_plain", level=logging.DEBUG,
                          id=self.id, detail=result.note)
                else:
                    notes.append(f"文本：{result.note}")

        inbound = Inbound(
            sender=sender,
            chat_text=text,
            chat_images=tuple(shown),
            covert=tuple(covert),
            notes=tuple(notes),
            handshake=handshake,
        )
        self.inbox.append(inbound)
        if self._on_inbound is not None:
            self._on_inbound(inbound)
        return inbound

    # -------------------------------------------------------------- inspection

    def _inspect_image(self, raw: bytes):
        """Try to prove ``raw`` is a stego image. Returns (result, display_image).

        The host picture is decoded **whatever** the stego verdict is: an image
        with no hidden frame is still a picture the user must see in the chat
        window. Decoding it on the failure path too is what makes "stego is
        ignored, host shows normally" actually true.
        """
        display = _try_open(raw)
        try:
            payload = self._extract_bytes(raw)
        except (NoHiddenFrame, CarrierDamaged, gif_stego.GifStegoError) as exc:
            # A photo that was never stego and a frame damaged past recovery are
            # indistinguishable at the ECC layer (a wrong key looks exactly like
            # a mangled lead), and the first case is by far the common one. So
            # the note states the fact without alarming: the reason text keeps
            # both possibilities, per the project's error-reporting rule.
            return _Inspected(note=f"未读到隐写帧——{exc}", unremarkable=True), display

        try:
            kind = peek_kind(payload)
        except NoHiddenFrame as exc:
            return _Inspected(note=f"载荷不是 PBX1 信封——{exc}"), display

        if kind is Kind.HANDSHAKE:
            # 握手包是协议控制报文，不是用户内容：它的图不该作为"聊天图片"
            # 出现在窗口里。之前把 host 图照常显示，于是"我发的请求图"和
            # "对方用同一张图回发的响应"在聊天记录里成了两条一样的图，
            # 看起来像重复发送。这里返回 display=None，只留一行系统提示。
            return self._on_handshake(payload), None
        if kind is Kind.BODY_TEXT:
            # A text body inside an image means the sender's carrier choice was
            # inconsistent. Not fatal, but worth saying rather than guessing.
            return _Inspected(note="信封类型与载体不符：文本正文出现在图片里"), display
        if kind in (Kind.BODY_SLICE, Kind.BODY_SLICE_PADDED):
            index, count, chunk = parse_slice(payload)
            return _Inspected(piece=(index, count), slice_bytes=chunk), display
        # A body image holds the whole message in one picture; decrypt it now.
        return _Inspected(covert=self._decrypt(kind, payload)), display

    def _inspect_text(self, text: str) -> _Inspected:
        """Text may only carry ``BODY_TEXT``. Everything else is not stego.

        Two carriers can put an envelope in a text field: the Chinese channel
        (prose that *is* the cover) and a hex dump. The Chinese channel is tried
        first when it exists because it is the one this terminal sends with; a
        text that decodes to neither is ordinary chat and stays quiet.
        """
        envelope = self._envelope_from_text(text)
        if envelope is None:
            return _Inspected(note="不是 PBX1 信封", unremarkable=True)
        try:
            kind = peek_kind(envelope)
        except NoHiddenFrame:
            return _Inspected()
        if kind is not Kind.BODY_TEXT:
            return _Inspected(note=f"文本里出现 {kind.name} 信封，类型与载体不符")
        message = self._decrypt(kind, envelope)
        return _Inspected(covert=message) if message else _Inspected()

    def _envelope_from_text(self, text: str) -> bytes | None:
        """Pull a PBX1 envelope out of a text field, or None if there is none."""
        if self.text_channel is not None:
            try:
                envelope = self.text_channel.extract_text(text)
            except StegoError:
                # 不是隐写、或不是这块模型的隐写：都只是普通文本。
                envelope = b""
            if envelope:
                return envelope
        return _unhex_or_none(text)

    def _handshake_mac(self, public: bytes) -> bytes:
        """Our own MAC over ``public``, bound to this psk and this role."""
        from ..core.identity import build_handshake_payload
        payload = build_handshake_payload(public, public, self.psk)
        return payload[PUBLIC_KEY_BYTES:]

    def _verify_handshake_mac(self, public: bytes, mac: bytes) -> None:
        """Raise ``ValueError`` when the frame is not from someone with our psk."""
        if not mac:
            raise ValueError("握手帧未携带校验码（对端版本过旧）")
        from ..core.identity import build_handshake_payload
        expected = build_handshake_payload(public, public, self.psk)[PUBLIC_KEY_BYTES:]
        import hmac as _hmac
        if not _hmac.compare_digest(mac, expected):
            raise ValueError("握手帧校验失败：对方口令与你不同，或帧被篡改")

    def _on_handshake(self, payload: bytes) -> _Inspected:
        peer_public, mac, mode = parse_handshake(payload)
        # **先验 MAC，再碰会话状态。** 未通过验证的公钥绝不能进 absorb()——
        # 那等于让任何知道协议格式的人往会话里塞一个密钥。
        try:
            self._verify_handshake_mac(peer_public, mac)
        except ValueError as exc:
            event("terminal", "handshake_mac_rejected", level=logging.WARNING,
                  id=self.id, reason=str(exc), has_mac=bool(mac))
            return _Inspected(
                note=f"握手被拒：{exc}",
                handshake="握手校验失败（口令不一致或被篡改）",
            )
        was = self.session.state
        completed = self.session.absorb(peer_public, mode)
        summary = f"握手 {mode.name}（{was.label} → {self.session.state.label}）"
        event("terminal", "handshake_in", id=self.id, mode=mode.value,
              state=self.session.state.value)

        if mode is Mode.REQUEST:
            # **不自动回复。** 只挂起，等用户在界面上点「接受」。
            #
            # 自动回复的代价：从收到到发出图可能只有 0.2 秒，QQ 侧看就是
            # 机器行为；用户没机会核对指纹；客户端挂着时任何人都能自动把
            # 连接建立起来。这三条都不能接受。
            #
            # 已建立的会话收到 REQUEST 是**重握手**：旧密钥继续可用，
            # 所以"等待用户接受"不意味着通道断了。
            was_established = was is State.ESTABLISHED

            # **同一个公钥只认一次。** 对方在用户还没回应时反复发请求，
            # 或者有人重放旧图，都会走到这里。X25519 公钥是随机的，重复
            # 出现几乎只可能是重放 —— 直接忽略，不刷新待确认状态。
            if peer_public in self._seen_requests:
                event("terminal", "handshake_replay_ignored", level=logging.INFO,
                      id=self.id)
                return _Inspected(
                    note="重复的握手请求（已忽略）",
                    handshake=summary + "，重复请求已忽略",
                )
            self._remember_request(peer_public)

            # 只保留**最新**一个请求：旧的被覆盖。
            # 不排队等着一一回复 —— 一次性建立一堆连接没有任何必要，
            # 而且用户点"接受"时根本分不清在回哪个。
            if self.pending_request is not None                     and self.pending_request != peer_public:
                event("terminal", "handshake_superseded", id=self.id)
            self.pending_request = peer_public
            self.pending_since = time.monotonic()
            event("terminal", "handshake_awaiting_user", id=self.id,
                  established=was_established)
            suffix = "，等待你确认" + ("（旧密钥仍可用）" if was_established else "")
            return _Inspected(handshake=summary + suffix)
        if completed:
            return _Inspected(handshake=summary + "，会话可用")
        return _Inspected(handshake=summary)

    def _remember_request(self, public: bytes) -> None:
        """Remember a seen request's public key, keeping the list small."""
        self._seen_requests.append(public)
        if len(self._seen_requests) > 64:
            del self._seen_requests[:-64]

    @property
    def pending_age(self) -> float:
        """How many seconds ago the held request arrived (0 when none)."""
        if self.pending_request is None or not self.pending_since:
            return 0.0
        return max(0.0, time.monotonic() - self.pending_since)

    def respond_handshake(self) -> bool:
        """Answer a held request **because the user asked to**.

        Returns True when a reply went out. False means it is still held
        (no cover picture, or it would not fit) -- the caller shows that to
        the user instead of silently pretending something happened.
        """
        peer_public = self.pending_request
        if peer_public is None:
            return False
        target = self.peer_id
        if target is None:
            return False

        cover = self.take_cover() if self.take_cover is not None else None
        if cover is None:
            event("terminal", "handshake_held", level=logging.INFO,
                  id=self.id, peer=target, reason="no cover picture queued")
            return False

        public, mode = self.session.answer()
        envelope = build_handshake(public, mode, mac=self._handshake_mac(public))
        try:
            stego = self._embed_bytes(envelope, cover)
        except StegoError as exc:
            # 自己的图太小装不下握手帧。留着请求，让用户换张大图重试；
            # 现在提交就等于为一个没发出去的回复安装密钥。
            self.session.discard_answer()
            event("terminal", "handshake_cover_failed", level=logging.WARNING,
                  id=self.id, peer=target, error=exc)
            return False

        self.pending_request = None
        self.pending_since = 0.0
        event("terminal", "handshake_user_response", id=self.id, peer=target)
        self.network.send(self.id, target, _encode_packet(Packet(images=(stego,))))
        self.session.commit_answer()
        return True

    def retry_pending(self) -> bool:
        """Answer a held handshake request now that a cover may be available.

        Returns ``True`` if a reply went out. Safe to call at any time: it does
        nothing when there is no pending request.
        """
        if self.pending_request is None:
            return False
        before = self.session.state
        sent = self.respond_handshake()
        if sent:
            event("terminal", "handshake_held_released", id=self.id,
                  state_before=before.value, state_after=self.session.state.value)
        return sent

    def _decrypt(self, kind: Kind, payload: bytes) -> str | None:
        try:
            plaintext = self.session.open(kind, payload)
        except BadKey as exc:
            event("terminal", "covert_undecryptable", id=self.id,
                  kind=kind.name, reason=exc)
            return None
        except StegoError as exc:
            event("terminal", "covert_malformed", id=self.id,
                  kind=kind.name, reason=exc)
            return None
        text = plaintext.decode("utf-8", "replace")
        event("terminal", "covert_in", id=self.id, kind=kind.name,
              bytes=len(plaintext))
        return text

    def _extract_bytes(self, raw: bytes) -> bytes:
        is_gif = raw[:6] in (b"GIF87a", b"GIF89a")
        if is_gif:
            payload, _info = gif_stego.extract_gif(raw, self.psk)
            return payload
        image = Image.open(io.BytesIO(raw))
        payload, _info = image_stego.try_extract(image, self.psk)
        return payload


# ---------------------------------------------------------------- helpers


def _try_open(raw: bytes) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(raw))
        if image.format == "GIF":
            image.seek(0)
        return image.convert("RGB")
    except Exception:                           # noqa: BLE001
        return None


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _unhex_or_none(text: str) -> bytes | None:
    """A covert text body is hex; ordinary chat text almost never is."""
    stripped = text.strip()
    if len(stripped) < HEADER_BYTES * 2 or len(stripped) % 2:
        return None
    try:
        raw = bytes.fromhex(stripped)
    except ValueError:
        return None
    return raw if raw[:4] == b"PBX1" else None


def split_payload(envelope: bytes, parts: int,
                  capacities: list[int] | None = None
                  ) -> tuple[bytes, ...]:
    """Cut ``envelope`` into ``parts`` consecutive slices, each a PBX1 envelope.

    Consecutive (not interleaved) because the receiver must be able to put them
    back with nothing but their order, and because a picture that fails to
    decode then costs a contiguous hole rather than shredding every byte.

    Each slice gets its own :func:`build_slice` envelope carrying ``index`` and
    ``count``, for two reasons: the receiver can tell a genuine mid-picture
    slice from a picture with no frame in it, and a missing picture is detected
    by index rather than by lucky-count. Slices must be non-empty --- an empty
    slice would mean a picture with nothing hidden, the one thing an observer
    can single out --- so ``parts`` is authoritative and too many parts is an
    error rather than a silently skipped picture.
    """
    if parts <= 0:
        raise InvalidInput(
            "密文图片发送需要至少一张图片",
            hints=("先在待发送队列里准备图片",),
        )
    if parts > len(envelope):
        raise InvalidInput(
            f"密文共 {len(envelope)} 字节，不够分给 {parts} 张图片",
            hints=("少用几张图，或把密文写长一点",),
        )
    size, extra = divmod(len(envelope), parts)
    slices: list[bytes] = []
    start = 0
    for index in range(parts):
        end = start + size + (1 if index < extra else 0)
        chunk = envelope[start:end]
        start = end
        # **补满到这张图的实际容量。**
        #
        # 不补的话，每张图的载荷长度和它的明文份额成正比：一张只装了
        # 12 字节的图，和一张装了 400 字节的图，从"能装多少、实际用了
        # 多少"就看得出来。补满之后每张图都是满载，观察者只能知道
        # "发了几张图"——那正是用户要的：图片数量已经泄露了信息量，
        # 单张长度不该再泄露一份。
        #
        # 填充用随机字节：可压缩的数据（全零）会让图片体积异常变小，
        # 反而成了新的特征。
        real = len(chunk)
        room = 0
        if capacities and index < len(capacities):
            # 片信封头：MAGIC(4) + kind(1) + index+count(2) + 有效长度(2)
            overhead = len(MAGIC) + 1 + 2 + 2
            room = max(0, int(capacities[index]) - overhead)
        if room > real:
            # 填充用随机字节：可压缩的数据（全零）会让图片体积异常变小，
            # 反而成了新特征。
            chunk = chunk + os.urandom(room - real)
        slices.append(build_slice(index, parts, chunk, real_length=real))
    return tuple(slices)


def _encode_packet(packet: Packet) -> bytes:
    """Serialize a packet: a tiny length-prefixed container.

    Layout: ``b"PKT1" | n_images(2) | [len(4) | bytes] * n | text(utf-8)``.
    The trailing text is length-free because it is last -- one length field saved
    per message, and text is the field that exists on practically every packet.
    """
    parts = [b"PKT1", len(packet.images).to_bytes(2, "big")]
    for raw in packet.images:
        parts.append(len(raw).to_bytes(4, "big"))
        parts.append(raw)
    parts.append((packet.text or "").encode("utf-8"))
    return b"".join(parts)


def _decode_packet(data: bytes) -> tuple[str | None, tuple[bytes, ...]]:
    """Inverse of :func:`_encode_packet`. Returns ``(text, images)``."""
    if not data.startswith(b"PKT1"):
        raise InvalidInput("数据包格式无法识别（缺少 PKT1 标记）")
    cursor = 4
    if len(data) < cursor + 2:
        raise InvalidInput("数据包被截断：缺少图片数量")
    count = int.from_bytes(data[cursor:cursor + 2], "big")
    cursor += 2
    images: list[bytes] = []
    for _ in range(count):
        if len(data) < cursor + 4:
            raise InvalidInput("数据包被截断：缺少图片长度")
        size = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        if len(data) < cursor + size:
            raise InvalidInput(f"数据包被截断：图片声明 {size} 字节但不够")
        images.append(data[cursor:cursor + size])
        cursor += size
    text = data[cursor:].decode("utf-8", "replace")
    return (text if text else None), tuple(images)


__all__ = ["Inbound", "Packet", "Terminal"]
