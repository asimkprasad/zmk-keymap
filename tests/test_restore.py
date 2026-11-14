"""The restore sequence, driven against a keyboard made of Python.

Everything here runs with no hardware, because the properties that matter are about
ORDER and are impossible to check by looking at a keyboard afterwards: that nothing
is sent during a dry run, that the pre-flight capture is written before the first
write, that verification happens while a discard still undoes things, and that
save_changes is the last thing to leave.

`_FakeRpc` sits at the `Rpc` seam one floor below `WritableKeyboard`, so it speaks
ZMK's wire protobufs (`zmk_studio_client._pb`) while everything above it - the real
`Keyboard`, `WritableKeyboard`, `Edit`, and `_from_zmk` conversion - runs unmodified.
"""
from __future__ import annotations

import pathlib
import typing

import pytest

from zmk_keymap import from_json, model as pb, restore
from zmk_studio_client import Rpc, model as client_model
from zmk_studio_client.writable import WritableKeyboard
from zmk_studio_client._pb import behaviors_pb2, core_pb2, keymap_pb2

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPUS = sorted(DATA.glob("*.json"))[0]


def _snapshot() -> pb.KeymapSnapshot:
    return from_json(CORPUS.read_text(encoding="utf-8"))


class _Info:
    name = "Sofle"
    serial_number = b"\x01\x02\x03\x04\x05\x06"


