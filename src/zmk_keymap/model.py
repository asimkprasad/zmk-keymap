"""The snapshot archive model and its canonical JSON representation.

Everything stored in a snapshot file is defined in `proto/keymap_snapshot.proto`
under `zmk_keymap.v1`, importing `zmk_studio_client.v1.Behavior` for the behavior
table. Callers import message types and `to_json` / `from_json` from here rather
than reaching into generated `_pb2` modules directly.

This module references `zmk_studio_client` only for that protobuf descriptor
(`zmk_studio_client.proto.keyboard_pb2`) and exposes none of its runtime or
transports, so `render` and `cli_render` have no call path to a device connection.
(Importing `zmk_studio_client.proto.keyboard_pb2` or `zmk_keymap` still executes
their package `__init__.py` files, loading `zmk_studio_client`'s read package into
`sys.modules`.)

The `.proto` schema ships inside the wheel so tools in other languages can
generate bindings directly:

    python3 -c 'import zmk_keymap, pathlib;
                print(pathlib.Path(zmk_keymap.__file__).parent / "proto")'
"""
from __future__ import annotations

import json

from google.protobuf import json_format

from .proto.keymap_snapshot_pb2 import (
    PARAMETER_KIND_CONSTANT,
    PARAMETER_KIND_HID_USAGE,
    PARAMETER_KIND_LAYER_ID,
    PARAMETER_KIND_RANGE,
    PARAMETER_KIND_UNDECLARED,
    PARAMETER_KIND_UNSPECIFIED,
    Binding,
    Capture,
    Device,
    KeyPosition,
    KeymapSnapshot,
    Layer,
    ParameterKind,
    PhysicalLayout,
    RenderedParam,
)

__all__ = [
    "Binding",
    "Capture",
    "Device",
    "KeyPosition",
    "KeymapSnapshot",
    "Layer",
    "ParameterKind",
    "PhysicalLayout",
    "RenderedParam",
    "PARAMETER_KIND_UNSPECIFIED",
    "PARAMETER_KIND_CONSTANT",
    "PARAMETER_KIND_RANGE",
    "PARAMETER_KIND_HID_USAGE",
    "PARAMETER_KIND_LAYER_ID",
    "PARAMETER_KIND_UNDECLARED",
    "to_json",
    "from_json",
]


def to_json(snapshot: KeymapSnapshot) -> str:
    """Serialise a snapshot as JSON, in protobuf's canonical mapping.

    Fields at their default value are written out rather than omitted, so a snapshot
    read by eye shows every position even where a value happens to be zero. Keys are
    sorted, because protobuf otherwise appends defaulted fields after the rest and the
    key order then varies with the data - which makes diffing two snapshots noisy.
    """
    return str(
        json_format.MessageToJson(
            snapshot,
            always_print_fields_with_no_presence=True,
            preserving_proto_field_name=True,
            sort_keys=True,
        )
    )


def from_json(raw: str) -> KeymapSnapshot:
    """Parse a JSON string written by `to_json` back into a KeymapSnapshot.

    Raises ValueError when `raw` is not valid JSON or does not conform to the
    snapshot schema, so callers need not import `google.protobuf.json_format`
    to handle malformed files.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("not a keymap snapshot: expected a JSON object at top level")
    try:
        return json_format.ParseDict(parsed, KeymapSnapshot())
    except json_format.ParseError as exc:
        raise ValueError(f"not a keymap snapshot: {exc}") from exc
