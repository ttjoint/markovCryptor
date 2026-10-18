"""Point-to-point steganographic terminal.

The protocol layers, in dependency order:

``protocol``  the PBX1 envelope that rides inside an MKX1 frame
``session``   per-peer X25519 session state machine
``transport`` a ``Network`` abstraction plus an in-process implementation
``terminal``  the terminal: sends packets, inspects incoming ones, routes results

Nothing here imports a UI. The Tk/CLI front ends and the tests drive
:class:`~markovcryptor.terminal.terminal.Terminal` through the same API, which is
what makes "verify feasibility without a GUI" possible.
"""

from __future__ import annotations

from .protocol import Kind, Mode
from .session import Session, State
from .terminal import Inbound, Packet, Terminal
from .transport import LoopbackNetwork, Network

__all__ = [
    "Inbound", "Kind", "LoopbackNetwork", "Mode", "Network", "Packet",
    "Session", "State", "Terminal",
]
