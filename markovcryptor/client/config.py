"""Client settings: the little that must survive a restart.

Everything about *the user's conversations* stays in memory -- friends, history,
picture queue, session keys. This module holds only what is genuinely
configuration:

- which folders to draw covers from,
- how many to queue, and how to pick,
- the shared stego password,
- the OneBot endpoint of this machine's QQ.

The distinction matters and is easy to blur. "No data on disk" is about not
leaving a copy of the user's chat behind. Remembering that they pointed at
``D:/Pictures/covers`` is not chat data -- it is a preference, and asking for it
every launch would be hostile.

Location: ``%APPDATA%\\markovcryptor\\client.json``. Not next to the executable,
because that directory may be read-only (Program Files, a mounted image, a
locked-down share) and a config that cannot be written is worse than none.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..observability import event

#: Overridable so tests never touch the real user profile.
CONFIG_ENV = "MKX_CLIENT_CONFIG"


def default_config_path() -> Path:
    """``%APPDATA%\\markovcryptor\\client.json``, with an env override."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "markovcryptor" / "client.json"
    # APPDATA missing is unusual on Windows but possible in a stripped
    # environment; fall back to the home directory rather than failing.
    return Path.home() / ".markovcryptor" / "client.json"


@dataclass
class ClientConfig:
    """Everything we remember between runs."""

    #: Folders to draw cover pictures from. Empty means "cannot send handshakes",
    #: which the UI must say out loud rather than failing at send time.
    cover_folders: list[Path] = field(default_factory=list)
    max_queue: int = 8
    pick: str = "random"
    psk: str = "pbx1"
    #: 用户关掉了「你在用默认口令」这条提示。
    #:
    #: 记住而不是每次启动都弹：提示一旦变成噪音就会被无视，比不提示更糟。
    #: 只在设置页里重新出现（那里能看到当前口令）。
    psk_notice_dismissed: bool = False
    #: ``ws://host:port/`` of this machine's OneBot. Usually discovered from
    #: SnowLuma's generated config, so this is only an override.
    onebot_url: str = ""
    onebot_token: str = ""
    #: Port for the client's own HTTP server.
    #: Local port for the UI. 0 means "pick one that is free", starting from
    #: :data:`markovcryptor.client.app.DEFAULT_CLIENT_PORT`. Users can pin a
    #: specific port when something else keeps taking 8741.
    port: int = 0
    #: Set false to skip opening a browser (headless testing).
    open_browser: bool = True
    #: Where ``*.json`` text models live. Empty means the text channel is off and
    #: only picture-based covert sending works -- which is a valid configuration,
    #: not an error, so it is not auto-created.
    model_folder: Path | None = None

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, path: Path | None = None) -> "ClientConfig":
        """Read the file, or return defaults if it is missing or broken.

        A malformed config must not stop the program: the user cannot fix a JSON
        syntax error they never see. Log it and start with defaults, which the
        UI then shows as "no cover folder configured".
        """
        target = path or default_config_path()
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            event("config", "unreadable", level=logging.WARNING,
                  path=str(target), error=exc)
            return cls()

        try:
            data = json.loads(raw)
        except ValueError as exc:
            event("config", "malformed", level=logging.WARNING,
                  path=str(target), error=exc)
            return cls()
        if not isinstance(data, dict):
            return cls()

        queue = data.get("queue") if isinstance(data.get("queue"), dict) else {}
        onebot = data.get("onebot") if isinstance(data.get("onebot"), dict) else {}

        folders = [Path(str(p)).expanduser()
                   for p in (queue.get("folders") or ()) if str(p).strip()]

        pick = str(queue.get("pick", "random")).lower()
        if pick not in ("random", "sequential"):
            pick = "random"

        try:
            max_queue = max(1, int(queue.get("max_items", 8)))
        except (TypeError, ValueError):
            max_queue = 8

        try:
            port = int(data.get("port", 0) or 0)
        except (TypeError, ValueError):
            port = 0

        return cls(
            cover_folders=folders,
            max_queue=max_queue,
            pick=pick,
            psk=str(data.get("psk") or "pbx1"),
            psk_notice_dismissed=bool(data.get("psk_notice_dismissed")),
            onebot_url=str(onebot.get("url") or ""),
            onebot_token=str(onebot.get("token") or ""),
            port=port,
            open_browser=bool(data.get("open_browser", True)),
            model_folder=(Path(str(data["models"]["folder"])).expanduser()
                          if isinstance(data.get("models"), dict)
                          and str(data["models"].get("folder") or "").strip()
                          else None),
        )

    # ------------------------------------------------------------------- saving

    def save(self, path: Path | None = None) -> bool:
        """Write the file. Returns False if it could not be written.

        Never raises: a read-only location is a normal situation (portable
        install on a locked drive) and the program should keep working without
        remembering anything.
        """
        target = path or default_config_path()
        payload = {
            "queue": {
                "folders": [str(p) for p in self.cover_folders],
                "max_items": self.max_queue,
                "pick": self.pick,
            },
            "psk": self.psk,
            "psk_notice_dismissed": self.psk_notice_dismissed,
            "onebot": {"url": self.onebot_url, "token": self.onebot_token},
            "port": self.port,
            "open_browser": self.open_browser,
            "models": {"folder": str(self.model_folder) if self.model_folder else ""},
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            return True
        except OSError as exc:
            event("config", "unwritable", level=logging.WARNING,
                  path=str(target), error=exc)
            return False

    # -------------------------------------------------------------------- views

    def usable_cover_folders(self) -> list[Path]:
        """Only folders that exist and contain at least one image.

        Checked up front so the UI can distinguish "you configured nothing" from
        "the folder you configured has no pictures in it" -- two very different
        things to tell a user.
        """
        from .queue import _IMAGE_EXTS

        usable: list[Path] = []
        for folder in self.cover_folders:
            try:
                if not folder.is_dir():
                    event("config", "cover_folder_missing",
                          level=logging.WARNING, folder=str(folder))
                    continue
                with os.scandir(folder) as entries:
                    has_image = any(
                        e.is_file() and Path(e.name).suffix.lower() in _IMAGE_EXTS
                        for e in entries)
            except OSError as exc:
                event("config", "cover_folder_unreadable", level=logging.WARNING,
                      folder=str(folder), error=exc)
                continue
            if has_image:
                usable.append(folder)
            else:
                event("config", "cover_folder_empty",
                      level=logging.WARNING, folder=str(folder))
        return usable
