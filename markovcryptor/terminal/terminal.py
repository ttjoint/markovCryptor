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
from .covers import CoverLibrary
from .protocol import (
    HEADER_BYTES,
    Kind,
    Mode,
    build_handshake,
    parse_handshake,
    peek_kind,
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
    #: True when ``note`` merely says "nothing here" -- routine, log-only.
    unremarkable: bool = False


class Terminal:
    """A named endpoint. Owns one session and one outbox of pending handshakes."""

    def __init__(self, terminal_id: str, network: Network, *,
                 psk: str = "pbx1", on_inbound: Callable[[Inbound], None] | None = None,
                 covers: CoverLibrary | None = None):
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
        #: Where handshake-response covers come from. Every image we send must
        #: be based on a real picture; see ``terminal/covers.py``.
        self.covers = covers or CoverLibrary()

    # ------------------------------------------------------------------ config

    @property
    def established(self) -> bool:
        return self.session.established

    @property
    def state(self) -> State:
        return self.session.state

    def subscribe(self, callback: Callable[[Inbound], None]) -> None:
        self._on_inbound = callback

    def attach(self, address: str | None = None) -> "Terminal":
        """Register on the network so incoming packets reach :meth:`deliver`.

        Returns ``self`` so a terminal can be created and attached in one
        expression. The sink signature is ``(sender, payload)`` because only the
        network knows who sent a packet.
        """
        network = self.network
        register = getattr(network, "register", None)
        if register is None:
            raise InvalidInput(
                f"{type(network).__name__} 不支持注册；请手动把包交给 deliver(sender, packet)"
            )
        register(address or self.id, lambda sender, data: self.deliver(sender, data))
        return self

    # ---------------------------------------------------------------- sending

    def send_packet(self, target: str, packet: Packet) -> None:
        """Send text and/or images in the clear, as one packet."""
        if packet.is_empty():
            raise InvalidInput("消息是空的：至少要有文本或一张图片")
        self.network.send(self.id, target, _encode_packet(packet))

    def send_handshake(self, target: str, images: tuple[bytes, ...],
                       *, mode: Mode | None = None) -> None:
        """Start (or answer) a handshake, hiding the key in the first image.

        The requirement is explicit that a handshake message *must* carry at
        least one image, because that is the only carrier the exchange travels
        in. We therefore fail loudly -- naming the required capacity -- rather
        than silently downgrading to a text carrier.
        """
        if not images:
            raise InvalidInput(
                "密钥交换必须至少附带一张图片：X25519 公钥藏在图片里，文本载体装不下"
            )
        if mode is Mode.RESPONSE:
            public, mode = self.session.answer()
        else:
            public, mode = self.session.begin()

        envelope = build_handshake(public, mode)
        stego, used = self._embed_into_first(envelope, images, what="密钥交换报文")
        self.peer_id = target
        event("terminal", "handshake_out", id=self.id, peer=target,
              mode=mode.value, cover=used)
        self.send_packet(target, Packet(images=stego))

    def send_covert_text(self, target: str, text: str) -> None:
        """Encrypt ``text`` and hide it in the text channel.

        The text carrier is the Chinese channel, which needs a trained model.
        Rather than import a model here (and make the whole terminal depend on
        user data), the encoder is injected with :meth:`use_text_channel`. A
        terminal without one can still do everything else -- text covert sending
        is simply unavailable, and says so.
        """
        if not self.session.established:
            raise InvalidInput(
                f"会话{self.session.state.label}，不能发送密文；先点「密钥交换」"
            )
        if not text:
            raise InvalidInput("密文内容为空")
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

    def use_text_channel(self, channel) -> None:
        """Attach the text carrier. ``channel`` must have ``embed_text``/``extract_text``."""
        self.text_channel = channel

    def send_covert_in_images(self, target: str, text: str,
                              images: tuple[bytes, ...],
                              *, chat_text: str | None = None) -> None:
        """Encrypt ``text`` and hide it in every image of a normal message.

        The *visible* text (``chat_text``) still goes out as plaintext: the user
        asked to keep the plaintext side unchanged. Only the images are stego.
        """
        if not self.session.established:
            raise InvalidInput(
                f"会话{self.session.state.label}，不能发送密文；先点「密钥交换」"
            )
        if not images:
            raise InvalidInput("密文图片发送需要至少一张图片")
        envelope = self.session.seal(Kind.BODY_IMAGE, text.encode("utf-8"))
        stego, used = self._embed_into_first(envelope, images, what="正文密文")
        event("terminal", "covert_out", id=self.id, peer=target,
              cover=used, bytes=len(envelope))
        self.send_packet(target, Packet(text=chat_text, images=stego))

    # --------------------------------------------------------------- embedding

    def _embed_into_first(self, envelope: bytes, images: tuple[bytes, ...],
                          *, what: str) -> tuple[tuple[bytes, ...], str]:
        """Hide ``envelope`` in the first image that can hold it.

        Tries them in order and reports *why* the ones that failed did, so a user
        who picked four small pictures is told what was wrong rather than "失败".
        """
        reasons: list[str] = []
        for index, raw in enumerate(images):
            try:
                stego = self._embed_bytes(envelope, raw)
            except CapacityExceeded as exc:
                reasons.append(f"第 {index + 1} 张：{exc}")
                continue
            except (UnsupportedCarrier, InvalidInput) as exc:
                reasons.append(f"第 {index + 1} 张：{exc}")
                continue
            rest = images[:index] + images[index + 1:]
            return (stego,) + rest, f"#{index + 1}"
        raise CapacityExceeded(
            f"{what}装不进这 {len(images)} 张图片里",
            reasons=tuple(reasons),
            hints=(
                f"先从图片序列里选一张能容纳 {len(envelope)} 字节的图",
                "需要的容量随图面积增长：160×160 约 162 字节，256×256 约 441 字节",
            ),
        )

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

        for index, raw in enumerate(raws):
            try:
                result, display = self._inspect_image(raw)
            except Exception as exc:            # noqa: BLE001 - never surface carrier bugs
                notes.append(f"图片 {index + 1}：处理失败——{exc}")
                event("terminal", "image_failed", level=logging.WARNING,
                      id=self.id, index=index + 1, error=exc)
                continue
            if display is not None:
                shown.append(display)
            if result.covert:
                covert.append(result.covert)
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
        except (NoHiddenFrame, CarrierDamaged) as exc:
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
            return self._on_handshake(payload), display
        if kind is Kind.BODY_TEXT:
            # A text body inside an image means the sender's carrier choice was
            # inconsistent. Not fatal, but worth saying rather than guessing.
            return _Inspected(note="信封类型与载体不符：文本正文出现在图片里"), display
        return _Inspected(covert=self._decrypt(kind, payload)), display

    def _inspect_text(self, text: str) -> _Inspected:
        """Text may only carry ``BODY_TEXT``. Everything else is not stego."""
        envelope = _unhex_or_none(text)
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

    def _on_handshake(self, payload: bytes) -> _Inspected:
        peer_public, mode = parse_handshake(payload)
        was = self.session.state
        completed = self.session.absorb(peer_public, mode)
        summary = f"握手 {mode.name}（{was.label} → {self.session.state.label}）"
        event("terminal", "handshake_in", id=self.id, mode=mode.value,
              state=self.session.state.value)

        if mode is Mode.REQUEST:
            # Requirement: a request must be answered automatically, with an
            # image, without the user doing anything.
            self._auto_respond(peer_public)
            return _Inspected(handshake=summary + "，已自动回发")
        if completed:
            return _Inspected(handshake=summary + "，会话可用")
        return _Inspected(handshake=summary)

    def _auto_respond(self, peer_public: bytes) -> None:
        """Reply with our own key, hidden in an image we generate.

        We have no cover from the peer to reuse, so we synthesise one. It has to
        be big enough for the handshake envelope; 256x256 gives 441 bytes, well
        over the ~60 needed, and keeps the response robust.
        """
        target = self.peer_id
        if target is None:
            event("terminal", "handshake_no_peer", level=logging.WARNING, id=self.id)
            return
        public, mode = self.session.answer()
        envelope = build_handshake(public, mode)
        # The response must ride in a real picture, same as any other image we
        # send -- a noise cover would announce itself. cover_for() fails loudly
        # if the library cannot hold the envelope, naming the shortfall.
        cover = self.covers.cover_for(len(envelope))
        stego = self._embed_bytes(envelope, cover)
        event("terminal", "handshake_auto_response", id=self.id, peer=target)
        self.network.send(self.id, target, _encode_packet(Packet(images=(stego,))))

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
