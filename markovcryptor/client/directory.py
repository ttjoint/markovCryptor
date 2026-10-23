"""The friend list, held in memory and thrown away on exit.

Deliberately not persisted. This program exists to keep conversations private;
caching a copy of who you talk to on disk would contradict that. Fetching it
again on every start costs one OneBot call.

The only non-obvious rule is which name to show. QQ gives us both a nickname and
a remark, and the remark is what the *user* chose -- it is the name they will
recognise. Showing the nickname when a remark exists looks like the wrong person.
"""

from __future__ import annotations

from dataclasses import dataclass


def avatar_url(uin: str, size: int = 100) -> str:
    """QQ's public avatar URL for ``uin``.

    ``q1.qlogo.cn`` is the endpoint QQ's own clients use. Building it here means
    the friend list can render names and placeholder circles instantly while the
    images stream in, with no extra OneBot round trip per friend (29 friends
    would otherwise be 29 blocking calls at startup).
    """
    return f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s={int(size)}"


@dataclass(frozen=True)
class Friend:
    """One row in the friend list."""

    uin: str
    nickname: str = ""
    remark: str = ""
    #: Avatar URL. QQ's CDN is stable and needs no round trip, so we build it
    #: locally instead of asking the bridge -- the list renders immediately and
    #: images stream in afterwards.
    avatar: str = ""

    @property
    def display_name(self) -> str:
        """What to print: the remark if the user set one, else the nickname.

        Falls back to the number itself only for a friend with neither, which
        should not happen but must not render as an empty row.
        """
        return self.remark.strip() or self.nickname.strip() or self.uin


class Directory:
    """Friends, ordered for display, keyed by QQ number.

    ``refresh`` replaces the whole list rather than merging: a friend you just
    deleted should disappear, and merging would leave a ghost row forever.
    """

    def __init__(self) -> None:
        self._friends: dict[str, Friend] = {}
        #: The account these friends belong to, filled by ``refresh``.
        self.owner: str = ""
        #: Our own nickname and avatar, for the header. Empty until login_info
        #: comes back; the UI shows the number meanwhile rather than a blank.
        self.self_nickname: str = ""
        self.self_avatar: str = ""

    def __len__(self) -> int:
        return len(self._friends)

    def __contains__(self, uin) -> bool:
        return str(uin) in self._friends

    def __iter__(self):
        return iter(self.ordered())

    def get(self, uin) -> Friend | None:
        return self._friends.get(str(uin))

    def name_of(self, uin) -> str:
        """Display name, or the bare number for an unknown contact.

        Unknown is normal -- a stranger can message you, and the group member
        list is not the friend list. Returning the number keeps the row usable.
        """
        friend = self.get(uin)
        return friend.display_name if friend else str(uin)

    def ordered(self) -> list[Friend]:
        """Stable display order: by name, then number.

        By name rather than by number because users look for names. The number is
        the tiebreak so two friends with the same remark cannot swap places
        between refreshes.
        """
        return sorted(self._friends.values(),
                      key=lambda f: (f.display_name, f.uin))

    def refresh(self, payload: list[dict], *, owner: str = "") -> None:
        """Replace the list from a ``get_friend_list`` reply.

        Tolerates junk rows. A malformed entry is skipped rather than raising:
        this runs right after login, and one odd record must not stop the client
        from starting.
        """
        found: dict[str, Friend] = {}
        for row in payload or ():
            if not isinstance(row, dict):
                continue
            uin = row.get("user_id")
            if uin is None:
                continue
            key = str(uin)
            found[key] = Friend(
                uin=key,
                nickname=str(row.get("nickname") or ""),
                remark=str(row.get("remark") or ""),
                avatar=avatar_url(key),
            )
        self._friends = found
        if owner:
            self.owner = str(owner)

    # ---------------------------------------------------------------- 本机

    def set_self(self, uin: str, nickname: str = "", avatar: str = "") -> None:
        """Record our own profile, for the header row.

        Kept separate from ``_friends``: we are not our own friend, and mixing
        them in would put a row for ourselves in the contact list.
        """
        self.owner = str(uin or "")
        self.self_nickname = str(nickname or "")
        self.self_avatar = str(avatar or "") or avatar_url(self.owner)

    @property
    def self_name(self) -> str:
        """Our own display name: real nickname, falling back to the number."""
        return self.self_nickname.strip() or self.owner

    def clear(self) -> None:
        self._friends.clear()
        self.owner = ""
