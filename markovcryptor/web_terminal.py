"""Terminal sessions exposed over the local Web API.

The terminal itself is UI-free (``markovcryptor/terminal/``); this module wires
two of them -- ``alice`` and ``bob`` -- into the HTTP surface so the web UI can
drive a conversation. One session pair per process is deliberate: the terminals
are a *feature demonstration* of the protocol, not a multiuser chat server, and
per-connection state would need the websocket layer this project deliberately
does not have.

Queues are NOT here. Which pictures the user has lined up to send is editor
state that belongs to the browser: the user should be able to add, reorder,
inspect and discard candidates without a round trip, and the server has no
business holding pictures that may never be sent. The UI keeps its own pending
list and uploads a picture only when the user actually sends it.
"""

from __future__ import annotations

import logging
import time
from typing import List

from fastapi import UploadFile
from fastapi.responses import JSONResponse, Response

from .channels.base import Carrier
from .observability import METRICS
from .terminal import LoopbackNetwork, Network, Packet, Terminal
from .terminal.terminal import Inbound, _decode_packet

#: Module-level alias: with ``from __future__ import annotations`` every
#: annotation is a string, and pydantic resolves those against module globals --
#: a name defined inside ``wire_terminal_routes`` would stay unresolved.
FileList = List[UploadFile]

#: How many recent conversations each window remembers server-side. The UI
#: renders what it receives live; this is only so a page reload can show
#: something sensible instead of an empty window.
HISTORY_LIMIT = 200


class TextChannelAdapter:
    """把 ``Channel`` 的（载体，密钥）接口收成终端要的 ``embed_text``/``extract_text``。

    终端协议层只认识"给我一段明文，换回一段中文"——它不该知道信道有
    ``options``、``Carrier``、``Envelope`` 这些平台概念。适配器就做这一件事，
    所以它很小，也就可以留在这里而不是塞进信道层。
    """

    def __init__(self, channel) -> None:
        self.channel = channel

    def embed_text(self, payload: bytes):
        envelope = self.channel.embed(Carrier.of_text(""), payload, b"")
        return str(envelope.primary), envelope.report

    def extract_text(self, covertext: str) -> bytes:
        return self.channel.extract(Carrier.of_text(covertext), b"")


