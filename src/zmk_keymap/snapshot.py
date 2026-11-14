"""Assembling a KeymapSnapshot from what the keyboard reported.

The library answers in `zmk_studio_client.model`, which describes a live device. A
snapshot describes a backup, so it carries three things a device has no concept of:
provenance, a digest in place of a serial, and a decoded form the device never
sends. This module is the one place those two shapes meet.

The behavior table is not one of the three. It is purely what the keyboard said, so
the schema imports the library's message rather than redeclaring it, and copying
the table in is now `CopyFrom` - the two sides are the same type. They used not to
be, and the difference was a field name.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import typing

from . import decode, model as pb
from zmk_studio_client import STUDIO_MESSAGES_REVISION, model as client_model


class InvalidLayout(ValueError):
    """The keyboard reported an active physical layout index outside its own list."""


class _DeviceInfoLike(typing.Protocol):
    @property
    def name(self) -> str: ...
    @property
    def serial_number(self) -> bytes: ...


def _tool_version() -> str:
    """Our own version, or "unknown" when running from an uninstalled source tree.

    Derived from `__package__` rather than a hardcoded distribution name. The name
    used to be spelled out, and when this package was renamed every snapshot
    silently began recording "unknown" - a valid file, no error, exit 0, and no way
    afterwards to tell which tool wrote it. The broad `except` was what made it
    silent; it is narrowed to the one exception that genuinely means "not installed".
    """
    from importlib.metadata import PackageNotFoundError, version

    if not __package__:
        return "unknown"
    try:
        return version(__package__)
    except PackageNotFoundError:
        return "unknown"


#: decode's parameter-kind spellings -> the ParameterKind enum in the schema.
_KINDS: dict[str, pb.ParameterKind] = {
    decode.CONSTANT: pb.PARAMETER_KIND_CONSTANT,
    decode.RANGE: pb.PARAMETER_KIND_RANGE,
    decode.HID_USAGE: pb.PARAMETER_KIND_HID_USAGE,
    decode.LAYER_ID: pb.PARAMETER_KIND_LAYER_ID,
    decode.UNDECLARED: pb.PARAMETER_KIND_UNDECLARED,
}


def _build_behaviors(
    snapshot: pb.KeymapSnapshot,
    behaviors: Mapping[int, client_model.Behavior],
) -> dict[int, client_model.Behavior]:
    """Copy the behavior table in, and return it keyed by id for the decoder.

    Built before the layers, because decoding a binding means asking the keyboard's
    own description of the behavior it is bound to.

    One `CopyFrom` per entry. This used to be sixteen lines walking a oneof variant
    by variant, because the snapshot declared its own copy of the behavior schema
    and the two differed in a field name. The schema now imports the library's, so
    there is nothing left to translate - and nothing left to get wrong.
    """
    table: dict[int, client_model.Behavior] = {}
    for bid, details in sorted(behaviors.items()):
        out = snapshot.behaviors.add()
        out.CopyFrom(details)
        # The keyboard's reply carries its own id; the table's key is what the
        # caller asked for. They agree today. Set from the key so that a firmware
        # answering out of turn cannot file a behavior under the wrong number.
        out.id = bid
        table[bid] = out
    return table


def _build_layout(
    snapshot: pb.KeymapSnapshot,
    layouts: client_model.PhysicalLayouts,
) -> None:
    """Copy the active physical layout in, with enough to identify it later."""
    if not layouts.layouts:
        return
    index = layouts.active_index
    if index >= len(layouts.layouts):
        raise InvalidLayout(
            f"keyboard reports active layout {index} of {len(layouts.layouts)}; "
            "refusing to guess which layout the key positions belong to")
    active = layouts.layouts[index]
    snapshot.layout.name = active.name
    snapshot.layout.active_index = index
    snapshot.layout.available.extend(one.name for one in layouts.layouts)
    for position, key in enumerate(active.keys):
        out_key = snapshot.layout.keys.add()
        out_key.position = position
        out_key.x, out_key.y = key.x, key.y
        out_key.width, out_key.height = key.width, key.height
        out_key.rotation = key.rotation
        out_key.rotation_x, out_key.rotation_y = key.rotation_x, key.rotation_y


def build(
    info: _DeviceInfoLike,
    keymap: client_model.Keymap,
    layouts: client_model.PhysicalLayouts,
    behaviors: Mapping[int, client_model.Behavior],
    include_serial: bool = False,
) -> pb.KeymapSnapshot:
    """Turn the four reads into one snapshot message.

    `behaviors` maps behavior id to its `model.Behavior`, as `Keyboard.behaviors()`
    returns it.
    """
    snapshot = pb.KeymapSnapshot()

    snapshot.capture.captured_at.GetCurrentTime()
    snapshot.capture.tool_version = _tool_version()
    snapshot.capture.studio_messages_revision = STUDIO_MESSAGES_REVISION

    snapshot.device.name = info.name
    snapshot.device.id = hashlib.sha256(info.serial_number).hexdigest()[:12]
    if include_serial:
        snapshot.device.serial_number = info.serial_number.hex()

    table = _build_behaviors(snapshot, behaviors)
    unknown = client_model.Behavior()

    for order, layer in enumerate(keymap.layers):
        out = snapshot.layers.add()
        out.id = layer.id
        out.display_order = order
        out.name = layer.name
        for position, bound in enumerate(layer.bindings):
            behavior = table.get(bound.behavior_id, unknown)
            rendered = decode.binding(behavior, bound.param1, bound.param2)
            binding = out.bindings.add()
            binding.position = position
            binding.behavior_id = bound.behavior_id
            binding.behavior_name = behavior.display_name
            binding.param1 = bound.param1
            binding.param2 = bound.param2
            binding.rendered = rendered.text
            for param in rendered.params:
                out_param = binding.params.add()
                out_param.name = param.name
                out_param.raw = param.raw
                out_param.decoded = param.decoded
                out_param.kind = _KINDS[param.kind]

    _build_layout(snapshot, layouts)
    return snapshot
