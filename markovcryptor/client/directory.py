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


@dataclass(frozen=True)
class Friend:
    """One row in the friend list."""

    uin: str
    nickname: str = ""
    remark: str = ""

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
            )
        self._friends = found
        if owner:
            self.owner = str(owner)

    def clear(self) -> None:
        self._friends.clear()
        self.owner = ""