class TerminalHub:
    """One pair of terminals sharing one network, plus their history.

    By default the network is :class:`LoopbackNetwork` -- two windows on this
    machine, the "本地演练" mode. Pass ``network=`` to put the same terminals on
    a real transport (``terminal/onebot.py``); nothing below this line changes,
    which is the whole point of the ``Network`` seam.

    ``names`` lets the two sides be called something other than alice/bob -- on
    QQ they are the two UINs, and every log line, route path and UI label
    should say so.
    """

    def __init__(self, psk: str = "pbx1", text_channel=None, *,
                 network: Network | None = None,
                 names: tuple[str, str] = ("alice", "bob"),
                 fallback_covers: dict[str, list[bytes]] | None = None) -> None:
        self.network = network if network is not None else LoopbackNetwork()
        self.terminals: dict[str, Terminal] = {}
        for name in names:
            terminal = Terminal(name, self.network, psk=psk).attach()
            if text_channel is not None:
                # 文本混淆走中文信道；模型由调用方注入，没有就诚实地不可用。
                terminal.use_text_channel(text_channel)
            self.terminals[name] = terminal
        self.history: dict[str, list[Inbound]] = {name: [] for name in self.terminals}
        #: 收到的每个包的原始字节，按到达顺序。UI 重载后要重放宿主图，
        #: 而 Inbound 里只有解码后的 PIL 图；存原始包是最简单的回放来源。
        self.raw_history: dict[str, list[tuple[str, bytes]]] = {
            name: [] for name in self.terminals
        }
        #: 每个终端**看到**的完整对话（自己发的 + 对方发的），一条时间线。
        #: 两端各自一份，顺序按本地时钟推进——真实的两个客户端也是各记各的。
        self.transcript: dict[str, list[dict]] = {name: [] for name in self.terminals}
        #: 每个终端自己发出去的图片字节。收件历史里没有"我发的包"，
        #: 但 UI 要显示自己发的图，所以单独留一份，供 /terminal/media 读。
        self.outgoing: dict[str, list[tuple[bytes, ...]]] = {
            name: [] for name in self.terminals
        }
        #: 每个终端"手头现成可用的图"的队首。浏览器队列在前端，服务端看不见；
        #: 前端在用户加图时把它推上来，自动握手回包就从这里取头一张。
        self.cover_queue: dict[str, list[bytes]] = {name: [] for name in self.terminals}
        #: 兜底底图：接 QQ 时没有浏览器在前面推图，而握手回包必须先有一张
        #: 自己的图才能发（不能拿对方那张重写）。没有兜底的话，对端发来的
        #: 握手会一直压在 ``pending_request`` 里，用户看不到任何提示。
        self.fallback_covers: dict[str, list[bytes]] = {
            name: list(fallback_covers.get(name, ())) if fallback_covers else []
            for name in self.terminals
        }
        self._fallback_cursor: dict[str, int] = {name: 0 for name in self.terminals}
        for name, terminal in self.terminals.items():
            terminal.take_cover = (lambda n=name: self._take_cover(n))
            terminal.on_packet = (
                lambda sender, packet, n=name: self.raw_history[n].append((sender, packet))
            )
            terminal.subscribe(self._collector(name))

    def _take_cover(self, name: str) -> bytes | None:
        """Hand out a cover for an automatic handshake reply.

        The browser queue first (it holds exactly the pictures the user chose),
        then the fallback pool. The fallback **cycles** rather than pops: a
        cover is not consumed by being used, and a second handshake later in the
        session should not fail just because the first one spent the only one.
        """
        queue = self.cover_queue[name]
        if queue:
            return queue.pop(0)
        pool = self.fallback_covers.get(name) or []
        if not pool:
            return None
        cursor = self._fallback_cursor[name]
        self._fallback_cursor[name] = cursor + 1
        return pool[cursor % len(pool)]

    def set_covers(self, name: str, images: list[bytes]) -> bool:
        """Replace what this side has on hand, then answer any held handshake.

        Called when the user queues pictures. Returning whether a held
        handshake was answered lets the UI say "sent the reply just now".
        """
        self.cover_queue[name] = list(images)
        released = self.terminals[name].retry_pending()
        if released:
            self.pump()
        return released

    def name_of(self, address: str) -> str:
        """The local name for a wire address, when it is one of ours.

        The transcript must read the same whichever mode it is in: locally the
        address was already ``bob``; on QQ it is a UIN, and showing ``10002`` in
        a window titled "bob" makes the user wonder if two different peers are
        talking. Anything unknown is left as-is -- an unregistered sender is a
        real third party and their address is the honest label.
        """
        for name, uin in self.uins.items():
            if uin == address:
                return name
        return address

    def _collector(self, name: str):
        def sink(inbound: Inbound) -> None:
            bucket = self.history[name]
            index = len(self.raw_history[name]) - 1                 if self.raw_history[name] else -1
            bucket.append(inbound)
            if len(bucket) > HISTORY_LIMIT:
                del bucket[: len(bucket) - HISTORY_LIMIT]
            self._record(name, {
                "direction": "in",
                "sender": self.name_of(inbound.sender),
                "chat_text": inbound.chat_text,
                "chat_image_count": len(inbound.chat_images),
                "covert": list(inbound.covert),
                "notes": list(inbound.notes),
                "handshake": inbound.handshake,
                "packet_index": index,
                "own_index": -1,
                "mode": "plain",
                # 收到的时间：聊天软件每条消息都要带时刻。
                "at": time.time(),
            })
        return sink

    def _record(self, name: str, entry: dict) -> None:
        bucket = self.transcript[name]
        bucket.append(entry)
        if len(bucket) > HISTORY_LIMIT:
            del bucket[: len(bucket) - HISTORY_LIMIT]

    def note_outgoing(self, name: str, *, mode: str, text: str | None,
                      images: tuple[bytes, ...], covert: str | None = None) -> int:
        """记一条"我发出的"到本地对话线，返回本条在对话线里的序号。

        发送动作本身不产生 ``Inbound``（那是收到的包），但聊天软件必须显示
        自己发的消息。这里只在本地记一笔，不进网络——两端各记各的，
        正好对应"两个隔绝的客户端"这个设定。

        三件容易搞错的事，都在这里定死：

        * **``chat_text`` 是网线内容，不是用户键入的原文**：明文窗站在网关
          位置预览"这条消息实际传出去的东西"。明文发送传的是用户写的字；
          文本混淆传的是生成的那串中文宿主文本（真正的密文在它里面，
          别人看不出）；图片混淆传的是用户的公开说明。要隐藏的那句话
          永远不进明文窗——它只出现在密文窗。
        * **图片字节要留一份**：自己发的图不在收件历史里，UI 又要显示它们，
          所以这里存下来供 ``/terminal/media`` 读。
        * **``covert`` 是刚发出去的密文原文**：发送方知道自己写了什么，
          所以密文窗里也该看见它；对方是靠解密才知道的，两条路径互不相干。
        """
        entry = {
            "direction": "out",
            "sender": name,
            # 网线上可见的那串内容。密文发送时由调用方传入宿主文本（或 None），
            # 原始密文绝不出现在这里。
            "chat_text": text,
            "chat_image_count": len(images),
            "covert": [covert] if covert else [],
            "notes": [],
            "handshake": None,
            "mode": mode,
            "packet_index": -1,
            # 「我发的第几条」——media 端点靠它取自己发的图。
            "own_index": len(self.outgoing[name]),
            "at": time.time(),
        }
        self.outgoing[name].append(images)
        self._record(name, entry)
        return entry["own_index"]
    @property
    def text_channel_ready(self) -> bool:
        return any(t.text_channel is not None for t in self.terminals.values())

    #: 名字 -> 线上地址（接 QQ 时是 QQ 号）。本地演练为空，名字就是地址。
    uins: dict[str, str] = {}

    #: 单账号模式下的对端 QQ 号（本机只登录了一个号时，对端在另一台设备上）。
    peer_uin: str = ""

    def peer_address_of(self, name: str) -> str:
        """Where this local terminal should send: the peer's wire address.

        Two shapes exist and they must not be confused:

        * 同机双号 -- 对端就是本机另一个终端，用它的地址；
        * 一台设备一个号 -- 对端在另一台设备上，本机只知道它的 QQ 号，
          由 ``--peer`` 给出。此时 ``_other`` 无解，硬要两个终端只会凭空
          多出一个永远收不到东西的空窗口。
        """
        others = [n for n in self.terminals if n != name]
        if others:
            return self.address_of(others[0])
        if self.peer_uin:
            return self.peer_uin
        from .core.errors import InvalidInput

        raise InvalidInput(
            "本机只有一个 QQ 号，也不知道对端是谁",
            hints=("启动时加 --peer <对端QQ号>",),
        )

    def address_of(self, name: str) -> str:
        """The address to put on the wire for a local terminal name.

        Locally the name *is* the address (``alice`` -> ``alice``). On QQ the
        name stays ``alice`` for the sake of every route and the whole UI, while
        the wire needs the UIN -- sending to ``"bob"`` would be rejected by the
        transport, correctly, because there is no QQ number ``bob``.
        """
        return self.uins.get(name, name)

    @property
    def transport_name(self) -> str:
        """What the UI should call the current transport."""
        return "local" if isinstance(self.network, LoopbackNetwork) else "onebot"

    def pump(self) -> int:
        """Let a loopback network deliver what is queued.

        A real transport has nothing to pump -- its own thread delivers -- so
        this is a no-op there rather than an error: the UI calls it after every
        send without caring which mode it is in.
        """
        pump = getattr(self.network, "pump", None)
        return pump() if pump is not None else 0

    def reset(self) -> None:
        """清空会话与历史，回到“从未联系过”的状态。

        会话状态活在终端对象上，所以仅清 history 不够：残留的已建立会话会让
        下一个用例的明文消息带着旧密钥上路。这里把两边都退回 IDLE。
        """
        for name, terminal in self.terminals.items():
            terminal.session.reset()
            terminal.inbox.clear()
            self.history[name].clear()
            self.raw_history[name].clear()
            self.transcript[name].clear()
            self.outgoing[name].clear()
            self.cover_queue[name].clear()
            terminal.pending_request = None
        history = getattr(self.network, "history", None)
        if history is not None:
            history.clear()


