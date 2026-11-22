"""Immutable diff and behavior-ID translation between two `KeymapSnapshot`s (`KeymapDiff`).

Why `BehaviorTranslator` and `KeyDiff` live inside `KeymapDiff`:

1. **A `behavior_id` is local to one firmware build:** Replaying a stored numeric id onto
   a board whose behavior table shifted binds keys to a *different* behavior while every
   RPC call returns OK. Across snapshots or firmware builds, identity is the behavior's
   `display_name`, matched and translated by `KeymapDiff.BehaviorTranslator`.
2. **Bounded sparse position join:** `KeymapSnapshot` stores `KeyBinding.position`
   explicitly and need not be dense or ordered. Diffing validates layer and position
   bounds on both snapshots and produces `KeymapDiff.KeyDiff` items carrying the
   translated target `behavior_id` alongside decoded `before_binding` and `after_binding`
   messages.

Call `before.diff(target)` on a `KeymapSnapshot` to compute a `KeymapDiff`.
This module is pure: it opens no ports, imports no `zmk_studio_client` sessions, and
performs zero I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
import typing

from .proto import keymap_snapshot_pb2 as _pb

__all__ = [
    "BehaviorAmbiguous",
    "BehaviorMissing",
    "KeymapDiff",
    "LayoutMismatch",
]

#: Upper bound on key positions when neither snapshot carries `PhysicalLayout.keys`.
_MAX_KEY_POSITIONS = 1024


class _NamedBehavior(typing.Protocol):
    @property
    def id(self) -> int: ...
    @property
    def display_name(self) -> str: ...


class _SnapshotInput(typing.Protocol):
    @property
    def layout(self) -> _pb.PhysicalLayout: ...
    @property
    def layers(self) -> Sequence[_pb.Layer]: ...
    @property
    def behaviors(self) -> Sequence[_NamedBehavior]: ...


class BehaviorMissing(Exception):
    """The target behavior table has no behavior matching a name the snapshot binds."""


class BehaviorAmbiguous(Exception):
    """Two behaviors share a display name, so a name does not identify one of them."""


class LayoutMismatch(Exception):
    """The target snapshot's layers or key positions do not fit the baseline snapshot."""


def _validate_snapshot_positions(
    layers: Sequence[_pb.Layer],
    *,
    role: str,
    max_positions: int,
    bound_from_layout: bool,
) -> dict[int, dict[int, _pb.KeyBinding]]:
    """Index `layers` sparsely by `(layer_id, position)` after bounds checks."""
    layers_by_id: dict[int, dict[int, _pb.KeyBinding]] = {}
    for layer in layers:
        if layer.id in layers_by_id:
            raise LayoutMismatch(
                f"the {role} snapshot reported layer id {layer.id} twice; it cannot "
                f"be addressed unambiguously. Nothing was written."
            )
        by_pos: dict[int, _pb.KeyBinding] = {}
        for binding in layer.bindings:
            if binding.position >= max_positions:
                if bound_from_layout:
                    raise LayoutMismatch(
                        f"the {role} snapshot binds {layer.name or layer.id} position "
                        f"{binding.position}, but its physical layout has only "
                        f"{max_positions} keys. Nothing was written."
                    )
                raise LayoutMismatch(
                    f"the {role} snapshot binds {layer.name or layer.id} position "
                    f"{binding.position}, which exceeds the maximum supported key "
                    f"position ({_MAX_KEY_POSITIONS - 1}). Nothing was written."
                )
            if binding.position in by_pos:
                raise LayoutMismatch(
                    f"the {role} snapshot binds {layer.name or layer.id} position "
                    f"{binding.position} twice. Nothing was written."
                )
            by_pos[binding.position] = binding
        layers_by_id[layer.id] = by_pos
    return layers_by_id


