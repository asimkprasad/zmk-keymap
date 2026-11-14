"""What would have to change on the keyboard for it to match a snapshot.

Restoring by writing all 360 bindings would work and would be wrong. It burns flash,
takes a minute over BLE, and - the part that matters - makes the operation unreviewable:
"360 writes" tells you nothing, while "3 writes, here they are" can be read before it
happens. So the restore path computes a difference first and writes only that.

A second reason: a diff of nothing is a restore of nothing. Running restore against a
keyboard that already matches is a no-op that says so, rather than a minute of writes
that happen to leave everything where it was.

One trap, called out because it is the kind that corrupts quietly:

    The LIVE keymap has no position field. `Keymap.Layer.bindings` is a repeated
    field whose INDEX is the key position. The snapshot's `Binding` stores position
    explicitly, and need not be in order. Joining the two by zipping them would be
    correct right up until a snapshot arrived out of order.

This module is pure. It takes two messages and a Remapping and returns a list; it
opens nothing and sends nothing.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import typing

from . import decode, model as pb, remap


class _LiveBinding(typing.Protocol):
    @property
    def behavior_id(self) -> int: ...
    @property
    def param1(self) -> int: ...
    @property
    def param2(self) -> int: ...


class _LiveLayer(typing.Protocol):
    @property
    def id(self) -> int: ...
    @property
    def name(self) -> str: ...
    @property
    def bindings(self) -> Sequence[_LiveBinding]: ...


class _LiveKeymap(typing.Protocol):
    @property
    def layers(self) -> Iterable[_LiveLayer]: ...


class LayoutMismatch(Exception):
    """The snapshot does not describe the keyboard it is being restored onto.

    Raised rather than skipped: a snapshot with layers or key counts that do not line
    up is a snapshot from a different keyboard or a different physical layout, and
    writing the part that happens to fit is worse than writing nothing.
    """


@dataclass(frozen=True)
class Change:
    """One key that differs, with both sides spelled out for a human to read."""

    layer_id: int
    layer_name: str
    position: int

    #: Live behavior id, already translated through the Remapping. This is what goes
    #: on the wire; the snapshot's own id is deliberately not kept, so it cannot be
    #: sent by accident.
    behavior_id: int
    param1: int
    param2: int

    before: str
    after: str

    def __str__(self) -> str:
        return (f"{self.layer_name} position {self.position}: "
                f"{self.before} -> {self.after}")


@dataclass(frozen=True)
class Plan:
    """Every write a restore would perform, and nothing else."""

    changes: tuple[Change, ...]

    #: Key positions compared, so "0 changes" can be distinguished from "nothing
    #: was actually looked at", which is the failure mode of a silent mismatch.
    compared: int

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def __len__(self) -> int:
        return len(self.changes)


def _live_index(live_keymap: _LiveKeymap) -> dict[int, _LiveLayer]:
    layers: dict[int, _LiveLayer] = {}
    for layer in live_keymap.layers:
        if layer.id in layers:
            raise LayoutMismatch(
                f"the keyboard reported layer id {layer.id} twice; it cannot be "
                f"addressed unambiguously")
        layers[layer.id] = layer
    return layers


def _render(
    table: Mapping[int, typing.Any] | None,
    behavior_id: int,
    param1: int,
    param2: int,
) -> str:
    """Readable form of a binding, or an honest raw fallback."""
    behavior = table.get(behavior_id) if table else None
    if behavior is None:
        return f"behavior {behavior_id} (0x{param1:08x}, 0x{param2:08x})"
    return decode.binding(behavior, param1, param2).text


def plan(
    snapshot: pb.KeymapSnapshot,
    live_keymap: _LiveKeymap,
    remapping: remap.Remapping,
    live_behaviors: Mapping[int, typing.Any] | None = None,
) -> Plan:
    """The writes that would make `live_keymap` match `snapshot`.

    `remapping` comes from `remap.plan` and translates the snapshot's behavior ids
    into this keyboard's. `live_behaviors` is an optional {id: Behavior} table used
    only to render the current state readably in the returned Changes; omitting it
    costs nothing but legibility.
    """
    live_layers = _live_index(live_keymap)

    missing = [f"{layer.name or layer.id} (id {layer.id})"
               for layer in snapshot.layers if layer.id not in live_layers]
    if missing:
        raise LayoutMismatch(
            f"the keyboard has no layer matching {', '.join(missing)}. The snapshot "
            f"has {len(snapshot.layers)} layers and the keyboard has "
            f"{len(live_layers)}; restoring bindings into layers that do not line up "
            f"would scatter them. Nothing was written.")

    changes: list[Change] = []
    compared = 0
    for layer in snapshot.layers:
        live = live_layers[layer.id]
        for binding in layer.bindings:
            # Position is an INDEX into the live layer, not a field on it.
            if binding.position >= len(live.bindings):
                raise LayoutMismatch(
                    f"the snapshot binds {layer.name or layer.id} position "
                    f"{binding.position}, but the keyboard's layer {layer.id} has "
                    f"only {len(live.bindings)} keys. This snapshot is from a "
                    f"different physical layout. Nothing was written.")
            current = live.bindings[binding.position]
            target_id = remapping.translate(binding.behavior_id)
            compared += 1
            if (current.behavior_id == target_id
                    and current.param1 == binding.param1
                    and current.param2 == binding.param2):
                continue
            changes.append(Change(
                layer_id=layer.id,
                layer_name=layer.name or str(layer.id),
                position=binding.position,
                behavior_id=target_id,
                param1=binding.param1,
                param2=binding.param2,
                before=_render(live_behaviors, current.behavior_id,
                               current.param1, current.param2),
                after=binding.rendered or _render(
                    live_behaviors, target_id, binding.param1, binding.param2),
            ))
    return Plan(changes=tuple(changes), compared=compared)
