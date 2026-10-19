"""生成 SnowLuma 的 OneBot 配置（``config/onebot_<UIN>.json``）。

SnowLuma 是外部进程，本库不改它一行代码，只负责写它的配置文件。放在库里
而不是只放在 ``tools/``，是因为装机后 ``tools/`` 未必跟着走 —— 用
``python -m markovcryptor.cli qq-init`` 就能配，比记脚本路径省事。

两个字段是一开始就要对的，错了不报错只是行为不对：

``messageFormat: "array"``
    消息段用数组，图片才能带结构。

``reportSelfMessage: false``
    自己发的消息不推回来。终端只把"收到的包"当对端来的，自己发的若回显，
    会变成自己跟自己握手。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: 一个账号要考虑的四个端口。同机挂两个号时必须分开，否则第二个号
#: ``EADDRINUSE`` 起不来（实测踩过）。
DEFAULT_WS_PORT = 9711
DEFAULT_HTTP_PORT = 9712


@dataclass(frozen=True)
class OneBotSettings:
    """写进 `onebot_<uin>.json` 的取值。"""

    uin: str
    ws_port: int = DEFAULT_WS_PORT
    http_port: int = DEFAULT_HTTP_PORT
    host: str = "127.0.0.1"
    access_token: str = ""

    @property
    def token(self) -> str:
        return self.access_token or f"mkx-{self.uin}"

    @property
    def ws_url(self) -> str:
        """本机连接用的地址。监听 0.0.0.0 时用回环地址——那不叫能连的地址。"""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"ws://{host}:{self.ws_port}/"


def build_config(settings: OneBotSettings) -> dict:
    """拼出整份 OneBot 配置。"""
    token = settings.token
    return {
        "mode": "snapshot",
        "networks": {
            "httpServers": [{
                "name": "http-default",
                "accessToken": token,
                "messageFormat": "array",
                "reportSelfMessage": False,
                "host": settings.host,
                "port": settings.http_port,
                "path": "/",
                "enableWebSocket": False,
            }],
            "httpClients": [],
            "wsServers": [{
                "name": "ws-default",
                "accessToken": token,
                "messageFormat": "array",
                "reportSelfMessage": False,
                "host": settings.host,
                "port": settings.ws_port,
                "path": "/",
                "role": "Universal",
            }],
            "wsClients": [],
        },
        "statusCommand": {"enabled": False, "swallow": False,
                          "cooldownSeconds": 5, "trigger": "#sl"},
        "historySync": {"enabled": False},
        "notifications": {"channelIds": []},
    }


def config_path(snowluma: Path, uin: str) -> Path:
    return Path(snowluma) / "config" / f"onebot_{uin}.json"


def write_config(snowluma: Path, settings: OneBotSettings, *,
                 force: bool = False) -> tuple[Path, bool]:
    """写配置。返回 ``(路径, 是否写入)``；已存在且未 ``force`` 时保留原文件。

    保留是有意的：SnowLuma 可能在配置里补字段，重跑不该抹掉。
    """
    directory = Path(snowluma) / "config"
    if not directory.is_dir():
        raise FileNotFoundError(f"{snowluma} 下没有 config/ 目录，--snowluma 要指向已解压的 release")

    path = config_path(snowluma, settings.uin)
    if path.exists() and not force:
        return path, False
    path.write_text(json.dumps(build_config(settings), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path, True


def read_settings(path: Path) -> OneBotSettings | None:
    """从已有配置里读回取值，用于 ``status`` / ``probe`` 这类无需再问的场景。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = (data.get("networks") or {}).get("wsServers") or []
    if not servers:
        return None
    server = servers[0]
    http = ((data["networks"].get("httpServers") or [{}])[0]).get("port", DEFAULT_HTTP_PORT)
    uin = Path(path).stem.removeprefix("onebot_")
    # 把"写出去时补上的默认 token"归一回空值，这样读写是对称的。
    token = str(server.get("accessToken", ""))
    return OneBotSettings(
        uin=uin,
        ws_port=int(server.get("port", DEFAULT_WS_PORT)),
        http_port=int(http),
        host=str(server.get("host", "127.0.0.1")),
        access_token="" if token == f"mkx-{uin}" else token,
    )


def discover(snowluma: Path) -> dict[str, OneBotSettings]:
    """列出 ``config/onebot_*.json`` 里的所有账号，按 UIN 排序。"""
    found = {}
    for path in sorted((Path(snowluma) / "config").glob("onebot_*.json")):
        settings = read_settings(path)
        if settings is not None:
            found[settings.uin] = settings
    return found
