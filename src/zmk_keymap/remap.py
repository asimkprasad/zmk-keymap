"""Translate a snapshot's behavior ids into the ids the live keyboard uses.

A `behavior_id` is assigned by the firmware, not by the protocol. It is an index into
whatever behavior set that build happened to compile, so it means nothing outside the
board and build that produced it. The snapshot stores the table alongside the bindings
precisely so the ids can be translated later rather than trusted.

Why this matters more than it looks:

    Replaying a stored id onto a board whose table has shifted binds the key to a
    DIFFERENT BEHAVIOR, and every call returns OK, because the id is individually
    valid. There is no error to catch. The keyboard simply does the wrong thing.

So the identity used across a restore is the behavior's NAME, and the id is treated as
a local coordinate on each side. `Binding.behavior_name` is captured for exactly this.

This module is a pure function over two tables. It opens nothing, sends nothing, and
needs no keyboard: the live table is passed in by the caller that read it.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import typing

from . import model as pb


class _NamedBehavior(typing.Protocol):
    @property
    def id(self) -> int: ...
    @property
    def display_name(self) -> str: ...


class BehaviorMissing(Exception):
    """The live keyboard has no behavior by a name the snapshot binds.

    Restoring anyway would leave those keys wrong, so this aborts rather than
    writing the part that happens to fit.
    """


class BehaviorAmbiguous(Exception):
    """Two behaviors share a display name, so a name does not identify one of them."""


@dataclass(frozen=True)
class Remapping:
    """How to rewrite the snapshot's behavior ids for this keyboard."""

    #: snapshot behavior id -> live behavior id. Covers only the behaviors the
    #: snapshot actually binds; see `plan`.
    ids: dict[int, int]

    #: Behavior names the snapshot carries but never binds, which are absent from the
    #: live table. Reported rather than raised: an unused behavior cannot break a key.
    unused_and_missing: tuple[str, ...]

    @property
    def is_identity(self) -> bool:
        """True when every id maps to itself, i.e. the tables agree.

        The expected case for a backup restored onto the firmware that produced it.
        Worth naming so a caller can say "no translation needed" instead of implying
        a rewrite happened.
        """
        return all(source == target for source, target in self.ids.items())

    def translate(self, behavior_id: int) -> int:
        """The live id for a snapshot id. KeyError if it was never planned for."""
        return self.ids[behavior_id]


def _by_name(behaviors: Iterable[_NamedBehavior], side: str) -> dict[str, int]:
    """Index a behavior table by display name, refusing a name that is not unique."""
    index: dict[str, int] = {}
    seen: set[str] = set()
    for behavior in behaviors:
        name = behavior.display_name
        if name in seen:
            raise BehaviorAmbiguous(
                f"the {side} behavior table has two behaviors called {name!r}, so a "
                f"name does not identify one of them and bindings cannot be "
                f"translated safely"
            )
        seen.add(name)
        index[name] = behavior.id
    return index


def plan(snapshot: pb.KeymapSnapshot, live_behaviors: Iterable[_NamedBehavior]) -> Remapping:
    """Work out how to rewrite `snapshot`'s behavior ids for the live keyboard.

    Only the behaviors the snapshot actually BINDS have to exist on the keyboard. A
    board that dropped a behavior nobody uses can still take the restore, and saying
    so keeps a restore working across firmware changes that merely trim the set.

    Raises BehaviorMissing naming the keys at stake, because "Sticky Key is missing"
    is a question ("do I care?") while "Sticky Key is missing, used by NAV position
    14" is an answer.
    """
    stored = {b.id: b.display_name for b in snapshot.behaviors}
    snapshot_index = _by_name(snapshot.behaviors, "snapshot")   # also rejects dupes
    live_index = _by_name(live_behaviors, "keyboard")

    # Where each bound behavior is used, so a failure can point at real keys.
    used: dict[int, list[str]] = {}
    for layer in snapshot.layers:
        for binding in layer.bindings:
            used.setdefault(binding.behavior_id, []).append(
                f"{layer.name or layer.id} position {binding.position}")

    ids: dict[int, int] = {}
    missing: list[str] = []
    for behavior_id in sorted(used):
        name = stored.get(behavior_id)
        if name is None:
            # A binding naming an id the snapshot's own table lacks. The corpus test
            # test_every_binding_resolves_to_a_behavior_in_the_table forbids this, so
            # reaching here means a hand-edited or truncated file.
            raise BehaviorMissing(
                f"the snapshot binds behavior id {behavior_id}, which its own behavior "
                f"table does not describe; the file is incomplete. Used by: "
                f"{', '.join(used[behavior_id][:3])}"
            )
        if name not in live_index:
            where = used[behavior_id]
            shown = ", ".join(where[:3]) + (f" and {len(where) - 3} more" if len(where) > 3 else "")
            missing.append(f"{name!r} (used by {shown})")
            continue
        ids[behavior_id] = live_index[name]

    if missing:
        raise BehaviorMissing(
            "the keyboard has no behavior matching " + "; ".join(sorted(missing))
            + ". Restoring would leave those keys bound to something else, so nothing "
              "was written. This usually means the snapshot came from a different "
              "firmware build than the one now on the keyboard."
        )

    unused_missing = tuple(sorted(
        name for name in snapshot_index if name not in live_index))
    return Remapping(ids=ids, unused_and_missing=unused_missing)
