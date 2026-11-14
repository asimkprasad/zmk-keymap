"""The published archive model, its descriptor identity, and JSON round-tripping."""
from __future__ import annotations

import pathlib

import pytest

import zmk_keymap
from zmk_keymap import from_json, model, to_json


def test_descriptor_uses_qualified_path_and_public_package() -> None:
    """A bare filename in the default descriptor pool collides with other packages."""
    descriptor = model.KeymapSnapshot.DESCRIPTOR.file
    assert descriptor.name == "zmk_keymap/proto/keymap_snapshot.proto"
    assert descriptor.package == "zmk_keymap.v1"


def test_proto_schema_ships_beside_the_package() -> None:
    proto = pathlib.Path(zmk_keymap.__file__).parent / "proto" / "keymap_snapshot.proto"
    assert proto.is_file()
    assert "package zmk_keymap.v1;" in proto.read_text(encoding="utf-8")


def test_to_json_and_from_json_round_trip() -> None:
    snap = model.KeymapSnapshot()
    snap.device.name = "Corne"
    snap.device.id = "0123456789ab"
    layer = snap.layers.add(id=0, display_order=0, name="BASE")
    layer.bindings.add(position=0, behavior_id=1, behavior_name="Key Press",
                       param1=0x00070004, rendered="&kp A")

    encoded = to_json(snap)
    decoded = from_json(encoded)
    assert decoded == snap
    assert to_json(decoded) == encoded


@pytest.mark.parametrize("bad", [
    "not json at all",
    "[]",
    "42",
    '"a string"',
    '{"not_a_snapshot_field": 1}',
])
def test_from_json_rejects_malformed_inputs_as_value_error(bad: str) -> None:
    with pytest.raises(ValueError):
        from_json(bad)
