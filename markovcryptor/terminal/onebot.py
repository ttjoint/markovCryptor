"""OneBot v11 transport: the ``Network`` that puts the terminal on a real IM.

Everything above this file (protocol, session, terminal) is transport-agnostic
already; this is the one place that knows about QQ. See
``docs/QQ_TRANSPORT.md`` for why the seam sits exactly here.

Three jobs, and only three:

**Sending.** ``send()`` gets an already-PKT1-encoded packet. It is decoded, then
re-emitted as OneBot ``send_private_msg`` actions: one QQ message per picture,
plus the packet's text riding on the first of them.

**Receiving.** OneBot events are turned back into packets and handed to
``deliver``. Ordinary chat from the peer is *also* delivered --- the terminal
inspects it and decides it is not ours, which is exactly the behaviour
``docs/TERMINAL.md`` §5 asks for. Deciding "is this stego?" is the terminal's
job, not the transport's.

**Reassembly.** One QQ message carries at most one picture worth of payload that
we can depend on, so a packet with n pictures leaves as n messages. QQ gives no
ordering or grouping guarantee across messages, so each outgoing picture carries
a small transport-level trailer saying which packet it belongs to and its index
within that packet. The receiver buffers per packet until all n have arrived.

Why the trailer is *not* in the PBX1 envelope: the envelope is authenticated
ciphertext (or a signed handshake). Putting routing metadata inside it would make
the transport depend on the crypto layer and would spend AEAD-protected bytes on
something that is not a secret. Routing belongs to the wire, so it rides on the
wire, appended after the PNG's ``IEND`` --- which every decoder ignores and which
therefore leaves the picture's pixels --- and so the stego payload --- untouched.
An image without a trailer is simply "a packet of one picture", which is what
both ordinary chat images and the peer's own messages look like.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import struct
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from ..observability import event
from .terminal import Packet, _decode_packet, _encode_packet
from .transport import TransportError

#: Marks an image as carrying routing information. Four bytes of magic make a
#: false positive on a real PNG ending essentially impossible.
TRAILER_MAGIC = b"MKXT"
#: magic(4) | packet_id(8) | index(1) | count(1)
TRAILER = struct.Struct(">4sQBB")

#: A picture whose trailer never gets its siblings is dropped after this long, so
#: a lost message cannot leak memory or silently complete a much later packet.
DEFAULT_REASSEMBLY_TIMEOUT = 30.0
#: Reconnect backoff. Deliberately lazy: a tight reconnect loop against QQ looks
#: like abuse and is the fastest way to get an account flagged.
DEFAULT_RECONNECT_DELAY = 3.0
#: How long to wait for an image URL before giving up on that one picture.
DEFAULT_DOWNLOAD_TIMEOUT = 30.0


def _new_packet_id() -> int:
    """8 random bytes, kept inside the positive range of ``Q``."""
    return int.from_bytes(os.urandom(8), "big") & 0x7FFF_FFFF_FFFF_FFFF


# ------------------------------------------------------------------ trailer


def attach_trailer(raw: bytes, packet_id: int, index: int, count: int) -> bytes:
    """Mark ``raw`` as picture ``index`` of ``count`` belonging to ``packet_id``."""
    if not 0 <= index < count <= 255:
        raise TransportError(f"图片分片序号越界：{index}/{count}")
    return raw + TRAILER.pack(TRAILER_MAGIC, packet_id, index, count)


def strip_trailer(raw: bytes) -> tuple[bytes, int, int, int] | None:
    """Return ``(image, packet_id, index, count)``, or ``None`` if unmarked.

    Returning ``None`` rather than raising is deliberate: an unmarked picture is
    the normal case (the peer's chat image, a photo someone just sent), not a
    fault.
    """
    if len(raw) <= TRAILER.size:
        return None
    body, tail = raw[: -TRAILER.size], raw[-TRAILER.size:]
    magic, packet_id, index, count = TRAILER.unpack(tail)
    if magic != TRAILER_MAGIC:
        return None
    if count == 0 or index >= count:
        # Magic matched but the numbers are nonsense -- that is corruption, and
        # guessing a position would splice a wrong picture into the packet.
        return None
    return body, packet_id, index, count


# ---------------------------------------------------------------- reassembly


@dataclass
class _Partial:
    """Pictures of one packet that have arrived so far."""

    count: int
    pictures: dict[int, bytes] = field(default_factory=dict)
    text: str | None = None
    started: float = field(default_factory=time.monotonic)

    def complete(self) -> bool:
        return len(self.pictures) == self.count and all(
            i in self.pictures for i in range(self.count)
        )


class Reassembler:
    """Turns OneBot messages back into whole PKT1 packets.

    Kept separate from the socket code so the grouping rules can be tested
    without a network at all --- they are the part that is easy to get subtly
    wrong, and the part the P0 measurement said we could not skip.
    """

    def __init__(self, timeout: float = DEFAULT_REASSEMBLY_TIMEOUT) -> None:
        self.timeout = timeout
        self._partials: dict[tuple[str, int], _Partial] = {}

    def feed(self, sender: str, text: str | None,
             images: list[bytes]) -> list[bytes]:
        """Feed one incoming message; return the packets it completed."""
        self._expire()
        done: list[bytes] = []
        loose: list[bytes] = []

        for raw in images:
            marked = strip_trailer(raw)
            if marked is None:
                loose.append(raw)
                continue
            body, packet_id, index, count = marked
            key = (sender, packet_id)
            partial = self._partials.get(key)
            if partial is None:
                partial = self._partials[key] = _Partial(count=count)
            elif partial.count != count:
                # Same id, different count: the sender restarted or ids collided.
                # Trust the newcomer and start over rather than mix two packets.
                event("onebot", "reassembly_conflict", level=logging.WARNING,
                      sender=sender, packet_id=packet_id,
                      had=partial.count, now=count)
                partial = self._partials[key] = _Partial(count=count)
            partial.pictures[index] = body
            if text:
                partial.text = text
            if partial.complete():
                del self._partials[key]
                done.append(_rebuild(partial))
            elif count == 1:
                # Defensive: count==1 always completes above, but if it ever did
                # not we would rather drop the picture than hang forever.
                del self._partials[key]

        if loose:
            # Untrailered pictures are their own single-picture packet. They are
            # never merged with a partial: they are not ours to group.
            done.append(_encode_packet(
                Packet(images=tuple(loose), text=text or None)))
        elif not images and text:
            # A message with words and nothing hidden in it. The terminal still
            # has to see it -- ordinary chat belongs in the chat window, and a
            # text carrier (the Chinese channel) rides here too. Dropping it
            # because there were no pictures would silently swallow every
            # covert-text message.
            done.append(_encode_packet(Packet(text=text)))
        return done

    def _expire(self) -> None:
        cutoff = time.monotonic() - self.timeout
        stale = [key for key, part in self._partials.items() if part.started < cutoff]
        for key in stale:
            part = self._partials.pop(key)
            event("onebot", "reassembly_timeout", level=logging.WARNING,
                  sender=key[0], packet_id=key[1],
                  have=sorted(part.pictures), want=part.count)

    def reset(self) -> None:
        self._partials.clear()


def _rebuild(partial: _Partial) -> bytes:
    """Put a finished packet back together in index order."""
    images = tuple(partial.pictures[i] for i in range(partial.count))
    return _encode_packet(Packet(images=images, text=partial.text or None))


# --------------------------------------------------------------- outbound


def _action(target: str, segments: list[dict], echo: str = "") -> dict:
    """One ``send_private_msg`` action. ``user_id`` must be numeric for OneBot."""
    try:
        user_id = int(str(target))
    except (TypeError, ValueError):
        raise TransportError(
            f"对端 id 必须是 QQ 号（纯数字），收到 {target!r}"
        ) from None
    payload: dict = {"action": "send_private_msg",
                     "params": {"user_id": user_id, "message": segments}}
    if echo:
        payload["echo"] = echo
    return payload


def _image_segment(raw: bytes) -> dict:
    """A picture as a base64 segment.

    base64 rather than a local path because the two sides may be different
    machines and the path would not exist on the peer; ``base64://`` is the one
    form OneBot implementations universally accept.
    """
    return {"type": "image",
            "data": {"file": "base64://" + base64.b64encode(raw).decode("ascii")}}


def frames_for(target: str, packet: bytes, *,
               packet_id: int | None = None) -> list[dict]:
    """PKT1 packet bytes -> the OneBot actions that put it on the wire.

    Pure (no socket, no clock beyond id generation), which is what lets the
    mapping be tested exactly.
    """
    text, images = _decode_packet(packet)
    if not images:
        if not text:
            raise TransportError("空包：既没有文字也没有图片")
        # Text-only is a single message and needs no reassembly at all.
        return [_action(target, [{"type": "text", "data": {"text": text}}])]

    pid = _new_packet_id() if packet_id is None else packet_id
    count = len(images)
    actions = []
    for index, raw in enumerate(images):
        marked = attach_trailer(raw, pid, index, count)
        segments = []
        if index == 0 and text:
            # Text rides with the first picture so it shares that picture's
            # packet id; a separate text message would have no id at all and
            # would be delivered as its own, unrelated packet.
            segments.append({"type": "text", "data": {"text": text}})
        segments.append(_image_segment(marked))
        actions.append(_action(target, segments, echo=f"mkx-{pid}-{index}"))
    return actions


# ---------------------------------------------------------------- inbound


def _segments_of(event_payload: dict) -> list[dict]:
    """Message segments, tolerating the string form (``messageFormat=string``).

    The config we write uses ``array`` (``onebot_config.py``), but a hand-edited
    ``onebot_<uin>.json`` could be either, and failing silently on that is worse
    than handling it.
    """
    message = event_payload.get("message")
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    text = event_payload.get("raw_message") or (message if isinstance(message, str) else "")
    return [{"type": "text", "data": {"text": text}}] if text else []


def split_segments(segments: list[dict]) -> tuple[str, list[dict]]:
    """Return ``(text, image_data_records)`` from OneBot message segments."""
    texts: list[str] = []
    images: list[dict] = []
    for seg in segments:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            texts.append(str(data.get("text") or ""))
        elif kind == "image":
            images.append(data)
    return "".join(texts), images


def _decode_base64(data: dict) -> bytes | None:
    """Handle the ``base64://`` form directly, without a round trip."""
    file = str(data.get("file") or "")
    if not file.startswith("base64://"):
        return None
    try:
        return base64.b64decode(file[len("base64://"):], validate=False)
    except Exception:                                  # noqa: BLE001
        return None


def _download_source(data: dict) -> str | None:
    """Pick the best URL for an image segment.

    ``url`` first: it is a real fetchable link. Then a ``file`` that is itself a
    URL (some implementations put the direct link there).
    """
    url = data.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://", "file://")):
        return url
    file = data.get("file")
    if isinstance(file, str) and file.startswith(("http://", "https://", "file://")):
        return file
    return None


class OneBotNetwork:
    """A ``Network`` backed by one OneBot v11 WebSocket connection.

    One instance = one QQ account = one terminal. ``register()`` records the
    local address so incoming events can be routed; the *peer* is never
    registered, because it is a remote QQ number and there is nothing to call
    locally --- which is why :meth:`send` does not require the target to be
    known, unlike :class:`~markovcryptor.terminal.transport.LoopbackNetwork`.
    """

    def __init__(self, url: str, self_id: str | None = None, *, token: str = "",
                 connect_factory=None, download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
                 reassembly_timeout: float = DEFAULT_REASSEMBLY_TIMEOUT,
                 reconnect_delay: float = DEFAULT_RECONNECT_DELAY) -> None:
        self.url = url
        self.self_id = str(self_id) if self_id else None
        self.token = token
        self.reconnect_delay = reconnect_delay
        self.download_timeout = download_timeout
        self.reassembler = Reassembler(timeout=reassembly_timeout)
        self._connect_factory = connect_factory
        self._sinks: list = []
        self._outbox: "queue.Queue[dict]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = threading.Event()
        #: Counters the UI / a health check can read without touching internals.
        self.stats = {"sent": 0, "received": 0, "images": 0, "dropped": 0,
                      "reconnects": 0}

    # ------------------------------------------------------------ lifecycle

    def register(self, address: str, sink) -> None:
        """Record a local sink. ``sink(sender, packet)`` is called per packet."""
        self._sinks.append(sink)
        if not self.self_id:
            self.self_id = str(address)

    def start(self) -> "OneBotNetwork":
        """Start the background connection thread. Returns ``self``."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stopping.clear()
        self._thread = threading.Thread(target=self._thread_main,
                                        name=f"onebot-{self.self_id or '?'}",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Ask the loop to finish and wait briefly for it."""
        self._stopping.set()
        # The writer blocks on the outbox; a sentinel is what actually releases
        # it. Cancelling tasks from another thread is not allowed, so this is
        # the only safe way in.
        self._outbox.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:                       # noqa: BLE001 - thread boundary
            event("onebot", "loop_crashed", level=logging.ERROR,
                  url=self.url, error=exc)

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            try:
                async with self._connect() as link:
                    event("onebot", "connected", url=self.url, id=self.self_id)
                    await self._pump(link)
            except Exception as exc:                   # noqa: BLE001 - reconnect, not crash
                if self._stopping.is_set():
                    return
                self.stats["reconnects"] += 1
                event("onebot", "disconnected", level=logging.WARNING,
                      url=self.url, error=exc,
                      retry_in=self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    def _connect(self):
        if self._connect_factory is not None:
            return self._connect_factory(self.url, self.token)
        from ..onebot_ws import connect          # lazily: websockets is optional
        return connect(self.url, self.token)

    async def _pump(self, link) -> None:
        reader = asyncio.create_task(self._read_loop(link))
        writer = asyncio.create_task(self._write_loop(link))
        try:
            await asyncio.wait({reader, writer},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reader, writer):
                task.cancel()
            await asyncio.gather(reader, writer, return_exceptions=True)

    # ---------------------------------------------------------------- outbox

    def send(self, sender: str, target: str, packet: bytes) -> None:
        """Queue ``packet`` for ``target``. Never blocks the caller."""
        for action in frames_for(target, packet):
            self._outbox.put(action)

    async def _write_loop(self, link) -> None:
        loop = asyncio.get_running_loop()
        while True:
            action = await loop.run_in_executor(None, self._outbox.get)
            if action is None:                 # shutdown sentinel
                return
            try:
                await link.send(json.dumps(action))
                self.stats["sent"] += 1
            except Exception as exc:                   # noqa: BLE001
                event("onebot", "send_failed", level=logging.WARNING,
                      url=self.url, error=exc)
                # Put it back so a reconnect can try again, but do not spin.
                self._outbox.put(action)
                await asyncio.sleep(1.0)
                raise

    # --------------------------------------------------------------- inbound

    async def _read_loop(self, link) -> None:
        while True:
            raw = await link.recv()
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                await self._handle_event(payload)

    async def _handle_event(self, payload: dict) -> None:
        if payload.get("post_type") != "message":
            return                              # heartbeats, notices, meta events
        if payload.get("message_type") != "private":
            return                              # private chats only, for now
        sender = payload.get("user_id")
        if sender is None:
            return
        sender = str(sender)
        if self.self_id and sender == self.self_id:
            return                              # our own echo, if ever reported

        text, image_records = split_segments(_segments_of(payload))
        images: list[bytes] = []
        for record in image_records:
            blob = await asyncio.to_thread(self._fetch_image, record)
            if blob is None:
                self.stats["dropped"] += 1
                continue
            images.append(blob)

        self.stats["received"] += 1
        self.stats["images"] += len(images)
        for packet in self.reassembler.feed(sender, text or None, images):
            for sink in list(self._sinks):
                try:
                    sink(sender, packet)
                except Exception as exc:               # noqa: BLE001 - never kill the reader
                    event("onebot", "sink_failed", level=logging.ERROR,
                          url=self.url, error=exc)

    def _fetch_image(self, record: dict) -> bytes | None:
        """Get the bytes of one image segment, or ``None`` if it is unreachable.

        Downloading immediately is deliberate: QQ image URLs expire, so a
        deferred fetch is a silent data loss (``docs/QQ_TRANSPORT.md`` §4).
        """
        inline = _decode_base64(record)
        if inline is not None:
            return inline
        source = _download_source(record)
        if source is None:
            event("onebot", "image_no_source", level=logging.WARNING,
                  url=self.url, keys=sorted(record))
            return None
        try:
            with urllib.request.urlopen(source, timeout=self.download_timeout) as response:
                return response.read()
        except Exception as exc:                          # noqa: BLE001
            event("onebot", "image_download_failed", level=logging.WARNING,
                  url=self.url, error=exc)
            return None


__all__ = [
    "DEFAULT_REASSEMBLY_TIMEOUT", "OneBotNetwork", "Reassembler", "TRAILER",
    "TRAILER_MAGIC", "attach_trailer", "frames_for", "split_segments",
    "strip_trailer",
]


# ------------------------------------------------------- in-process OneBot
#
# The OneBot equivalent of ``LoopbackNetwork``: a stand-in "server" that routes
# one account's actions to another account's event handler, entirely in memory.
#
# This is not only a test double. It is the reference semantics for the wire
# behaviour we must survive -- in particular that **one action is one message**,
# never a batch, which is what the P0 measurement found on real QQ
# (``docs/QQ_TRANSPORT.md`` §4). Anything a real OneBot adds on top of this
# (ordering, loss, latency) is a deviation from it.


class _InProcessLink:
    """Delivers actions straight into another account's event handler."""

    def __init__(self, hub: "InProcessOneBot", uin: str) -> None:
        self.hub = hub
        self.uin = uin

    async def send(self, raw: str) -> None:
        self.hub.route(self.uin, json.loads(raw))

    async def recv(self):
        await asyncio.Event().wait()               # never yields; nothing to read

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class InProcessOneBotNetwork(OneBotNetwork):
    """An ``OneBotNetwork`` whose "socket" is another account in this process.

    ``send()`` routes immediately rather than queueing, because a caller that
    is driving the pair by hand wants the action on the wire before it asserts.
    """

    def __init__(self, hub: "InProcessOneBot", uin: str, *,
                 token: str = "", **kwargs) -> None:
        super().__init__(f"in-process://{uin}", uin, token=token, **kwargs)
        self.hub = hub
        self._link = _InProcessLink(hub, uin)

    def send(self, sender: str, target: str, packet: bytes) -> None:
        for action in frames_for(target, packet):
            self.hub.route(self.self_id, action)

    def start(self) -> "InProcessOneBotNetwork":
        self.hub.accounts[self.self_id] = self   # already "connected"
        return self

    def stop(self) -> None:
        self.hub.accounts.pop(self.self_id, None)


class InProcessOneBot:
    """Routes OneBot actions between accounts, in memory, synchronously.

    Usage mirrors a real pair: give each side an
    :class:`InProcessOneBotNetwork`, then call :meth:`pump` to deliver.
    """

    def __init__(self) -> None:
        self.accounts: dict[str, InProcessOneBotNetwork] = {}
        self.pending: list[tuple[str, dict]] = []
        #: Every action ever routed, in order, for assertions and transcripts.
        self.history: list[tuple[str, dict]] = []

    def connect(self, uin: str) -> InProcessOneBotNetwork:
        """Create and "connect" one account. Returns its network."""
        return InProcessOneBotNetwork(self, str(uin)).start()

    def route(self, sender: str, action: dict) -> None:
        """Turn one outgoing action into one incoming event on its target."""
        self.history.append((sender, action))
        params = action.get("params") or {}
        try:
            target = str(params.get("user_id"))
        except Exception:                          # noqa: BLE001
            return
        if target not in self.accounts:
            event("onebot", "in_process_no_target", level=logging.DEBUG,
                  sender=sender, target=target)
            return
        self.pending.append((target, {
            "post_type": "message",
            "message_type": "private",
            "user_id": int(sender),
            "self_id": int(target),
            "message": params.get("message") or [],
        }))

    def pump(self, *, reverse: bool = False, max_rounds: int = 500) -> int:
        """Deliver everything queued, including what delivery triggers.

        ``reverse=True`` delivers newest-first, which is how the reassembly
        rules get tested against the ordering QQ does not promise.
        """
        delivered = 0
        for _ in range(max_rounds):
            if not self.pending:
                return delivered
            target, payload = (self.pending.pop() if reverse
                               else self.pending.pop(0))
            network = self.accounts[target]
            asyncio.run(network._handle_event(payload))
            delivered += 1
        raise TransportError(
            f"pump 在 {max_rounds} 轮后仍未排空（剩 {len(self.pending)} 条）；"
            "两侧可能在无限互相回复"
        )


__all__ += ["InProcessOneBot", "InProcessOneBotNetwork"]


# ------------------------------------------------------------ per-side fanout


class RoutedNetwork:
    """One ``Network`` made of several, chosen by the *sender's* address.

    ``TerminalHub`` gives both terminals one network object, which is right for
    loopback (one shared wire) but wrong for QQ: each account has its own
    WebSocket and its own UIN. This adapts "two connections" into the single
    object the hub expects, without the hub knowing anything about QQ.

    Only the sender decides the route. A terminal may send to a peer that is not
    locally registered -- that is the normal case, the peer is a remote QQ -- so
    there is nothing to look up on the receiving side.
    """

    def __init__(self, routes: dict[str, "OneBotNetwork"]) -> None:
        #: Keyed by the *local* terminal name (``alice``/``bob``), not by UIN.
        #: The hub, the routes and the web UI all address terminals by that name,
        #: and keeping it stable means switching to QQ changes no route at all.
        #: Each network still knows its own UIN -- that is what goes on the wire.
        self.routes = {str(address): network for address, network in routes.items()}

    def register(self, address: str, sink) -> None:
        key = str(address)
        try:
            network = self.routes[key]
        except KeyError:
            raise TransportError(
                f"没有为 {key!r} 配连接；已配：{sorted(self.routes)}"
            ) from None
        network.register(key, sink)

    def send(self, sender: str, target: str, packet: bytes) -> None:
        key = str(sender)
        try:
            network = self.routes[key]
        except KeyError:
            raise TransportError(
                f"{key!r} 没有出站连接；已配：{sorted(self.routes)}"
            ) from None
        network.send(sender, target, packet)

    def start(self) -> "RoutedNetwork":
        for network in self.routes.values():
            network.start()
        return self

    def stop(self) -> None:
        for network in self.routes.values():
            network.stop()


__all__ += ["RoutedNetwork"]
