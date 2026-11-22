"""The immutable `KeymapSnapshot` domain model, parameter decoder, and JSON codec.

Everything persisted in a snapshot file is defined in `proto/keymap_snapshot.proto`
under `zmk_keymap.v1`, importing `zmk_studio_client.v1.Behavior` for the behavior
table. `KeymapSnapshot` wraps the generated protobuf message behind an immutable
interface (`KeymapSnapshot(...)`, `KeymapSnapshot.from_json()`, `to_json()`,
`sha256()`, `diff()`).

This module references `zmk_studio_client` only for the static `Behavior` protobuf
descriptor (`zmk_studio_client.proto.keyboard_pb2`) and exposes none of its runtime
or transports, so offline diffing and rendering have no call path to a device.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import datetime
import hashlib
import json
from types import MappingProxyType
import typing

from google.protobuf import json_format
from zmk_studio_client.proto import keyboard_pb2

from . import _decoder
from .. import keymap_diff
from ..proto import keymap_snapshot_pb2 as _pb
from ..proto.keymap_snapshot_pb2 import (
    ARGUMENT_KIND_CONSTANT,
    ARGUMENT_KIND_HID_USAGE,
    ARGUMENT_KIND_LAYER_ID,
    ARGUMENT_KIND_RANGE,
    ARGUMENT_KIND_UNDECLARED,
    ARGUMENT_KIND_UNSPECIFIED,
    ArgumentKind,
    BehaviorArgument,
    Device,
    KeyBinding,
    KeyPosition,
    Layer,
    PhysicalLayout,
    SnapshotMetadata,
)

__all__ = [
    "ArgumentKind",
    "BehaviorArgument",
    "Device",
    "KeyBinding",
    "KeyPosition",
    "KeymapSnapshot",
    "Layer",
    "PhysicalLayout",
    "SnapshotMetadata",
    "ARGUMENT_KIND_UNSPECIFIED",
    "ARGUMENT_KIND_CONSTANT",
    "ARGUMENT_KIND_RANGE",
    "ARGUMENT_KIND_HID_USAGE",
    "ARGUMENT_KIND_LAYER_ID",
    "ARGUMENT_KIND_UNDECLARED",
    "argument_kind_name",
    "decode_binding",
]

decode_binding = _decoder.decode_binding

LayerSpec: typing.TypeAlias = (
    Layer | tuple[int, str, Iterable[tuple[int, int, int, int] | KeyBinding]]
)

_KIND_NAMES: dict[int, str] = {
    ARGUMENT_KIND_UNSPECIFIED: "unspecified",
    ARGUMENT_KIND_CONSTANT: "constant",
    ARGUMENT_KIND_RANGE: "range",
    ARGUMENT_KIND_HID_USAGE: "HID usage",
    ARGUMENT_KIND_LAYER_ID: "layer id",
    ARGUMENT_KIND_UNDECLARED: "undeclared",
}


def argument_kind_name(kind: int) -> str:
    """Human-readable label for an `ArgumentKind` enum value."""
    return _KIND_NAMES.get(kind, "unspecified")


def _tool_version() -> str:
    """Our own version, or 'unknown' when running from an uninstalled source tree."""
    from importlib.metadata import PackageNotFoundError, version

    if not __package__:
        return "unknown"
    try:
        return version(__package__.split(".", 1)[0])
    except PackageNotFoundError:
        return "unknown"


class KeymapSnapshot:
    """Immutable point-in-time keymap archive domain model backed by `_pb.KeymapSnapshot`."""

    DESCRIPTOR: typing.ClassVar[typing.Any] = _pb.KeymapSnapshot.DESCRIPTOR

    def __init__(
        self,
        *,
        device: Device | None = None,
        layout: PhysicalLayout | None = None,
        behaviors: Iterable[keyboard_pb2.Behavior] = (),
        layers: Iterable[LayerSpec] = (),
        captured_at: datetime.datetime | None = None,
        studio_messages_revision: str = "",
        _proto: _pb.KeymapSnapshot | None = None,
    ) -> None:
        pb_snap = _pb.KeymapSnapshot()
        unknown = keyboard_pb2.Behavior()
        table: dict[int, keyboard_pb2.Behavior] = {}

        if _proto is not None:
            pb_snap.CopyFrom(_proto)
            for out_beh in pb_snap.behaviors:
                if out_beh.id in table:
                    raise ValueError(
                        f"not a keymap snapshot: behavior id {out_beh.id} appears more than once"
                    )
                table[out_beh.id] = out_beh
            for out_layer in pb_snap.layers:
                for out_binding in out_layer.bindings:
                    out_binding.CopyFrom(
                        _decoder.decode_binding(
                            table.get(out_binding.behavior_id, unknown),
                            out_binding.param1,
                            out_binding.param2,
                            position=out_binding.position,
                            behavior_id=out_binding.behavior_id,
                        )
                    )
        else:
            if captured_at is not None:
                pb_snap.metadata.captured_at.FromDatetime(captured_at)
            if captured_at is not None or studio_messages_revision:
                pb_snap.metadata.tool_version = _tool_version()
                pb_snap.metadata.studio_messages_revision = studio_messages_revision

            if device is not None:
                pb_snap.device.CopyFrom(device)
            if layout is not None:
                pb_snap.layout.CopyFrom(layout)

            for details in sorted(behaviors, key=lambda b: b.id):
                if details.id in table:
                    raise ValueError(
                        f"behavior id {details.id} appears more than once in behavior table"
                    )
                out_beh = pb_snap.behaviors.add()
                out_beh.CopyFrom(details)
                table[out_beh.id] = out_beh

            for order, layer_spec in enumerate(layers):
                if isinstance(layer_spec, Layer):
                    out_layer = pb_snap.layers.add()
                    out_layer.id = layer_spec.id
                    out_layer.display_order = layer_spec.display_order
                    out_layer.name = layer_spec.name
                    for existing in layer_spec.bindings:
                        out_layer.bindings.append(
                            _decoder.decode_binding(
                                table.get(existing.behavior_id, unknown),
                                existing.param1,
                                existing.param2,
                                position=existing.position,
                                behavior_id=existing.behavior_id,
                            )
                        )
                    continue
                layer_id, layer_name, raw_bindings = layer_spec
                out_layer = pb_snap.layers.add()
                out_layer.id = layer_id
                out_layer.display_order = order
                out_layer.name = layer_name
                for item in raw_bindings:
                    if isinstance(item, KeyBinding):
                        out_layer.bindings.append(
                            _decoder.decode_binding(
                                table.get(item.behavior_id, unknown),
                                item.param1,
                                item.param2,
                                position=item.position,
                                behavior_id=item.behavior_id,
                            )
                        )
                    else:
                        pos, bid, p1, p2 = item
                        out_layer.bindings.append(
                            _decoder.decode_binding(
                                table.get(bid, unknown),
                                p1,
                                p2,
                                position=pos,
                                behavior_id=bid,
                            )
                        )

        self._pb = pb_snap
        self._sha256: str | None = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, KeymapSnapshot):
            return NotImplemented
        return bool(self._pb == other._pb)

    def __hash__(self) -> int:
        return hash(self.sha256())

    # -- serialization, identity & diffing ------------------------------------

    @classmethod
    def from_json(cls, raw: str) -> KeymapSnapshot:
        """Parse a canonical JSON snapshot string into a `KeymapSnapshot`."""
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(f"not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("not a keymap snapshot: expected a JSON object at top level")
        try:
            proto = json_format.ParseDict(parsed, _pb.KeymapSnapshot())
        except (json_format.ParseError, RecursionError) as exc:
            raise ValueError(f"not a keymap snapshot: {exc}") from exc
        return cls(_proto=proto)

    def to_json(self) -> str:
        """Serialize this snapshot as canonical, deterministically-sorted JSON."""
        return str(
            json_format.MessageToJson(
                self._pb,
                always_print_fields_with_no_presence=True,
                preserving_proto_field_name=True,
                sort_keys=True,
            )
        )

    def sha256(self) -> str:
        """Canonical SHA-256 hex digest over `to_json()`."""
        if self._sha256 is None:
            self._sha256 = hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
        return self._sha256

    def diff(self, target: KeymapSnapshot) -> keymap_diff.KeymapDiff:
        """Compute the `KeymapDiff` needed to move from `self` to `target`."""
        return keymap_diff._diff_snapshots(self, target)

    # -- read-only views (defensive copies so `self._pb` & `sha256` never drift) --

    @property
    def metadata(self) -> SnapshotMetadata:
        out = SnapshotMetadata()
        out.CopyFrom(self._pb.metadata)
        return out

    @property
    def device(self) -> Device:
        out = Device()
        out.CopyFrom(self._pb.device)
        return out

    @property
    def layout(self) -> PhysicalLayout:
        out = PhysicalLayout()
        out.CopyFrom(self._pb.layout)
        return out

    @property
    def layers(self) -> Sequence[Layer]:
        out: list[Layer] = []
        for layer in self._pb.layers:
            copied = Layer()
            copied.CopyFrom(layer)
            out.append(copied)
        return tuple(out)

    @property
    def behaviors(self) -> Sequence[keyboard_pb2.Behavior]:
        out: list[keyboard_pb2.Behavior] = []
        for beh in self._pb.behaviors:
            copied = keyboard_pb2.Behavior()
            copied.CopyFrom(beh)
            out.append(copied)
        return tuple(out)

    @property
    def captured_iso(self) -> str:
        """RFC-3339 UTC timestamp (`...Z`), or `""` when `captured_at` is unset."""
        ts = self._pb.metadata.captured_at
        if ts.seconds == 0 and ts.nanos == 0:
            return ""
        return ts.ToDatetime().isoformat() + "Z"

    @property
    def layer_names(self) -> Mapping[int, str]:
        """Mapping of `{layer.id: layer.name}` across all layers."""
        return MappingProxyType({layer.id: layer.name for layer in self._pb.layers})
