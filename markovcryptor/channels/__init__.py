"""Channels: every steganography algorithm behind one uniform interface.

The contract is three operations -- :meth:`Channel.probe` / :meth:`Channel.embed`
/ :meth:`Channel.extract` -- plus metadata rich enough to build a UI from. Options
stay channel-specific (:mod:`options`) so an adapter never grows an ``if`` ladder.

Current channels:

* ``image``    DCT-QIM in a still photo, blind extraction (``image_stego``)
* ``gif``      the same inside one frame of an animation (``gif_stego``)
* ``chinese``  Markov chain + arithmetic coding into Chinese prose (``codec``)

The last one is registered only when a trained model is available, because a
model is user data; importing this package never touches the filesystem.
"""

from .base import Carrier, CarrierKind, EmbedReport, Envelope, Feasibility
from .protocol import Channel, describe

__all__ = [
    "Carrier", "CarrierKind", "EmbedReport", "Envelope", "Feasibility",
    "Channel", "describe",
]
