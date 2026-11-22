"""The published immutable `KeymapSnapshot` domain model, descriptor identity, and JSON codec."""
from __future__ import annotations

import pathlib

import pytest

import zmk_keymap
from zmk_keymap.keymap_snapshot import Device, KeymapSnapshot
from zmk_studio_client import model as client_model


def test_descriptor_uses_qualified_path_and_public_package() -> None:
    """A bare filename in the default descriptor pool collides with other packages."""
    descriptor = KeymapSnapshot.DESCRIPTOR.file
    assert descriptor.name == "zmk_keymap/proto/keymap_snapshot.proto"
    assert descriptor.package == "zmk_keymap.v1"


def test_proto_schema_ships_beside_the_package() -> None:
    proto = pathlib.Path(zmk_keymap.__file__).parent / "proto" / "keymap_snapshot.proto"
    assert proto.is_file()
    assert "package zmk_keymap.v1;" in proto.read_text(encoding="utf-8")


def test_construction_decodes_bindings_and_round_trips_through_json() -> None:
    kp = client_model.Behavior(id=1, display_name="Key Press")
    pset = kp.parameter_sets.add()
    slot = pset.param1.add(name="Key")
    slot.hid_usage.keyboard_max = 0xFF

    snap = KeymapSnapshot(
        device=Device(name="Corne", id="0123456789ab"),
        behaviors=[kp],
        layers=[(0, "BASE", [(0, 1, 0x08070006, 0)])],
    )
    assert snap.layers[0].bindings[0].decoded == "&kp LG(C)"
    assert snap.layers[0].bindings[0].arguments[0].decoded == "LG(C)"
    assert isinstance(hash(snap), int)

    with pytest.raises(AttributeError):
        snap.device = Device(name="Mutated")  # type: ignore[misc]

    encoded = snap.to_json()
    decoded = KeymapSnapshot.from_json(encoded)
    assert decoded == snap
    assert decoded.to_json() == encoded
    assert decoded.sha256() == snap.sha256()
    assert hash(decoded) == hash(snap)


@pytest.mark.parametrize("bad", [
    "not json at all",
    "[]",
    "42",
    '"a string"',
    '{"not_a_snapshot_field": 1}',
    "{" * 2000 + "}" * 2000,
])
def test_from_json_rejects_malformed_inputs_as_value_error(bad: str) -> None:
    with pytest.raises(ValueError):
        KeymapSnapshot.from_json(bad)


def test_from_json_rederives_spoofed_decoded_text_and_rejects_duplicate_behavior_ids() -> None:
    """P0-1 & P0-2 regression: untrusted JSON cannot spoof `decoded` or repeat `behavior.id`."""
    import json

    corpus = pathlib.Path(__file__).resolve().parent / "data" / "sofle-2026-11-10.json"
    honest = KeymapSnapshot.from_json(corpus.read_text(encoding="utf-8"))

    # P0-1: Spoof `decoded` and `behavior_name` to `&kp A` while `behavior_id` points at Bootloader (4).
    raw_spoofed = json.loads(honest.to_json())
    bootloader_id = next(
        b["id"] for b in raw_spoofed["behaviors"] if b["display_name"] == "Bootloader"
    )
    b0 = raw_spoofed["layers"][0]["bindings"][0]
    b0["behavior_id"] = bootloader_id
    b0["behavior_name"] = "Key Press"
    b0["decoded"] = "&kp A"
    reloaded = KeymapSnapshot.from_json(json.dumps(raw_spoofed))
    actual = reloaded.layers[0].bindings[0]
    assert actual.behavior_id == bootloader_id
    assert actual.behavior_name == "Bootloader"
    assert "&bootloader" in actual.decoded
    assert "&kp A" not in actual.decoded

    # P0-2: Duplicate `behavior.id` in `behaviors` is rejected on load.
    raw_dup_id = json.loads(honest.to_json())
    raw_dup_id["behaviors"].append(
        {"id": raw_dup_id["behaviors"][0]["id"], "display_name": "SneakyExtra"}
    )
    with pytest.raises(ValueError, match="appears more than once"):
        KeymapSnapshot.from_json(json.dumps(raw_dup_id))
