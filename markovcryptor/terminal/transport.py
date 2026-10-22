"""Transport: how a packet gets from one terminal to another.

A terminal never knows whether it is talking to an in-process queue, a file drop,
or a socket. It hands a packet to a :class:`Network` and receives packets through
``Terminal.deliver``.

The in-process implementation is not just a test stub -- it is the reference
semantics. Anything a real transport adds (ordering, loss, delay) is a deviation
from this, so starting with the simplest correct thing keeps the protocol honest.
"""

from __future__ import annotations

from typing import Callable, Protocol


class TransportError(RuntimeError):
    """The packet could not be handed to the peer (unknown id, closed link, ...).

    Carries ``hints`` like :class:`~markovcryptor.core.errors.StegoError`, so the
    CLI and the web layer can print one actionable line under the error instead
    of making the user guess. Without it, "OneBot 还没连上" reached the user as
    ``TypeError: takes no keyword arguments`` -- a crash in the error path, which
    is the worst place to have one.
    """

    def __init__(self, message: str = "", *, hints: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.hints = tuple(hints)


class Network(Protocol):
    """Delivers packets between terminals addressed by id."""

    def send(self, sender: str, target: str, packet: bytes) -> None:
        """Queue ``packet`` from ``sender`` for ``target``.

        Raises :class:`TransportError`.  ``sender`` is passed rather than
        remembered so a single network can serve many terminals without any
        "current speaker" state on it.
        """
        ...


class LoopbackNetwork:
    """In-process network with an explicit deliver step.

    Delivery is *not* automatic: the test (or the demo) calls :meth:`pump` so it
    controls when each message is processed. That makes every scenario
    reproducible -- there is no background thread deciding the order -- and is
    what lets "what did the log say" be asserted exactly.
    """

    def __init__(self) -> None:
        self._sinks: dict[str, Callable[[str, bytes], None]] = {}
        self._queue: list[tuple[str, str, bytes]] = []
        #: Every packet ever sent, in order, for tests and the demo transcript.
        self.history: list[tuple[str, str, bytes]] = []

    def register(self, address: str, sink: Callable[[str, bytes], None]) -> None:
        """``sink(sender, payload)`` -- the network is the only thing that knows
        who sent a packet, so it passes the sender along."""
        self._sinks[address] = sink

    def send(self, sender: str, target: str, packet: bytes) -> None:
        if target not in self._sinks:
            raise TransportError(
                f"未知的对端 id：{target!r}；已注册：{sorted(self._sinks)}"
            )
        self.history.append((sender, target, packet))
        self._queue.append((sender, target, packet))

    def pump(self, *, max_rounds: int = 64) -> int:
        """Deliver queued packets until quiet. Returns packets delivered.

        A packet can produce a reply (a handshake request produces a response),
        so this keeps going until the queue drains. ``max_rounds`` guards against
        a protocol bug turning into an infinite loop inside a test.
        """
        delivered = 0
        for _ in range(max_rounds):
            if not self._queue:
                return delivered
            batch, self._queue = self._queue, []
            for sender, target, packet in batch:
                self._sinks[target](sender, packet)
                delivered += 1
        raise TransportError(
            f"pump 在 {max_rounds} 轮后仍未排空（{len(self._queue)} 个包待投递）；"
            "多半是两边在无限互相回复"
        )


__all__ = ["Network", "LoopbackNetwork", "TransportError"]
