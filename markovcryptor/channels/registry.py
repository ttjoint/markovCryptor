"""Channel registry: the single place that knows which algorithms exist.

Registration is **explicit and ordered** rather than discovered by scanning the
package. Two reasons: the CLI's flag list and the web UI's menu should be stable
across runs, and an import-time scan would make a typo in an optional channel
break every entry point.

New channels (video, live streams) register here and immediately appear in
``GET /channels``, the CLI and the web form, with no front-end change.
"""

from __future__ import annotations

from typing import Callable

from ..core.errors import InvalidInput
from .protocol import Channel, describe

_REGISTRY: dict[str, Channel] = {}


def register(channel: Channel) -> Channel:
    """Add a channel. Re-registering an id replaces it (useful for tests)."""
    if not getattr(channel, "id", ""):
        raise InvalidInput("信道必须有非空 id")
    _REGISTRY[channel.id] = channel
    return channel


def get(channel_id: str) -> Channel:
    """Look up by id, listing what *is* available when the id is unknown."""
    try:
        return _REGISTRY[channel_id]
    except KeyError:
        available = ", ".join(_REGISTRY) or "（无）"
        raise InvalidInput(
            f"未知信道 {channel_id!r}；可用信道：{available}",
            hints=(f"可用信道：{available}",),
        ) from None


def all_channels() -> tuple[Channel, ...]:
    """Registered channels in registration order."""
    return tuple(_REGISTRY.values())


def describe_all() -> list[dict]:
    """JSON-safe description of every channel, for the API and the CLI."""
    return [describe(channel) for channel in _REGISTRY.values()]


def register_defaults(model_provider: Callable[[], object] | None = None) -> tuple[Channel, ...]:
    """Register the built-in channels.

    The Chinese channel needs a *trained model*, which is user data rather than
    package data. It is therefore registered only when a provider is supplied --
    at CLI startup when a model is loaded, or never for a pure image workflow.
    That keeps ``import markovcryptor.channels`` free of any model IO.
    """
    from .gif import GifChannel
    from .image import ImageChannel

    register(ImageChannel())
    register(GifChannel())

    if model_provider is not None:
        from ..codec import MarkovStegoCodec
        from .chinese import ChineseChannel

        model = model_provider()
        if model is not None:
            register(ChineseChannel(MarkovStegoCodec(model)))

    return all_channels()


__all__ = ["register", "get", "all_channels", "describe_all", "register_defaults"]