@dataclass(frozen=True)
class KeymapDiff:
    """Minimal set of per-key binding diffs (and behavior translation) from baseline to target."""

    @dataclass(frozen=True)
    class BehaviorTranslator:
        """Translates a snapshot's `behavior_id`s into a target behavior table by display name."""

        #: snapshot behavior id -> target behavior id (covers bound behaviors only).
        ids: Mapping[int, int]

        #: Behavior names carried in the snapshot table that are never bound and absent on target.
        unused_missing: tuple[str, ...] = ()

        def __post_init__(self) -> None:
            object.__setattr__(self, "ids", MappingProxyType(dict(self.ids)))
            object.__setattr__(self, "unused_missing", tuple(self.unused_missing))

        def __hash__(self) -> int:
            return hash((tuple(sorted(self.ids.items())), self.unused_missing))

        @property
        def matched_count(self) -> int:
            """Number of bound behaviors matched between the snapshot and target tables."""
            return len(self.ids)

        @property
        def is_identity(self) -> bool:
            """True when every bound behavior id maps to itself."""
            return all(source == target for source, target in self.ids.items())

        def translate(self, behavior_id: int) -> int:
            """Target behavior id for `behavior_id` (`KeyError` if unplanned)."""
            return self.ids[behavior_id]

        @staticmethod
        def _index_table(
            behaviors: Iterable[_NamedBehavior], side: str
        ) -> tuple[dict[int, str], dict[str, list[int]]]:
            by_id: dict[int, str] = {}
            by_name: dict[str, list[int]] = {}
            for behavior in behaviors:
                if behavior.id in by_id:
                    raise BehaviorAmbiguous(
                        f"the {side} behavior table defines behavior id {behavior.id} "
                        f"more than once ({by_id[behavior.id]!r} and "
                        f"{behavior.display_name!r}), so bindings cannot be "
                        f"translated safely"
                    )
                by_id[behavior.id] = behavior.display_name
                by_name.setdefault(behavior.display_name, []).append(behavior.id)
            return by_id, by_name

        @classmethod
        def between(
            cls,
            snapshot: _SnapshotInput,
            target_behaviors: Iterable[_NamedBehavior],
        ) -> KeymapDiff.BehaviorTranslator:
            """Match every behavior bound in `snapshot` against `target_behaviors` by name."""
            stored, snapshot_by_name = cls._index_table(snapshot.behaviors, "snapshot")
            _, live_by_name = cls._index_table(target_behaviors, "keyboard")

            used: dict[int, list[str]] = {}
            for layer in snapshot.layers:
                for binding in layer.bindings:
                    used.setdefault(binding.behavior_id, []).append(
                        f"{layer.name or layer.id} position {binding.position}"
                    )

            ids: dict[int, int] = {}
            missing: list[str] = []
            for behavior_id in sorted(used):
                name = stored.get(behavior_id)
                if name is None:
                    raise BehaviorMissing(
                        f"the snapshot binds behavior id {behavior_id}, which its own behavior "
                        f"table does not describe; the file is incomplete. Used by: "
                        f"{', '.join(used[behavior_id][:3])}"
                    )
                if len(snapshot_by_name[name]) > 1:
                    raise BehaviorAmbiguous(
                        f"the snapshot behavior table has two behaviors called {name!r}, so a "
                        f"name does not identify one of them and bindings cannot be "
                        f"translated safely"
                    )
                live_matches = live_by_name.get(name)
                if not live_matches:
                    where = used[behavior_id]
                    shown = ", ".join(where[:3]) + (
                        f" and {len(where) - 3} more" if len(where) > 3 else ""
                    )
                    missing.append(f"{name!r} (used by {shown})")
                    continue
                if len(live_matches) > 1:
                    raise BehaviorAmbiguous(
                        f"the keyboard behavior table has two behaviors called {name!r}, so a "
                        f"name does not identify one of them and bindings cannot be "
                        f"translated safely"
                    )
                ids[behavior_id] = live_matches[0]

            if missing:
                raise BehaviorMissing(
                    "the keyboard has no behavior matching "
                    + "; ".join(sorted(missing))
                    + ". Restoring would leave those keys bound to something else, so nothing "
                    "was written. This usually means the snapshot came from a different "
                    "firmware build than the one now on the keyboard."
                )

            unused_missing = tuple(
                sorted(name for name in snapshot_by_name if name not in live_by_name)
            )
            return cls(ids=ids, unused_missing=unused_missing)

    @dataclass(frozen=True)
    class KeyDiff:
        """One key position `(layer_id, position)` that differs, carrying the target `behavior_id`."""

        layer_id: int
        layer_name: str
        position: int
        behavior_id: int
        param1: int
        param2: int
        before: str
        after: str
        before_binding: _pb.KeyBinding = field(hash=False)
        after_binding: _pb.KeyBinding = field(hash=False)

        def __str__(self) -> str:
            return f"{self.layer_name} position {self.position}: {self.before} -> {self.after}"

    key_diffs: Sequence[KeymapDiff.KeyDiff]
    compared: int
    translator: KeymapDiff.BehaviorTranslator
    baseline_layer_names: Mapping[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_diffs", tuple(self.key_diffs))
        object.__setattr__(
            self,
            "baseline_layer_names",
            MappingProxyType(dict(self.baseline_layer_names)),
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.key_diffs,
                self.compared,
                self.translator,
                tuple(sorted(self.baseline_layer_names.items())),
            )
        )

    @property
    def is_empty(self) -> bool:
        return not self.key_diffs

    def __len__(self) -> int:
        return len(self.key_diffs)

    def __iter__(self) -> Iterator[KeymapDiff.KeyDiff]:
        return iter(self.key_diffs)

    def for_layer(self, layer_id: int) -> Mapping[int, KeymapDiff.KeyDiff]:
        """Return `{position: KeyDiff}` for all changed keys on `layer_id`."""
        return {kd.position: kd for kd in self.key_diffs if kd.layer_id == layer_id}