def _terminal(hub: TerminalHub, name: str) -> Terminal:
    try:
        return hub.terminals[name]
    except KeyError:
        from .core.errors import InvalidInput

        raise InvalidInput(
            f"未知终端 {name!r}；可用：{sorted(hub.terminals)}"
        ) from None


def wire_terminal_routes(app, hub: TerminalHub, *, max_upload_bytes: int) -> None:
    """Attach the terminal endpoints. All paths live under ``/terminal``."""
    from fastapi import File, Form

    from .web import _read_upload

    async def _images(files: FileList) -> tuple[list[bytes], list[str]]:
        raws: list[bytes] = []
        names: list[str] = []
        for file in files:
            raws.append(await _read_upload(file, max_upload_bytes))
            names.append(file.filename or "")
        return raws, names

    @app.get("/terminal/state")
    def state() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            # 文本混淆走中文信道，没有模型时那条路径不可用；前端据此禁用按钮。
            "text_channel_ready": hub.text_channel_ready,
            # "local" = 两个窗口共用内存队列；"onebot" = 真的走 QQ。
            # 前端据此把标题从 alice/bob 换成两个 QQ 号。
            "transport": hub.transport_name,
            # 名字（alice/bob）是位置，QQ 号才是身份。接 QQ 时前端要能说清
            # "左边这个窗口其实是 3925697820"。
            "uins": dict(getattr(hub, "uins", {})),
            # 单账号部署时，对端不在这台机器上，只有一个 QQ 号可显示。
            # 前端据此把"对端"标成那个号，而不是画一个不存在的 bob。
            "peer_uin": getattr(hub, "peer_uin", ""),
            "terminals": {
                name: {
                    "state": terminal.state.value,
                    "state_label": terminal.state.label,
                    "established": terminal.established,
                    "peer": terminal.peer_id,
                    # 对端在等我们回包、而我们手头没图：前端要显示"等待本端
                    # 上传图片"，直到用户真的加图才自动回出去。
                    "awaiting_cover": terminal.pending_request is not None,
                    "covers": len(hub.cover_queue[name]),
                }
                for name, terminal in hub.terminals.items()
            },
        })

    @app.post("/terminal/{name}/covers")
    async def set_covers(name: str, files: FileList = File(default=[])):
        """Tell the server which pictures this side has on hand.

        The queue lives in the browser, but an automatic handshake reply has to
        be sent by the server -- so the front end pushes the queued pictures up
        here. If a handshake request is held waiting for a picture, this is what
        releases it.
        """
        terminal = _terminal(hub, name)
        raws, _names = await _images(files)
        released = hub.set_covers(name, raws)
        delivery = 0
        if released:
            delivery = 1
            METRICS.incr("terminal_handshake_released", sender=name)
        return JSONResponse({
            "ok": True,
            "covers": len(hub.cover_queue[name]),
            # 用户加图顺手把压着的握手回包发出去了，前端要说一声。
            "released": released,
            "delivered": delivery,
            "state": terminal.state.value,
            "state_label": terminal.state.label,
        })

    @app.get("/terminal/inbox/{name}")
    def inbox(name: str, after: int = 0) -> JSONResponse:
        """本端看到的完整双窗对话（自己发的 + 对方发的），从 ``after`` 起。"""
        terminal = _terminal(hub, name)
        items = hub.transcript[name][after:]
        return JSONResponse({
            "ok": True,
            "next": after + len(items),
            "state": terminal.state.value,
            "state_label": terminal.state.label,
            "established": terminal.established,
            "peer": terminal.peer_id,
            "messages": items,
        })

    @app.post("/terminal/{name}/send")
    async def send(name: str, text: str = Form(default=""),
                   mode: str = Form(default="plain"),
                   files: FileList = File(default=[])):
        terminal = _terminal(hub, name)
        # 线上地址，不是本地名字：接 QQ 时这里是 QQ 号。
        peer = hub.peer_address_of(name)
        raws, _names = await _images(files)
        # The images are exactly what the user selected, in the order they
        # selected them. Nothing here reorders, substitutes or invents a cover:
        # a covert or handshake send without pictures is refused instead.
        covert = None
        # ``wire_text`` 是"网线上真正传出去的可见文本"，不是用户键入的原文。
        # 网关视角的预览就靠它：文本混淆传出去的是混淆后的那串中文，
        # 图片混淆传出去的是用户写的公开说明（或什么都没有）。
        wire_text = text or None
        if mode == "covert-image":
            # 只从队首取够用的那几张：一句短话一张图就够，其余的留在队列里
            # 等下次。多发的图不但浪费，还会让"我到底发了几条"变得不可数。
            need = terminal.images_needed(text, tuple(raws))
            used = tuple(raws[:need])
            terminal.send_covert_in_images(peer, text, used)
            raws = used
            covert = text
            # 图片混淆没有可见文本：用户没写公开说明，明文窗就只有图。
            wire_text = None
        elif mode == "covert-text":
            # 文本混淆不消耗图片：密文藏进生成的中文文本。
            # 传出去的是混淆后的宿主文本，它才是网络上看得到的东西。
            wire_text = terminal.send_covert_text(peer, text)
            raws = ()
            covert = text
        elif mode == "handshake":
            terminal.send_handshake(peer, tuple(raws))
            # 握手图是控制报文的载体，不是"我发了张图"：记 0 张，
            # 免得聊天记录里冒出一条跟对方响应长得一样的图。
            raws = ()
        else:
            terminal.send_packet(peer, Packet(text=text or None, images=tuple(raws)))
        # 本地对话线记一笔"我发的"：聊天软件两边都要显示自己说过的话。
        own_index = hub.note_outgoing(name, mode=mode, text=wire_text,
                                     images=tuple(raws), covert=covert)
        delivered = hub.pump()
        METRICS.incr("terminal_sent", mode=mode, sender=name)
        return JSONResponse({
            "ok": True,
            "delivered": delivered,
            "state": terminal.state.value,
            "state_label": terminal.state.label,
            # 前端据此把用掉的图片从队列移入"已发送"：服务端说了算，
            # 免得两边各自猜一个数字。
            "images_used": len(raws),
            "own_index": own_index,
        })

    @app.get("/terminal/media/{name}/{index}")
    def media(name: str, index: int, image: int = 0, own: bool = False) -> Response:
        """One host image from either side of a client's transcript.

        ``index`` is the message's position in that client's timeline and
        ``image`` the picture inside it. ``own=true`` reads from what this client
        *sent*--those bytes never come back through the network, so they are kept
        separately; otherwise it reads from what it *received*. Bytes are served
        exactly as they were, and the browser does its own decoding.
        """
        from .core.errors import InvalidInput

        if name not in hub.transcript:
            raise InvalidInput(f"未知终端 {name!r}；可用：{sorted(hub.transcript)}")
        if own:
            sent = hub.outgoing[name]
            if not 0 <= index < len(sent):
                raise InvalidInput(
                    f"已发送序号 {index} 超出范围（0~{len(sent) - 1}）"
                )
            images = sent[index]
        else:
            history = hub.raw_history[name]
            if not 0 <= index < len(history):
                raise InvalidInput(
                    f"包序号 {index} 超出范围（0~{len(history) - 1}）"
                )
            _sender, packet = history[index]
            _text, images = _decode_packet(packet)
        if not 0 <= image < len(images):
            raise InvalidInput(f"图片序号 {image} 超出范围（该条有 {len(images)} 张）")
        media_type = _sniff(images[image])
        return Response(content=images[image], media_type=media_type)

    @app.get("/terminal/network/history")
    def network_history() -> JSONResponse:
        return JSONResponse(network_history_payload(hub.network))


MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_JPEG = b"\xff\xd8\xff"


def network_history_payload(network) -> dict:
    """What crossed the wire, when the transport can say.

    Only the in-memory 演示 line keeps a transcript. A real transport has no
    business retaining every packet it relays -- and ``RoutedNetwork`` has no
    such attribute at all, which used to surface as a 500 on an endpoint the UI
    polls. An empty list plus ``available: false`` is the honest answer there.
    """
    history = getattr(network, "history", ())
    return {
        "ok": True,
        # 分不清"没有记录"和"记录是空的"会让界面把真实链路显示成"什么都没发生"。
        "available": hasattr(network, "history"),
        "packets": [
            {"sender": sender, "target": target, "bytes": len(payload)}
            for sender, target, payload in history
        ],
    }


def _sniff(data: bytes) -> str:
    """Media type from magic bytes. The upload's declared type is unreliable."""
    if data.startswith(MAGIC_PNG):
        return "image/png"
    if data.startswith(MAGIC_JPEG):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _inbound_dict(inbound: Inbound) -> dict:
    return {
        "sender": inbound.sender,
        "chat_text": inbound.chat_text,
        # 图片不回传字节：UI 已在本地展示发送的原图；收到的宿主图由
        # 收包方自行渲染需要字节，这里只计数并在需要时走 /terminal/media。
        "chat_image_count": len(inbound.chat_images),
        "covert": list(inbound.covert),
        "notes": list(inbound.notes),
        "handshake": inbound.handshake,
    }


__all__ = ["HISTORY_LIMIT", "TerminalHub", "wire_terminal_routes"]
