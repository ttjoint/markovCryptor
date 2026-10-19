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
from typing import List

from fastapi import UploadFile
from fastapi.responses import JSONResponse, Response

from .observability import METRICS
from .terminal import LoopbackNetwork, Packet, Terminal
from .terminal.covers import CoverLibrary
from .terminal.terminal import Inbound

#: Module-level alias: with ``from __future__ import annotations`` every
#: annotation is a string, and pydantic resolves those against module globals --
#: a name defined inside ``wire_terminal_routes`` would stay unresolved.
FileList = List[UploadFile]

#: How many recent conversations each window remembers server-side. The UI
#: renders what it receives live; this is only so a page reload can show
#: something sensible instead of an empty window.
HISTORY_LIMIT = 200


class TerminalHub:
    """One pair of terminals sharing one loopback network, plus their history."""

    def __init__(self, cover_dir: str | None = None, psk: str = "pbx1") -> None:
        self.network = LoopbackNetwork()
        self.covers = CoverLibrary(cover_dir)
        self.terminals: dict[str, Terminal] = {}
        for name in ("alice", "bob"):
            terminal = Terminal(name, self.network, psk=psk,
                                covers=self.covers).attach()
            self.terminals[name] = terminal
        self.history: dict[str, list[Inbound]] = {name: [] for name in self.terminals}
        #: 收到的每个包的原始字节，按到达顺序。UI 重载后要重放宿主图，
        #: 而 Inbound 里只有解码后的 PIL 图；存原始包是最简单的回放来源。
        self.raw_history: dict[str, list[tuple[str, bytes]]] = {
            name: [] for name in self.terminals
        }
        for name, terminal in self.terminals.items():
            terminal.on_packet = (
                lambda sender, packet, n=name: self.raw_history[n].append((sender, packet))
            )
            terminal.subscribe(self._collector(name))

    def _collector(self, name: str):
        def sink(inbound: Inbound) -> None:
            bucket = self.history[name]
            bucket.append(inbound)
            if len(bucket) > HISTORY_LIMIT:
                del bucket[: len(bucket) - HISTORY_LIMIT]
        return sink

    def pump(self) -> int:
        return self.network.pump()

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
        self.network.history.clear()


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
            "covers": hub.covers.describe(),
            "terminals": {
                name: {
                    "state": terminal.state.value,
                    "state_label": terminal.state.label,
                    "established": terminal.established,
                    "peer": terminal.peer_id,
                }
                for name, terminal in hub.terminals.items()
            },
        })

    @app.get("/terminal/inbox/{name}")
    def inbox(name: str, after: int = 0) -> JSONResponse:
        terminal = _terminal(hub, name)
        items = hub.history[name][after:]
        # history 与 raw_history 同步追加（collector 和 on_packet 都在
        # deliver 的调用链上），所以下标一一对应；media 端点靠它找字节。
        messages = []
        for offset, item in enumerate(items):
            entry = _inbound_dict(item)
            entry["packet_index"] = after + offset
            messages.append(entry)
        return JSONResponse({
            "ok": True,
            "next": after + len(items),
            "state": terminal.state.value,
            "state_label": terminal.state.label,
            "messages": messages,
        })

    @app.post("/terminal/{name}/send")
    async def send(name: str, text: str = Form(default=""),
                   mode: str = Form(default="plain"),
                   files: FileList = File(default=[])):
        terminal = _terminal(hub, name)
        peer = _other(hub, name)
        raws, _names = await _images(files)
        if mode == "covert":
            if not raws:
                from .core.errors import InvalidInput

                raise InvalidInput(
                    "密文发送至少需要一张图片：密文藏在图片里",
                    hints=("先在预览栏添加图片，再点发送",),
                )
            terminal.send_covert_in_images(peer, text, tuple(raws))
        elif mode == "handshake":
            terminal.send_handshake(peer, tuple(raws) or (hub.covers.cover_for(38),))
        else:
            terminal.send_packet(peer, Packet(text=text or None, images=tuple(raws)))
        delivered = hub.pump()
        METRICS.incr("terminal_sent", mode=mode, sender=name)
        return JSONResponse({
            "ok": True,
            "delivered": delivered,
            "state": terminal.state.value,
            "state_label": terminal.state.label,
        })

    @app.get("/terminal/media/{name}/{index}")
    def media(name: str, index: int, image: int = 0) -> Response:
        """One received host image, so a reloaded page can rebuild its windows.

        ``index`` is the packet's position in the raw history and ``image`` the
        picture inside that packet. Bytes are served exactly as they arrived;
        the browser does its own decoding.
        """
        from .core.errors import InvalidInput

        if name not in hub.raw_history:
            raise InvalidInput(f"未知终端 {name!r}；可用：{sorted(hub.raw_history)}")
        history = hub.raw_history[name]
        if not 0 <= index < len(history):
            raise InvalidInput(
                f"包序号 {index} 超出范围（0~{len(history) - 1}）"
            )
        _sender, packet = history[index]
        from .terminal.terminal import _decode_packet

        _text, images = _decode_packet(packet)
        if not 0 <= image < len(images):
            raise InvalidInput(f"图片序号 {image} 超出范围（该包有 {len(images)} 张）")
        media_type = _sniff(images[image])
        return Response(content=images[image], media_type=media_type)

    @app.get("/terminal/network/history")
    def network_history() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "packets": [
                {"sender": sender, "target": target, "bytes": len(payload)}
                for sender, target, payload in hub.network.history
            ],
        })


def _other(hub: TerminalHub, name: str) -> str:
    from .core.errors import InvalidInput

    others = [n for n in hub.terminals if n != name]
    if not others:
        raise InvalidInput("只有一个终端，没有可发送的对端")
    return others[0]


MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_JPEG = b"\xff\xd8\xff"


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
