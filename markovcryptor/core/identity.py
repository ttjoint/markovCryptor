"""握手用的身份绑定与指纹（TOFU）。

## 为什么要这一层

握手本身是 X25519，**没有身份认证**。能改写消息流的人可以分别和两端握手，
全程解密再转发，双方都以为在做端到端加密。这在 `handshake.py` 的 docstring
里写作 "textbook unauthenticated DH" —— 但代码里一直没有真正实现它提到的
那个 MAC。

这里补两件事：

1. **帧内 MAC**：握手载荷从 32 字节变成 ``pubkey(32) || mac(16)``。
   MAC 覆盖对端公钥和双方会话角色，用共享口令派生。它保证「握手帧不是随便
   谁塞进来的」—— 不知道口令的人无法伪造。

   但**它防不住知道口令的人**：口令是共享的，不是身份。所以还需要第二层。

2. **TOFU 指纹**（Trust On First Use）：首次见到某人的公钥就记住它的指纹，
   之后每次比对。指纹变了就大声警告 —— 可能是对方重装了、也可能是中间人。
   这是 Signal / WhatsApp 的做法，也是唯一能真正发现 MITM 的手段。

## 指纹不是秘密

公钥本来就是公开的。指纹只是它的短哈希，用来让人**肉眼比对**
（"你在电话里念一下那 8 个字符"），所以它不是密钥，明文存储即可。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
from pathlib import Path

from .keys import derive
from ..observability import event

#: X25519 公钥长度。不 import image_stego 的同名常量：那会形成
#: identity -> image_stego -> keys 的环，而这层只需要这一个数字。
PUBLIC_KEY_BYTES = 32

#: 握手载荷长度：公钥 + MAC。
HANDSHAKE_PAYLOAD_BYTES = PUBLIC_KEY_BYTES + 16

#: MAC 域标签。改动它等于换协议，会让旧握手帧全部失效。
MAC_DOMAIN = b"mkx1-handshake-mac-v2\x00"


def _mac_key(psk: bytes) -> bytes:
    """Derive a MAC key so the same secret is never used for two purposes."""
    return derive(psk, "handshake-mac")


def build_handshake_payload(public: bytes, peer_hint: bytes, psk: bytes) -> bytes:
    """``public || MAC(public || role)``.

    ``peer_hint`` 是「这条帧是发给谁的」的一个可复现上下文（这里用发送方
    自己的公钥做角色隔离，见 :func:`verify_handshake_payload`）。它让同一个
    口令在不同会话里派生出不同的 MAC，避免一帧被原样重放到别的会话。
    """
    if len(public) != PUBLIC_KEY_BYTES:
        raise ValueError(f"公钥必须是 {PUBLIC_KEY_BYTES} 字节")
    mac = hmac.new(_mac_key(psk), MAC_DOMAIN + public + peer_hint,
                   hashlib.sha256).digest()[:16]
    return public + mac


def verify_handshake_payload(payload: bytes, peer_hint: bytes,
                             psk: bytes) -> bytes:
    """Return the public key, or raise if the MAC does not check out.

    The caller must not proceed with an unverified key: proceeding is exactly
    the unauthenticated-DH hole this function exists to close.
    """
    if len(payload) != HANDSHAKE_PAYLOAD_BYTES:
        raise ValueError(
            f"握手载荷长度异常：{len(payload)}（应为 {HANDSHAKE_PAYLOAD_BYTES}）")
    public, mac = payload[:PUBLIC_KEY_BYTES], payload[PUBLIC_KEY_BYTES:]
    expected = hmac.new(_mac_key(psk), MAC_DOMAIN + public + peer_hint,
                        hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise ValueError("握手帧校验失败：对方口令与你不同，或帧被篡改")
    return public


# ------------------------------------------------------------------ 指纹

def fingerprint(public: bytes) -> str:
    """A short, human-comparable fingerprint (SHA-256, first 8 bytes, hex).

    8 字节 = 16 个十六进制字符。比对时前 8 位就够（64 bit 碰撞概率可以忽略），
    完整串放在 title 里给较真的人看。
    """
    return hashlib.sha256(b"mkx1-fp\x00" + public).hexdigest()[:16]


class KnownPeers:
    """TOFU 指纹册。

    **这是全项目唯一持久化的东西**（除了用户配置）。理由：TOFU 的全部
    价值就在"记住上次见到的是谁"，内存里存等于每次重启都重新信任，
    中间人只需等你重启。

    存的是**指纹**不是密钥，也不含任何消息内容。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._known: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 指纹册损坏不能挡住启动；但要说出来，否则用户以为验证还在生效。
            event("security", "peers_unreadable", level=logging.WARNING,
                  path=str(self.path), error=exc)
            return
        peers = data.get("peers") if isinstance(data, dict) else None
        if isinstance(peers, dict):
            self._known = {str(k): str(v) for k, v in peers.items()}

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再替换：中途断电不能把指纹册写成半截 JSON，
            # 那会让所有对端都变成"首次见面"，静默丢掉全部验证历史。
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "peers": self._known},
                              handle, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            event("security", "peers_unwritable", level=logging.WARNING,
                  path=str(self.path), error=exc)

    def check(self, peer: str, public: bytes) -> tuple[str, str]:
        """Compare ``public`` against what we remembered.

        Returns ``(status, fingerprint)`` where status is one of:

        * ``"first"`` —— 第一次见，已记住（没有可比对的旧值）
        * ``"same"``  —— 和记忆一致，正常
        * ``"changed"`` —— **变过**，可能是重装，也可能是中间人
        """
        current = fingerprint(public)
        previous = self._known.get(str(peer))
        if previous is None:
            self._known[str(peer)] = current
            self._save()
            event("security", "peer_first_seen", peer=peer, fingerprint=current)
            return "first", current
        if previous == current:
            return "same", current
        event("security", "peer_fingerprint_changed", level=logging.WARNING,
              peer=peer, was=previous, now=current)
        return "changed", current

    def trust(self, peer: str, public: bytes) -> str:
        """User said "yes, this really is them". Adopt the new fingerprint."""
        current = fingerprint(public)
        self._known[str(peer)] = current
        self._save()
        event("security", "peer_trusted", peer=peer, fingerprint=current)
        return current

    def known(self, peer: str) -> str | None:
        return self._known.get(str(peer))

    def forget(self, peer: str) -> bool:
        had = self._known.pop(str(peer), None) is not None
        if had:
            self._save()
        return had
