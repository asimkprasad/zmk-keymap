"""Assembling a snapshot, end to end, against a keyboard made of bytes.

The fake is a Connection - three methods, bytes in and out - so the real session
runs unmodified above it. Substituting at that boundary rather than by subclassing
the session is what keeps framing, request construction, response matching, decoding
and assembly all under test, with no port, no BLE and no keyboard.

Because `FakeConnection` speaks raw transport frames below `Wire`, this test file
intentionally imports `zmk_studio_client.wire` and `zmk_studio_client._pb` to frame
and parse ZMK's wire messages.
"""
from __future__ import annotations

import pytest

import zmk_keymap as backup
from zmk_studio_client import READS, Keyboard, Rpc, Wire
import zmk_studio_client.wire as wire
from zmk_studio_client._pb import behaviors_pb2, core_pb2, studio_pb2

BEHAVIORS: dict[int, tuple[str, list[tuple[list[tuple[str, str]], list[tuple[str, str]]]]]] = {
    1: ("Key Press", [([("Key", "hid_usage")], [])]),
    2: ("Mod-Tap", [([("Key", "hid_usage")], [("Key", "hid_usage")])]),
    3: ("External Power", []),            # reports no metadata, as on real firmware
}


def _behavior_details(behavior_id: int) -> behaviors_pb2.GetBehaviorDetailsResponse:
    name, sets = BEHAVIORS[behavior_id]
    out = behaviors_pb2.GetBehaviorDetailsResponse(id=behavior_id, display_name=name)
    for slot1, slot2 in sets:
        parameter_set = out.metadata.add()
        for declared, specs in ((parameter_set.param1, slot1), (parameter_set.param2, slot2)):
            for param_name, kind in specs:
                described = declared.add()
                described.name = param_name
                getattr(described, kind).SetInParent()
    return out


class FakeConnection:
    """A keyboard made of bytes. Satisfies Connection by shape; inherits nothing."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self._outbox = b""

    def send(self, data: bytes) -> None:
        [payload] = wire.FrameReader().feed(data)
        request = studio_pb2.Request()
        request.ParseFromString(payload)
        subsystem = request.WhichOneof("subsystem")
        assert subsystem is not None
        field = getattr(request, subsystem).WhichOneof("request_type")
        assert field is not None
        self.requests.append((subsystem, field))

        response = studio_pb2.Response()
        response.request_response.request_id = request.request_id
        answer = getattr(response.request_response, subsystem)
        if field == "get_device_info":
            answer.get_device_info.CopyFrom(
                core_pb2.GetDeviceInfoResponse(name="Fake", serial_number=b"\x01\x02"))
        elif field == "get_keymap":
            keymap = answer.get_keymap
            layer = keymap.layers.add()
            layer.id, layer.name = 4, "BASE"
            layer.bindings.add(behavior_id=1, param1=0x00070006)          # &kp C
            layer.bindings.add(behavior_id=2, param1=0x08070006, param2=0x00070006)
            layer.bindings.add(behavior_id=3, param1=2)                   # &ext_power 2
        elif field == "get_physical_layouts":
            layouts = answer.get_physical_layouts
            layouts.active_layout_index = 1
            layouts.layouts.add(name="Other")
            chosen = layouts.layouts.add(name="Fake Layout")
            chosen.keys.add(width=100, height=100, x=0, y=0)
            chosen.keys.add(width=100, height=100, x=100, y=0)
            chosen.keys.add(width=150, height=100, x=200, y=0)
        elif field == "list_all_behaviors":
            answer.list_all_behaviors.behaviors.extend(sorted(BEHAVIORS))
        elif field == "get_behavior_details":
            answer.get_behavior_details.CopyFrom(
                _behavior_details(request.behaviors.get_behavior_details.behavior_id))
        else:
            raise AssertionError(f"unexpected read: {subsystem}.{field}")
        self._outbox = wire.frame(response.SerializeToString())

    def receive(self, timeout: float) -> bytes:
        out, self._outbox = self._outbox, b""
        return out

    def close(self) -> None:
        pass


@pytest.fixture
def keyboard(monkeypatch) -> FakeConnection:
    """The real session, over a fake connection. Only the bottom floor is replaced."""
    fake = FakeConnection()
    monkeypatch.setattr(backup, "connect", lambda: Keyboard(Rpc(Wire(fake))))
    return fake


@pytest.fixture
def snapshot(keyboard):
    return backup.dump()


def test_only_reads_are_sent(keyboard, snapshot) -> None:
    """The read-only guarantee observed at the wire, not at the whitelist."""
    assert keyboard.requests
    assert set(keyboard.requests) <= READS


def test_bindings_decode_through_the_behavior_table(snapshot) -> None:
    rendered = [b.rendered for b in snapshot.layers[0].bindings]
    assert rendered == ["&kp C", "&mt LG(C) C", "&ext_power 2"]


def test_each_binding_keeps_its_raw_parameters(snapshot) -> None:
    mod_tap = snapshot.layers[0].bindings[1]
    assert (mod_tap.param1, mod_tap.param2) == (0x08070006, 0x00070006)
    assert [(p.name, p.decoded) for p in mod_tap.params] == [("Key", "LG(C)"), ("Key", "C")]


def test_the_active_layout_is_identified_not_just_named(snapshot) -> None:
    """Binding positions index into this layout, so which one it was has to survive."""
    assert snapshot.layout.name == "Fake Layout"
    assert snapshot.layout.active_index == 1
    assert list(snapshot.layout.available) == ["Other", "Fake Layout"]
    assert len(snapshot.layout.keys) == 3


def test_layer_id_and_display_order_are_both_kept(snapshot) -> None:
    assert (snapshot.layers[0].id, snapshot.layers[0].display_order) == (4, 0)


def test_the_serial_is_a_digest_by_default(snapshot) -> None:
    assert snapshot.device.serial_number == ""
    assert len(snapshot.device.id) == 12


def test_json_is_deterministic(snapshot) -> None:
    first = backup.to_json(snapshot)
    assert first == backup.to_json(snapshot)
    assert '"rendered": "&mt LG(C) C"' in first