class _FakeRpc:
    """A keyboard made of protobufs, substituted at the Rpc seam."""

    def __init__(
        self,
        snapshot: pb.KeymapSnapshot,
        *,
        unsaved: bool = False,
        save_ok: bool = True,
        reject_at: int | None = None,
        apply_writes: bool = True,
        discard_raises: bool = False,
        layouts_message: keymap_pb2.PhysicalLayouts | None = None,
        drop_behavior_id: int | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._unsaved = unsaved
        self._save_ok = save_ok
        self._reject_at = reject_at
        self._apply_writes = apply_writes
        self._discard_raises = discard_raises
        self._drop_behavior_id = drop_behavior_id
        self.sent: list[str] = []
        self.keymap_message = _live_keymap(snapshot)
        self.layouts_message = (
            layouts_message if layouts_message is not None else _layouts(snapshot)
        )
        self.closed = False

    def call(self, request: typing.Any, timeout: float = 10.0) -> typing.Any:
        kind = "write" if type(request).__name__ == "WriteRequest" else "read"
        self.sent.append(f"{kind}:{request.field}")
        handler = getattr(self, f"_{request.field}")
        return handler(request)

    def close(self) -> None:
        self.closed = True

    # -- reads ---------------------------------------------------------------

    def _get_device_info(self, request: typing.Any) -> core_pb2.Response:
        return core_pb2.Response(get_device_info=core_pb2.GetDeviceInfoResponse(
            name=_Info.name, serial_number=_Info.serial_number))

    def _check_unsaved_changes(self, request: typing.Any) -> keymap_pb2.Response:
        return keymap_pb2.Response(check_unsaved_changes=self._unsaved)

    def _get_keymap(self, request: typing.Any) -> keymap_pb2.Response:
        return keymap_pb2.Response(get_keymap=self.keymap_message)

    def _get_physical_layouts(self, request: typing.Any) -> keymap_pb2.Response:
        return keymap_pb2.Response(get_physical_layouts=self.layouts_message)

    def _list_all_behaviors(self, request: typing.Any) -> behaviors_pb2.Response:
        ids = [
            b.id for b in self._snapshot.behaviors
            if b.id != self._drop_behavior_id
        ]
        return behaviors_pb2.Response(
            list_all_behaviors=behaviors_pb2.ListAllBehaviorsResponse(behaviors=ids))

    def _get_behavior_details(self, request: typing.Any) -> behaviors_pb2.Response:
        """Return the LIVE shape (`metadata`), not the snapshot shape (`parameter_sets`)."""
        wanted = request.message.behaviors.get_behavior_details.behavior_id
        stored = {b.id: b for b in self._snapshot.behaviors}[wanted]
        live = behaviors_pb2.GetBehaviorDetailsResponse(
            id=stored.id, display_name=stored.display_name)
        for parameter_set in stored.parameter_sets:
            raw = parameter_set.SerializeToString()
            assert live.metadata.add().ParseFromString(raw) == len(raw), (
                "snapshot ParameterSet and live BehaviorBindingParametersSet have "
                "drifted apart; this fake is no longer faithful")
        return behaviors_pb2.Response(get_behavior_details=live)

    # -- writes --------------------------------------------------------------

    def _set_layer_binding(self, request: typing.Any) -> keymap_pb2.Response:
        written = sum(1 for s in self.sent if s == "write:set_layer_binding")
        if self._reject_at is not None and written == self._reject_at:
            return keymap_pb2.Response(
                set_layer_binding=keymap_pb2.SET_LAYER_BINDING_RESP_INVALID_BEHAVIOR)
        if self._apply_writes:
            sent = request.message.keymap.set_layer_binding
            layer = {l.id: l for l in self.keymap_message.layers}[sent.layer_id]
            entry = layer.bindings[sent.key_position]
            entry.behavior_id = sent.binding.behavior_id
            entry.param1, entry.param2 = sent.binding.param1, sent.binding.param2
        return keymap_pb2.Response(
            set_layer_binding=keymap_pb2.SET_LAYER_BINDING_RESP_OK)

    def _save_changes(self, request: typing.Any) -> keymap_pb2.Response:
        response = keymap_pb2.Response()
        if self._save_ok:
            response.save_changes.ok = True
        else:
            response.save_changes.err = keymap_pb2.SAVE_CHANGES_ERR_NO_SPACE
        return response

    def _discard_changes(self, request: typing.Any) -> keymap_pb2.Response:
        if self._discard_raises:
            raise OSError("link dropped")
        return keymap_pb2.Response(discard_changes=True)


class _FakeSession(WritableKeyboard):
    """The real WritableKeyboard, over a keyboard made of protobufs."""

    def __init__(self, snapshot: pb.KeymapSnapshot, **kwargs: typing.Any) -> None:
        self._fake = _FakeRpc(snapshot, **kwargs)
        super().__init__(typing.cast(Rpc, self._fake))

    @property
    def sent(self) -> list[str]:
        return self._fake.sent

    @property
    def keymap_message(self) -> keymap_pb2.Keymap:
        return self._fake.keymap_message

    @property
    def layouts_message(self) -> keymap_pb2.PhysicalLayouts:
        return self._fake.layouts_message

    @property
    def writes(self) -> list[str]:
        return [s for s in self.sent if s.startswith("write:")]


def _live_keymap(snapshot: pb.KeymapSnapshot) -> keymap_pb2.Keymap:
    keymap = keymap_pb2.Keymap()
    for layer in snapshot.layers:
        out = keymap.layers.add()
        out.id, out.name = layer.id, layer.name
        for binding in sorted(layer.bindings, key=lambda b: b.position):
            entry = out.bindings.add()
            entry.behavior_id = binding.behavior_id
            entry.param1, entry.param2 = binding.param1, binding.param2
    return keymap


def _layouts(snapshot: pb.KeymapSnapshot) -> keymap_pb2.PhysicalLayouts:
    layouts = keymap_pb2.PhysicalLayouts()
    layouts.active_layout_index = 0
    layout = layouts.layouts.add()
    layout.name = snapshot.layout.name
    for key in snapshot.layout.keys:
        out = layout.keys.add()
        out.width, out.height = key.width, key.height
        out.x, out.y = key.x, key.y
        out.r, out.rx, out.ry = key.rotation, key.rotation_x, key.rotation_y
    return layouts


def _diverge(session: _FakeSession, layer_index: int = 0, position: int = 0) -> None:
    """Make the live keyboard differ from the snapshot by exactly one key."""
    session.keymap_message.layers[layer_index].bindings[position].param1 ^= 0xFF


# --------------------------------------------------------------------------- #
# planning sends nothing
# --------------------------------------------------------------------------- #

def test_planning_sends_no_writes() -> None:
    """The default path must be incapable of changing anything."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    restore.plan(session, want)
    assert session.writes == []
    assert "read:get_physical_layouts" in session.sent


def test_planning_against_a_matching_keyboard_finds_nothing() -> None:
    want = _snapshot()
    proposal = restore.plan(_FakeSession(want), want)
    assert proposal.is_empty


def test_planning_finds_the_one_divergence() -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    proposal = restore.plan(session, want)
    assert len(proposal.changes) == 1


def test_an_open_studio_session_is_refused() -> None:
    """Committing someone else's unsaved edits is not this tool's call to make."""
    want = _snapshot()
    with pytest.raises(restore.RestoreRefused, match="unsaved changes"):
        restore.plan(_FakeSession(want, unsaved=True), want)


def test_a_missing_bound_behavior_is_translated_to_restore_refused() -> None:
    """Callers of `restore.plan()` catch one refusal type, not `remap`'s internals."""
    want = _snapshot()
    bound_id = want.layers[0].bindings[0].behavior_id
    with pytest.raises(restore.RestoreRefused, match="no behavior matching"):
        restore.plan(_FakeSession(want, drop_behavior_id=bound_id), want)


def test_a_missing_layer_is_translated_to_restore_refused() -> None:
    want = _snapshot()
    session = _FakeSession(want)
    del session.keymap_message.layers[-1]
    with pytest.raises(restore.RestoreRefused, match="no layer matching"):
        restore.plan(session, want)


def test_a_different_active_layout_on_the_same_board_is_refused_with_a_hint() -> None:
    want = _snapshot()
    layouts = _layouts(want)
    other = layouts.layouts.add()
    other.CopyFrom(layouts.layouts[0])
    other.name = "ISO"
    layouts.active_layout_index = 1
    with pytest.raises(restore.RestoreRefused, match="Switch to that layout in ZMK Studio"):
        restore.plan(_FakeSession(want, layouts_message=layouts), want)


def test_a_different_board_layout_name_is_refused() -> None:
    want = _snapshot()
    layouts = _layouts(want)
    layouts.layouts[0].name = "Corne"
    with pytest.raises(restore.RestoreRefused, match="different keyboard or layout"):
        restore.plan(_FakeSession(want, layouts_message=layouts), want)


def test_a_larger_live_physical_layout_is_refused() -> None:
    """A 42-key snapshot onto a 60-key layout would fit in `live.bindings` and corrupt silently."""
    want = _snapshot()
    while len(want.layout.keys) > 42:
        want.layout.keys.pop()
    for layer in want.layers:
        kept = [b for b in layer.bindings if b.position < 42]
        del layer.bindings[:]
        layer.bindings.extend(kept)
    with pytest.raises(restore.RestoreRefused, match="42 keys.*60 keys"):
        restore.plan(_FakeSession(_snapshot()), want)


def test_a_sparse_snapshot_with_matching_physical_layout_still_plans() -> None:
    """Refusal is anchored on `PhysicalLayout`, so sparse `layer.bindings` stay valid."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session, layer_index=0, position=0)
    # Keep only the first binding on layer 0; layout geometry still matches all 60 keys.
    first = pb.Binding()
    first.CopyFrom(want.layers[0].bindings[0])
    del want.layers[0].bindings[:]
    want.layers[0].bindings.append(first)
    proposal = restore.plan(session, want)
    assert len(proposal.changes) == 1


def test_a_different_keyboard_warns_but_does_not_refuse() -> None:
    """Restoring onto a second identical board is a real thing to want."""
    want = _snapshot()
    want.device.id = "ffffffffffff"
    proposal = restore.plan(_FakeSession(want), want)
    assert proposal.identity_warning
    assert "ffffffffffff" in proposal.identity_warning


def test_the_same_keyboard_does_not_warn() -> None:
    want = _snapshot()
    assert want.device.id == restore.device_id(_Info())
    assert restore.plan(_FakeSession(want), want).identity_warning is None


def test_the_device_id_matches_what_a_snapshot_records() -> None:
    """If these drifted, every restore would warn about identity for no reason."""
    from zmk_keymap import snapshot as builder

    built = builder.build(
        client_model.DeviceInfo(name=_Info.name, serial_number=_Info.serial_number),
        client_model.Keymap(),
        client_model.PhysicalLayouts(),
        {},
    )
    assert built.device.id == restore.device_id(_Info())


# --------------------------------------------------------------------------- #
# applying: order is the design
# --------------------------------------------------------------------------- #

def test_apply_writes_only_what_differs(tmp_path) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert session.writes.count("write:set_layer_binding") == 1


def test_save_changes_is_the_last_thing_sent(tmp_path) -> None:
    """The irreversible step must come after every check."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert session.writes[-1] == "write:save_changes"
    assert "write:discard_changes" not in session.writes


def test_the_keyboard_is_re_read_before_it_is_committed(tmp_path) -> None:
    """Verification after save_changes would be an audit, not a safeguard."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    restore.apply(session, want, restore.plan(session, want), tmp_path)
    save = session.sent.index("write:save_changes")
    assert "read:get_keymap" in session.sent[session.sent.index("write:set_layer_binding"):save]


def test_a_backup_is_written_before_the_first_write(tmp_path) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    path = restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert path.exists()
    assert path.parent == tmp_path
    assert "before-restore-" in path.name


def test_the_backup_is_a_loadable_snapshot_of_the_old_state(tmp_path: pathlib.Path) -> None:
    """A pre-flight file that cannot be restored is not an undo."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    path = restore.apply(session, want, restore.plan(session, want), tmp_path)
    recovered = from_json(path.read_text(encoding="utf-8"))
    assert len(recovered.layers) == len(want.layers)
    old = recovered.layers[0].bindings[0]
    assert old.param1 == want.layers[0].bindings[0].param1 ^ 0xFF


def test_restoring_nothing_is_refused_rather_than_committed(tmp_path) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    with pytest.raises(restore.RestoreRefused, match="already matches"):
        restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert session.writes == []


# --------------------------------------------------------------------------- #
# failure paths: every one ends in discard
# --------------------------------------------------------------------------- #

def test_a_rejected_binding_rolls_back(tmp_path) -> None:
    want = _snapshot()
    session = _FakeSession(want, reject_at=1)
    _diverge(session)
    with pytest.raises(restore.RestoreFailed, match="INVALID_BEHAVIOR"):
        restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert session.writes[-1] == "write:discard_changes"
    assert "write:save_changes" not in session.writes


def test_a_write_that_does_not_take_effect_rolls_back(tmp_path) -> None:
    """The keyboard says OK and nothing changed. Verification is what catches it."""
    want = _snapshot()
    session = _FakeSession(want, apply_writes=False)
    _diverge(session)
    with pytest.raises(restore.RestoreFailed, match="did not take effect"):
        restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert session.writes[-1] == "write:discard_changes"
    assert "write:save_changes" not in session.writes


def test_a_failed_commit_rolls_back_and_says_so(tmp_path) -> None:
    want = _snapshot()
    session = _FakeSession(want, save_ok=False)
    _diverge(session)
    with pytest.raises(restore.RestoreFailed, match="NO_SPACE"):
        restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert session.writes[-1] == "write:discard_changes"


def test_a_failed_rollback_says_how_to_recover(tmp_path) -> None:
    """The worst case. The reader needs to be told power-cycling clears RAM."""
    want = _snapshot()
    session = _FakeSession(want, reject_at=1, discard_raises=True)
    _diverge(session)
    with pytest.raises(restore.RestoreFailed, match="power-cycling"):
        restore.apply(session, want, restore.plan(session, want), tmp_path)


def test_every_failure_path_names_the_backup(tmp_path) -> None:
    """An error that does not say where the undo lives is half an error."""
    want = _snapshot()
    session = _FakeSession(want, save_ok=False)
    _diverge(session)
    with pytest.raises(restore.RestoreFailed) as caught:
        restore.apply(session, want, restore.plan(session, want), tmp_path)
    assert "before-restore-" in str(caught.value)
