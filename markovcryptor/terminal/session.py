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

    @property
    def established(self) -> bool:
        return self.state is State.ESTABLISHED

    def reset(self) -> None:
        """Forget everything, including the ephemeral private key."""
        self.state = State.IDLE
        self._private_key = None
        self.my_public = b""
        self.peer_public = b""
        self.send_key = b""
        self.recv_key = b""
        self.transcript = b""

    # ------------------------------------------------------------ transitions

    def begin(self) -> tuple[bytes, Mode]:
        """Start a handshake; returns ``(our_public, REQUEST)``.

        Always generates a fresh key pair: a replayed old handshake image then
        derives a key the other side never agreed to, and every body message
        fails authentication instead of decrypting into nonsense.
        """
        self._private_key = crypto.generate_keypair()
        self.my_public = crypto.public_bytes(self._private_key)
        self.peer_public = b""
        self.send_key = self.recv_key = self.transcript = b""
        self.state = State.WAITING
        return self.my_public, Mode.REQUEST

    def answer(self) -> tuple[bytes, Mode]:
        """Reply to a peer request; returns ``(our_public, RESPONSE)``.

        The responder already holds both public keys (ours from :meth:`begin`,
        theirs from :meth:`absorb`), and X25519 is symmetric, so it derives the
        session right here. Waiting for a further round trip would deadlock:
        the initiator derives at the same moment and has nothing left to send.
        """
        if self._private_key is None or not self.my_public:
            self.begin()
        self._derive()
        return self.my_public, Mode.RESPONSE

    def absorb(self, peer_public: bytes, mode: Mode) -> bool:
        """Take in a handshake payload. Returns ``True`` if a session completed.

        ``mode=REQUEST`` means the peer is asking for a session: we keep their
        key and the caller responds. ``mode=RESPONSE`` means the exchange is
        complete and we derive the keys now.
        """
        if len(peer_public) != PUBLIC_KEY_BYTES:
            raise InvalidInput(
                f"对端公钥必须是 {PUBLIC_KEY_BYTES} 字节，收到 {len(peer_public)}"
            )
        self._store_peer(peer_public)
        if mode is Mode.REQUEST:
            # We have their key, but they do not have ours yet. No session until
            # the caller sends a response (which derives it -- see answer()).
            self.state = State.WAITING
            return False
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
