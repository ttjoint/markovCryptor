"""Session state machine for one peer.

The cryptography (X25519, HKDF direction split) already exists in
``core/crypto.py``. What a terminal additionally needs is the *rules*: when may
a handshake start, when must we reply, when is it legal to send ciphertext. Those
rules are here so that both the UI and the tests ask the same object instead of
each re-deriving them from flags.

States and the transitions that matter:

``IDLE``        no session. Ciphertext sending is impossible, and the UI must
                say so rather than let the user try and fail later.
``WAITING``     we sent a request; until a response arrives there is no session.
                Notably we do NOT hold the peer's key yet, so nothing can be
                decrypted.
``ESTABLISHED`` we have the peer's key and know our own. Both directions work.

Why ``WAITING`` exists rather than deriving keys as soon as we send our request:
until the response comes back we cannot compute a shared secret at all, so a
"half open" state that claims to be usable would be a lie. Keeping it explicit
means ``established`` is exactly "crypto is possible".
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from enum import Enum

from ..core import crypto
from ..core.errors import BadKey, InvalidInput
from .protocol import PUBLIC_KEY_BYTES, Kind, Mode


class State(str, Enum):
    IDLE = "idle"
    WAITING = "waiting"
    ESTABLISHED = "established"

    @property
    def label(self) -> str:
        return {
            State.IDLE: "未建立",
            State.WAITING: "等待对端回复",
            State.ESTABLISHED: "已建立",
        }[self]


@dataclass
class Session:
    """Per-peer key material plus the state needed to interpret it."""

    state: State = State.IDLE
    #: Our current ephemeral key pair. Private: never leaves this object.
    _private_key: object | None = field(default=None, repr=False)
    my_public: bytes = b""
    peer_public: bytes = b""
    send_key: bytes = b""
    recv_key: bytes = b""
    transcript: bytes = b""

    #: Set once a session completes: guards against a second handshake silently
    #: replacing the keys under an open conversation.
    generation: int = 0

    #: 当前这套密钥是什么时候建立的（``time.monotonic()``）。
    #:
    #: 界面要显示"会话已建立 12 分钟" —— 密钥年龄是真实的安全信号：会话
    #: 越长，同一套密钥暴露的窗口越大，用户据此决定要不要换。自动定期换钥
    #: 是反效果的（每次都要对方确认，对方不在线就卡住），所以只提示不决策。
    key_since: float = 0.0
    #: 发起握手请求的时刻，用于两段式超时提示。
    requested_at: float = 0.0
    #: 我们**主动发起**过、且还没被解决的握手。
    #:
    #: 双方同时发起时要用它判断"我是不是也在等对方" —— 只看
    #: waiting/peer_public 不够，因为接受方也会填 peer_public。
    outgoing_request: bool = False

    #: --- Rekey staging -------------------------------------------------
    #: The protocol is stateless: the peer may start a new handshake at any
    #: moment, with a brand-new key. If that immediately replaced the live keys,
    #: one unlucky rekey would take down a working channel -- and it did: when
    #: the responder had no cover picture queued, *both* sides ended up in
    #: WAITING and neither could send. So a rekey is staged here and only swapped
    #: in once the new exchange actually completes.
    _staged_private_key: object | None = field(default=None, repr=False)
    _staged_my_public: bytes = b""
    _staged_peer_public: bytes = b""
    #: Prepared response material; committed only after the packet is sent.
    _staged_outgoing: tuple | None = field(default=None, repr=False)

    @property
    def established(self) -> bool:
        return self.state is State.ESTABLISHED

    @property
    def key_age(self) -> float:
        """这套密钥用了多少秒。未建立时为 0。"""
        if not self.established or not self.key_since:
            return 0.0
        return max(0.0, time.monotonic() - self.key_since)

    @property
    def waiting_for(self) -> float:
        """已经等了对方多少秒（仅 WAITING 状态有意义）。"""
        if self.state is not State.WAITING or not self.requested_at:
            return 0.0
        return max(0.0, time.monotonic() - self.requested_at)

    def reset(self) -> None:
        """Forget everything, including the ephemeral private key."""
        self.state = State.IDLE
        self._private_key = None
        self.my_public = b""
        self.peer_public = b""
        self.send_key = b""
        self.recv_key = b""
        self.transcript = b""
        self._clear_staging()

    def _clear_staging(self) -> None:
        self._staged_private_key = None
        self._staged_my_public = b""
        self._staged_peer_public = b""
        self._staged_outgoing = None

    @property
    def rekeying(self) -> bool:
        """True while a rekey is staged but not yet complete.

        The live session keeps working throughout -- this only tells the UI that
        a key change is in flight.
        """
        return bool(self._staged_private_key or self._staged_peer_public
                    or self._staged_outgoing)

    # ------------------------------------------------------------ transitions

    def begin(self) -> tuple[bytes, Mode]:
        """Start a handshake; returns ``(our_public, REQUEST)``.

        Always generates a fresh key pair: a replayed old handshake image then
        derives a key the other side never agreed to, and every body message
        fails authentication instead of decrypting into nonsense.

        **When a session is already established this stages the new key pair
        instead of installing it.** The live keys stay in place, so the channel
        keeps carrying messages while the rekey is in flight; they are swapped
        only when the peer's response arrives (see :meth:`absorb`). Without that,
        starting a rekey would silently break the very channel it is meant to
        refresh.
        """
        private_key = crypto.generate_keypair()
        public = crypto.public_bytes(private_key)

        if self.established:
            self._staged_private_key = private_key
            self._staged_my_public = public
            return public, Mode.REQUEST

        self._private_key = private_key
        self.my_public = public
        self.peer_public = b""
        self.send_key = self.recv_key = self.transcript = b""
        self.state = State.WAITING
        self.requested_at = time.monotonic()
        self.outgoing_request = True
        return public, Mode.REQUEST

    def answer(self) -> tuple[bytes, Mode]:
        """Prepare a reply; returns ``(our_public, RESPONSE)``.

        The responder holds both public keys, and X25519 is symmetric, so the
        session can be derived here rather than after another round trip --
        waiting would deadlock, because the initiator derives at the same moment
        and has nothing left to send.

        **This does not modify the live session.** The caller must call
        :meth:`commit_answer` once the packet has actually been sent. The split
        matters: the response is carried in a picture, and a picture can turn out
        to be too small. Committing here would destroy a working session for a
        reply that never left the machine.
        """
        peer = self._staged_peer_public or self.peer_public
        if self.established and self._staged_peer_public:
            # Rekey requested by the peer while we had a working session.
            # Generate our half of the *new* session; the old keys stay live
            # until commit_answer().
            private_key = crypto.generate_keypair()
            public = crypto.public_bytes(private_key)
            self._staged_outgoing = (private_key, public, peer)
            return public, Mode.RESPONSE

        if self._private_key is None or not self.my_public:
            self.begin()
        self._derive()
        return self.my_public, Mode.RESPONSE

    def commit_answer(self) -> None:
        """Install the staged response material. Call after the send succeeds."""
        if self._staged_outgoing is None:
            return
        private_key, public, peer = self._staged_outgoing
        self._private_key = private_key
        self.my_public = public
        self.peer_public = peer
        self._clear_staging()
        self._derive()

    def discard_answer(self) -> None:
        """Throw away a prepared response whose packet could not be sent."""
        self._staged_outgoing = None

    def absorb(self, peer_public: bytes, mode: Mode) -> bool:
        """Take in a handshake payload. Returns ``True`` if a session completed.

        ``mode=REQUEST`` means the peer is asking for a session: we keep their
        key and the caller responds. ``mode=RESPONSE`` means the exchange is
        complete and we derive the keys now.

        A REQUEST arriving on an **established** session is a rekey: the peer has
        thrown away its old key and wants a new one. We stage it and stay
        ESTABLISHED, so messages keep flowing on the old keys until the new
        exchange completes. Replacing the live key immediately is what broke
        channels in testing -- the peer waits for a response we may not be able
        to send yet (no cover picture), and both sides go silent.
        """
        if len(peer_public) != PUBLIC_KEY_BYTES:
            raise InvalidInput(
                f"对端公钥必须是 {PUBLIC_KEY_BYTES} 字节，收到 {len(peer_public)}"
            )

        if mode is Mode.REQUEST:
            if self.established:
                self._staged_peer_public = peer_public
                return False
            self._store_peer(peer_public)
            # We have their key, but they do not have ours yet. No session until
            # the caller sends a response (which derives it -- see answer()).
            self.state = State.WAITING
            return False

        # RESPONSE
        if self._staged_private_key is not None:
            # We asked for the rekey; this response completes it.
            self._private_key = self._staged_private_key
            self.my_public = self._staged_my_public
            self.peer_public = peer_public
            self._clear_staging()
            return self._derive()

        # **同一个回复重复到达时必须忽略。**
        #
        # 场景：我们发出请求，对方（网络重传、或用户点了多次）回了几个
        # 相同/相近的包。原来每次都 `_derive()`，密钥被反复重建
        # （实测 generation 从 1 涨到 3），刚建立好的会话被瞬间换掉，
        # 已经用它加密的消息就解不开了。
        #
        # 公钥相同 = 同一个回复，直接当已完成处理，不再派生。
        if self.established and self.peer_public == peer_public:
            return False

        self._store_peer(peer_public)
        return self._derive()

    def _store_peer(self, peer_public: bytes) -> None:
        self.peer_public = peer_public
        if self._private_key is None or not self.my_public:
            # Peer spoke first; we still need our own key pair for the response.
            self._private_key = crypto.generate_keypair()
            self.my_public = crypto.public_bytes(self._private_key)

    def _derive(self) -> bool:
        if self._private_key is None or not self.peer_public:
            return False
        shared = crypto.exchange(self._private_key, self.peer_public)
        self.send_key, self.recv_key, self.transcript = crypto.session_keys(
            self.my_public, self.peer_public, shared
        )
        self.state = State.ESTABLISHED
        self.generation += 1
        self.key_since = time.monotonic()
        self.outgoing_request = False
        return True

    # ----------------------------------------------------------------- crypto

    def seal(self, kind: Kind, plaintext: bytes) -> bytes:
        if not self.established:
            raise InvalidInput(
                f"会话{self.state.label}，不能发送密文；先完成密钥交换"
            )
        from .protocol import seal_body

        return seal_body(kind, self.send_key, plaintext)

    def open(self, kind: Kind, payload: bytes) -> bytes:
        """Decrypt a body envelope. ``BadKey`` means "not for us"; do not raise it."""
        if not self.established:
            raise BadKey(f"会话{self.state.label}，无法解密正文")
        from .protocol import open_body

        return open_body(payload, kind, self.recv_key)


__all__ = ["State", "Session"]