def _diff_snapshots(
    before: _SnapshotInput,
    after: _SnapshotInput,
) -> KeymapDiff:
    """Compute the `KeymapDiff` needed to move from `before` to `after`."""
    before_keys = len(before.layout.keys)
    after_keys = len(after.layout.keys)
    if before_keys and after_keys and before_keys != after_keys:
        raise LayoutMismatch(
            f"the baseline snapshot has {before_keys} keys and the target snapshot "
            f"has {after_keys} keys; snapshots from different physical layouts "
            f"cannot be diffed position-for-position."
        )

    layout_width = before_keys or after_keys
    bound_from_layout = layout_width > 0
    max_positions = layout_width if bound_from_layout else _MAX_KEY_POSITIONS

    before_layers = _validate_snapshot_positions(
        before.layers,
        role="baseline",
        max_positions=max_positions,
        bound_from_layout=bound_from_layout,
    )
    _validate_snapshot_positions(
        after.layers,
        role="target",
        max_positions=max_positions,
        bound_from_layout=bound_from_layout,
    )

    missing = [
        f"{layer.name or layer.id} (id {layer.id})"
        for layer in after.layers
        if layer.id not in before_layers
    ]
    if missing:
        raise LayoutMismatch(
            f"the baseline snapshot has no layer matching {', '.join(missing)}. "
            f"The target snapshot has {len(after.layers)} layers and the baseline "
            f"has {len(before_layers)}. Nothing was written."
        )

    translator = KeymapDiff.BehaviorTranslator.between(after, before.behaviors)

    key_diffs: list[KeymapDiff.KeyDiff] = []
    compared = 0
    for layer in after.layers:
        for binding in layer.bindings:
            current = before_layers[layer.id].get(binding.position)
            if current is None:
                raise LayoutMismatch(
                    f"the target snapshot binds {layer.name or layer.id} position "
                    f"{binding.position}, which is absent from the baseline snapshot's "
                    f"layer {layer.id}; refusing to invent a default binding."
                )
            target_id = translator.translate(binding.behavior_id)
            compared += 1
            if (
                current.behavior_id == target_id
                and current.param1 == binding.param1
                and current.param2 == binding.param2
            ):
                continue

            before_str = (
                current.decoded
                or f"behavior {current.behavior_id} (0x{current.param1:08x}, 0x{current.param2:08x})"
            )
            after_str = (
                binding.decoded
                or f"behavior {target_id} (0x{binding.param1:08x}, 0x{binding.param2:08x})"
            )

            before_copy = _pb.KeyBinding()
            before_copy.CopyFrom(current)
            after_copy = _pb.KeyBinding()
            after_copy.CopyFrom(binding)

            key_diffs.append(
                KeymapDiff.KeyDiff(
                    layer_id=layer.id,
                    layer_name=layer.name or str(layer.id),
                    position=binding.position,
                    behavior_id=target_id,
                    param1=binding.param1,
                    param2=binding.param2,
                    before=before_str,
                    after=after_str,
                    before_binding=before_copy,
                    after_binding=after_copy,
                )
            )

    return KeymapDiff(
        key_diffs=tuple(key_diffs),
        compared=compared,
        translator=translator,
        baseline_layer_names={lyr.id: lyr.name for lyr in before.layers},
    )
