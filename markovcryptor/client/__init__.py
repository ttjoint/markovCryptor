"""The client: one QQ account, many conversations.

This package is separate from ``markovcryptor.web_terminal`` on purpose. That
one is the *test rig* -- two terminals side by side, both addresses typed in by
hand, and its docstring says plainly that it demonstrates a feature rather than
serving users. Here the same protocol layer is driven by a friend list instead.

What is shared and stays untouched: ``terminal/`` (protocol, session,
transport), ``channels/``, ``covers/``. Nothing in this package reimplements
cryptography; if something here needs to encrypt, it calls the same code the
test rig does.

Memory-only by design. Friend lists, history and picture queues live in Python
objects and die with the process. See ``docs/CLIENT.md`` §5 for why.
"""

from __future__ import annotations

__all__ = ["account", "conversations", "directory", "messages", "queue"]
