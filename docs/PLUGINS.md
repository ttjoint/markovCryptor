"""How to add a channel.

A channel is three operations and some metadata (``channels/protocol.py``).
Nothing else is required, and no existing file needs editing except one line in
``register_defaults`` (or a call to ``registry.register`` from your own entry
point).

This document is executable: the channel below lives in
``tests/test_plugins.py`` as a real, tested implementation. If the protocol
changes, that test fails rather than this file quietly going stale.

- [1. The minimum](#1-the-minimum)
- [2. What each member is for](#2-what-each-member-is-for)
- [3. Rules that are easy to get wrong](#3-rules-that-are-easy-to-get-wrong)
- [4. Registering](#4-registering)
- [5. Checklist](#5-checklist)
"""

# --

## 1. The minimum

```python
from typing import Any

from markovcryptor.channels.base import (
    Carrier, Envelope, EmbedReport, Feasibility,
)


class LsbChannel:
    """Hide bytes in the low bit of each colour sample."""

    id = "lsb"
    display_name = "图片（LSB）"
    summary = "改每个像素的最低位；容量大但一碰就坏，只能走无损文件"
    mime_in = ("image/png", "image/bmp")
    mime_out = "image/png"
    options_schema = None          # 没有可选参数
    concurrency_hint = 4

    def probe(self, cover: Carrier, payload_bytes: int) -> Feasibility:
        ...

    def embed(self, cover, payload, key, options=None, progress=None):
        ...

    def extract(self, carrier, key, options=None, progress=None):
        ...
```

That is the whole contract. ``isinstance(channel, Channel)`` is ``True``
structurally -- no base class, no registration decorator, no plugin metadata
file.

---

## 2. What each member is for

### `id` / `display_name` / `summary`

`id` is what appears in URLs and CLI flags, so it must be stable and
lower-case. `display_name` is what a person reads in a menu. `summary` is a
**one-line trade-off statement**, shown as a form hint -- "容量大但一碰就坏"
tells the user when *not* to use it, which is more useful than "LSB 隐写".

### `mime_in` / `mime_out`

`mime_in` is used to reject a wrong carrier early (`415`, not a confusing
failure later). `mime_out` tells the caller what extension to save.

**Do not sniff content here.** `mime_in` describes what you accept, not what
you detect. The caller states the kind; if it lied, `load` fails loudly.

### `options_schema`

A class with `model_json_schema()` (any Pydantic model qualifies). The web UI
generates its form from this, and the CLI's `channels` prints it. Returning
`None` means "no options".

Use standard JSON Schema shapes only -- `string`, `number`, `integer`,
`boolean`, and `anyOf: [{...}, {type: null}]` for a nullable value.
`tests/test_webui.py` fails if you declare something the form generator cannot
render, because a silently-dropped option means the user embeds with a default
they never chose.

### `probe`

Answer "can this carrier hold `payload_bytes`?" **without embedding**.

- Must **not** raise when the answer is "no". Return `Feasibility(ok=False, ...)`.
- Must fill `reasons` with the true cause and `hints` with a next step.
- Must avoid loading the carrier when the answer is knowable from metadata.
  `probe` is a cheap pre-flight check; if it costs as much as `embed`, callers
  will stop using it.

### `embed`

Takes **raw bytes** and returns an `Envelope`.

Payload encoding (compression, encryption, arithmetic coding, model selection)
belongs to `Engine`, not to a channel. That split is what lets every channel
reuse the whole message pipeline. If your channel starts importing `codec` or
`core.crypto`, something is in the wrong place.

### `extract`

Recover the payload from the carrier alone.

**Blind extraction is the goal**: no original carrier, no side channel telling
you the payload length. The image channel achieves this by deriving the block
order from the key and putting a length field in a self-describing header.

If your algorithm genuinely cannot do it blind, say so in `summary` -- a user
who assumes blindness and then discovers they must keep the original is worse
off than one who knew up front.

---

## 3. Rules that are easy to get wrong

**Never collapse the cause into a generic phrase.** Bad:
`raise CapacityExceeded("无法稳定承载数据")`. Good: the real numbers plus what to try.
`reasons` is shown verbatim to the user; it is the only thing that makes
failures fixable.

**Pick the right error class.** The class decides the HTTP status, and the API's
callers discriminate on `code`:
`InvalidInput` (422) / `UnsupportedCarrier` (415) / `CapacityExceeded` (413) /
`BadKey` (401) / `NoHiddenFrame` (404) / `CarrierDamaged` (422) /
`ChannelUnavailable` (503).

A wrong key and a wrong algorithm are different errors. Do not report "not
found" for both -- the caller cannot decide whether to retry.

**Do not mutate the caller's data.** `Carrier.load` returns bytes; work on a
copy. The GIF channel re-encodes frames and must leave the source untouched
because the same carrier is often probed and embedded in one session.

**Respect `progress` and cancellation.** Long loops should call
`progress(current, total)` and let `Cancelled` propagate. A batch of 15 000
files is unusable if one item cannot be stopped.

**Be honest about `concurrency_hint`.** It is how you say "my work holds the
GIL" or "I re-encode every frame, so 2 is plenty". Do not return 64 because
more threads feel faster.

---

## 4. Registering

For a built-in channel, add two lines to `register_defaults()` in
`channels/registry.py`, keeping the order stable (it is visible in CLI listings
and UI menus).

For an external channel, register at your own entry point:

```python
from markovcryptor.channels import registry
from mypackage import LsbChannel

registry.register(LsbChannel())

engine = Engine()           # now engine.channel("lsb") works
```

No other change is needed: the channel immediately appears in `GET /channels`,
the CLI's `channels` subcommand, the web UI's menu and form, and can be used by
`batch` and `POST /jobs`.

**Registration is explicit, not scanned.** Import-time discovery would let a
typo in an optional channel break every entry point.

---

## 5. Checklist

- [ ] `id` stable, lower-case, not colliding with `image` / `gif` / `chinese`
- [ ] `summary` states the trade-off, not just the technique
- [ ] `mime_in` rejects what you cannot read
- [ ] `probe` never raises for a bad fit, and never loads when metadata suffices
- [ ] `probe`'s `reasons` contain real numbers and `hints` a concrete next step
- [ ] `embed` takes and returns bytes only -- no message encoding
- [ ] `extract` is blind, or `summary` says it is not
- [ ] errors use the specific class, not `StegoError` for everything
- [ ] `progress` called in long loops; `Cancelled` propagates
- [ ] `concurrency_hint` reflects reality
- [ ] a test round-trips one carrier and asserts a wrong key fails distinctly
