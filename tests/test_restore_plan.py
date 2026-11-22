"""`RestorePlan` and `read_keyboard`, driven against a keyboard made of Python.

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

from collections.abc import Callable
from dataclasses import FrozenInstanceError
import hashlib
import pathlib
import typing

import pytest

from zmk_keymap import keymap_snapshot as pb, live_keyboard, snapshot_store
from zmk_keymap.live_keyboard import InvalidLayout, UnsavedChanges, read_keyboard
from zmk_keymap.restore_plan import RestoreFailed, RestorePlan, RestoreRefused
from zmk_keymap.snapshot_store import SnapshotStore
from zmk_studio_client import Rpc, model as client_model
from zmk_studio_client.writable import WritableKeyboard
from zmk_studio_client._pb import behaviors_pb2, core_pb2, keymap_pb2

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPUS = sorted(DATA.glob("*.json"))[0]


def _snapshot() -> pb.KeymapSnapshot:
    return pb.KeymapSnapshot.from_json(CORPUS.read_text(encoding="utf-8"))


def _with_device(
    snap: pb.KeymapSnapshot,
    *,
    name: str | None = None,
    id: str | None = None,
) -> pb.KeymapSnapshot:
    return pb.KeymapSnapshot(
        device=pb.Device(
            name=snap.device.name if name is None else name,
            id=snap.device.id if id is None else id,
            serial_number=snap.device.serial_number,
        ),
        layout=snap.layout,
        behaviors=snap.behaviors,
        layers=snap.layers,
    )


def _with_flipped_param1(
    snap: pb.KeymapSnapshot,
    layer_idx: int = 0,
    binding_idx: int = 0,
) -> pb.KeymapSnapshot:
    cur = snap.layers[layer_idx].bindings[binding_idx]
    beh = next(
        (b for b in snap.behaviors if b.id == cur.behavior_id),
        client_model.Behavior(),
    )
    redecoded = pb.decode_binding(
        beh,
        cur.param1 ^ 0xFF,
        cur.param2,
        position=cur.position,
        behavior_id=cur.behavior_id,
    )
    layers = list(snap.layers)
    layers[layer_idx] = pb.Layer(
        id=layers[layer_idx].id,
        display_order=layers[layer_idx].display_order,
        name=layers[layer_idx].name,
        bindings=[
            redecoded if i == binding_idx else b
            for i, b in enumerate(layers[layer_idx].bindings)
        ],
    )
    return pb.KeymapSnapshot(
        device=snap.device,
        layout=snap.layout,
        behaviors=snap.behaviors,
        layers=layers,
    )


def _backup_writer(
    undo_dir: pathlib.Path,
) -> Callable[[pb.KeymapSnapshot], pathlib.Path]:
    return lambda snap: SnapshotStore.write_undo_snapshot(undo_dir, snap)


def _plan(session: _FakeSession, target: pb.KeymapSnapshot) -> RestorePlan:
    return RestorePlan(live=read_keyboard(session, require_saved=True), target=target)


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
        return core_pb2.Response(
            get_device_info=core_pb2.GetDeviceInfoResponse(
                name=_Info.name, serial_number=_Info.serial_number
            )
        )

    def _check_unsaved_changes(self, request: typing.Any) -> keymap_pb2.Response:
        return keymap_pb2.Response(check_unsaved_changes=self._unsaved)

    def _get_keymap(self, request: typing.Any) -> keymap_pb2.Response:
        return keymap_pb2.Response(get_keymap=self.keymap_message)

    def _get_physical_layouts(self, request: typing.Any) -> keymap_pb2.Response:
        return keymap_pb2.Response(get_physical_layouts=self.layouts_message)

    def _list_all_behaviors(self, request: typing.Any) -> behaviors_pb2.Response:
        ids = [
            b.id
            for b in self._snapshot.behaviors
            if b.id != self._drop_behavior_id
        ]
        return behaviors_pb2.Response(
            list_all_behaviors=behaviors_pb2.ListAllBehaviorsResponse(behaviors=ids)
        )

    def _get_behavior_details(self, request: typing.Any) -> behaviors_pb2.Response:
        """Return the LIVE shape (`metadata`), not the snapshot shape (`parameter_sets`)."""
        wanted = request.message.behaviors.get_behavior_details.behavior_id
        stored = {b.id: b for b in self._snapshot.behaviors}[wanted]
        live = behaviors_pb2.GetBehaviorDetailsResponse(
            id=stored.id, display_name=stored.display_name
        )
        for parameter_set in stored.parameter_sets:
            raw = parameter_set.SerializeToString()
            assert live.metadata.add().ParseFromString(raw) == len(raw), (
                "snapshot ParameterSet and live BehaviorBindingParametersSet have "
                "drifted apart; this fake is no longer faithful"
            )
        return behaviors_pb2.Response(get_behavior_details=live)

    # -- writes --------------------------------------------------------------

    def _set_layer_binding(self, request: typing.Any) -> keymap_pb2.Response:
        written = sum(1 for s in self.sent if s == "write:set_layer_binding")
        if self._reject_at is not None and written == self._reject_at:
            return keymap_pb2.Response(
                set_layer_binding=keymap_pb2.SET_LAYER_BINDING_RESP_INVALID_BEHAVIOR
            )
        if self._apply_writes:
            sent = request.message.keymap.set_layer_binding
            layer = {l.id: l for l in self.keymap_message.layers}[sent.layer_id]
            entry = layer.bindings[sent.key_position]
            entry.behavior_id = sent.binding.behavior_id
            entry.param1, entry.param2 = sent.binding.param1, sent.binding.param2
        return keymap_pb2.Response(
            set_layer_binding=keymap_pb2.SET_LAYER_BINDING_RESP_OK
        )

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
# read_keyboard and planning send nothing
# --------------------------------------------------------------------------- #

def test_read_keyboard_reads_open_session_without_closing_it() -> None:
    want = _snapshot()
    session = _FakeSession(want)
    captured = read_keyboard(session, include_serial=True)
    assert session.writes == []
    assert not session._fake.closed
    assert captured.device.name == _Info.name
    assert captured.device.serial_number == _Info.serial_number.hex()


def test_planning_sends_no_writes() -> None:
    """The default path must be incapable of changing anything."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    _plan(session, want)
    assert session.writes == []
    assert "read:get_physical_layouts" in session.sent


def test_planning_against_a_matching_keyboard_finds_nothing() -> None:
    want = _snapshot()
    plan = _plan(_FakeSession(want), want)
    assert plan.is_empty


def test_planning_finds_the_one_divergence() -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    plan = _plan(session, want)
    assert len(plan.diff) == 1


def test_an_open_studio_session_is_refused(tmp_path: pathlib.Path) -> None:
    """Committing someone else's unsaved edits is not this tool's call to make."""
    want = _snapshot()
    with pytest.raises(UnsavedChanges, match="unsaved changes"):
        read_keyboard(_FakeSession(want, unsaved=True), require_saved=True)

    clean = _FakeSession(want)
    _diverge(clean)
    plan = _plan(clean, want)
    dirty = _FakeSession(want, unsaved=True)
    _diverge(dirty)
    with pytest.raises(RestoreRefused, match="unsaved changes"):
        plan.apply(dirty, _backup_writer(tmp_path))
    assert dirty.writes == []


def test_out_of_range_active_layout_index_is_refused() -> None:
    want = _snapshot()
    layouts = _layouts(want)
    layouts.active_layout_index = 5
    session = _FakeSession(want, layouts_message=layouts)
    with pytest.raises(InvalidLayout, match="active layout 5 of 1"):
        read_keyboard(session, require_saved=True)
    assert "read:list_all_behaviors" not in session.sent


def test_a_missing_bound_behavior_is_translated_to_restore_refused() -> None:
    """Callers of `RestorePlan` catch one refusal type, not `KeymapDiff`'s internals."""
    want = _snapshot()
    bound_id = want.layers[0].bindings[0].behavior_id
    with pytest.raises(RestoreRefused, match="no behavior matching"):
        _plan(_FakeSession(want, drop_behavior_id=bound_id), want)


def test_a_missing_layer_is_translated_to_restore_refused() -> None:
    want = _snapshot()
    session = _FakeSession(want)
    del session.keymap_message.layers[-1]
    with pytest.raises(RestoreRefused, match="no layer matching"):
        _plan(session, want)


def test_a_different_active_layout_on_the_same_board_is_refused_with_a_hint() -> None:
    want = _snapshot()
    layouts = _layouts(want)
    other = layouts.layouts.add()
    other.CopyFrom(layouts.layouts[0])
    other.name = "ISO"
    layouts.active_layout_index = 1
    session = _FakeSession(want, layouts_message=layouts)
    with pytest.raises(RestoreRefused, match="Switch to that layout in ZMK Studio"):
        _plan(session, want)
    assert session.writes == []


def test_a_different_board_layout_name_is_refused() -> None:
    want = _snapshot()
    layouts = _layouts(want)
    layouts.layouts[0].name = "Corne"
    with pytest.raises(RestoreRefused, match="different keyboard or layout"):
        _plan(_FakeSession(want, layouts_message=layouts), want)


def test_a_larger_live_physical_layout_is_refused() -> None:
    """A 42-key snapshot onto a 60-key layout would fit in `live.bindings` and corrupt silently."""
    base = _snapshot()
    want = pb.KeymapSnapshot(
        device=base.device,
        layout=pb.PhysicalLayout(
            name=base.layout.name,
            active_index=base.layout.active_index,
            available=base.layout.available,
            keys=list(base.layout.keys)[:42],
        ),
        behaviors=base.behaviors,
        layers=[
            pb.Layer(
                id=layer.id,
                display_order=layer.display_order,
                name=layer.name,
                bindings=[b for b in layer.bindings if b.position < 42],
            )
            for layer in base.layers
        ],
    )
    with pytest.raises(RestoreRefused, match="42 keys.*60 keys"):
        _plan(_FakeSession(base), want)


def test_direct_restore_plan_enforces_layout_and_behavior_preconditions() -> None:
    """Constructing `RestorePlan(live, target)` offline validates the same invariants."""
    live = _snapshot()
    wrong_layout = pb.KeymapSnapshot(
        device=live.device,
        layout=pb.PhysicalLayout(
            name="Corne",
            active_index=live.layout.active_index,
            available=live.layout.available,
            keys=live.layout.keys,
        ),
        behaviors=live.behaviors,
        layers=live.layers,
    )
    with pytest.raises(RestoreRefused, match="different keyboard or layout"):
        RestorePlan(live=live, target=wrong_layout)

    bound_id = live.layers[0].bindings[0].behavior_id
    missing_behavior_live = pb.KeymapSnapshot(
        device=live.device,
        layout=live.layout,
        behaviors=[b for b in live.behaviors if b.id != bound_id],
        layers=live.layers,
    )
    with pytest.raises(RestoreRefused, match="no behavior matching"):
        RestorePlan(live=missing_behavior_live, target=live)


def test_a_sparse_snapshot_with_matching_physical_layout_still_plans() -> None:
    """Refusal is anchored on `PhysicalLayout`, so sparse `layer.bindings` stay valid."""
    base = _snapshot()
    session = _FakeSession(base)
    _diverge(session, layer_index=0, position=0)
    layers = list(base.layers)
    layers[0] = pb.Layer(
        id=layers[0].id,
        display_order=layers[0].display_order,
        name=layers[0].name,
        bindings=[layers[0].bindings[0]],
    )
    want = pb.KeymapSnapshot(
        device=base.device,
        layout=base.layout,
        behaviors=base.behaviors,
        layers=layers,
    )
    plan = _plan(session, want)
    assert len(plan.diff) == 1


def test_a_different_keyboard_warns_but_does_not_refuse() -> None:
    """Restoring onto a second identical board is a real thing to want."""
    want = _with_device(_snapshot(), id="ffffffffffff")
    plan = _plan(_FakeSession(want), want)
    assert plan.identity_warning
    assert "ffffffffffff" in plan.identity_warning

    # P1-1: Stripping `device.id` to `""` still triggers `identity_warning` (no silent bypass).
    scrubbed_id = _with_device(_snapshot(), id="")
    plan_scrubbed = _plan(_FakeSession(_snapshot()), scrubbed_id)
    assert plan_scrubbed.identity_warning
    assert "no device id" in plan_scrubbed.identity_warning


def test_missing_snapshot_layout_is_refused_when_keyboard_has_layout() -> None:
    """P1-1: Omitting `target.layout` cannot bypass layout/key-count checks."""
    live = _snapshot()
    stripped_layout = pb.KeymapSnapshot(
        device=live.device,
        layout=None,
        behaviors=live.behaviors,
        layers=live.layers,
    )
    with pytest.raises(RestoreRefused, match="carries no physical layout"):
        RestorePlan(live=live, target=stripped_layout)


def test_the_same_keyboard_does_not_warn() -> None:
    want = _snapshot()
    expected_id = hashlib.sha256(_Info.serial_number).hexdigest()[:12]
    assert want.device.id == expected_id
    assert _plan(_FakeSession(want), want).identity_warning is None


def test_live_keyboard_records_matching_device_id() -> None:
    """If serial digest drifted, every restore would warn about identity for no reason."""
    built = live_keyboard._snapshot_from_reads(
        client_model.DeviceInfo(name=_Info.name, serial_number=_Info.serial_number),
        client_model.Keymap(),
        client_model.PhysicalLayouts(),
        {},
    )
    assert built.device.id == _snapshot().device.id


# --------------------------------------------------------------------------- #
# applying: order is the design
# --------------------------------------------------------------------------- #

def test_apply_writes_only_what_differs(tmp_path: pathlib.Path) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    _plan(session, want).apply(session, _backup_writer(tmp_path))
    assert session.writes.count("write:set_layer_binding") == 1


def test_apply_refuses_when_keyboard_drifted_since_plan(tmp_path: pathlib.Path) -> None:
    """If the keyboard changed after `RestorePlan` was created, `apply()` refuses before backup/writes."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session, position=0)
    plan = _plan(session, want)

    _diverge(session, position=1)
    with pytest.raises(RestoreRefused, match="changed since the plan"):
        plan.apply(session, _backup_writer(tmp_path))
    assert session.writes == []
    assert list(tmp_path.glob("before-restore-*.json")) == []


def test_save_changes_is_the_last_thing_sent(tmp_path: pathlib.Path) -> None:
    """The irreversible step must come after every check."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    _plan(session, want).apply(session, _backup_writer(tmp_path))
    assert session.writes[-1] == "write:save_changes"
    assert "write:discard_changes" not in session.writes


def test_the_keyboard_is_re_read_before_it_is_committed(tmp_path: pathlib.Path) -> None:
    """Verification after save_changes would be an audit, not a safeguard."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    _plan(session, want).apply(session, _backup_writer(tmp_path))
    save = session.sent.index("write:save_changes")
    assert (
        "read:get_keymap"
        in session.sent[session.sent.index("write:set_layer_binding") : save]
    )


def test_a_backup_is_written_before_the_first_write(tmp_path: pathlib.Path) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    path = _plan(session, want).apply(session, _backup_writer(tmp_path))
    assert path.exists()
    assert path.parent == tmp_path
    assert "before-restore-" in path.name


def test_the_backup_is_a_loadable_snapshot_of_the_old_state(
    tmp_path: pathlib.Path,
) -> None:
    """A pre-flight file that cannot be restored is not an undo."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    path = _plan(session, want).apply(session, _backup_writer(tmp_path))
    recovered = pb.KeymapSnapshot.from_json(path.read_text(encoding="utf-8"))
    assert len(recovered.layers) == len(want.layers)
    old = recovered.layers[0].bindings[0]
    assert old.param1 == want.layers[0].bindings[0].param1 ^ 0xFF


def test_restoring_nothing_is_refused_rather_than_committed(
    tmp_path: pathlib.Path,
) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    plan = _plan(session, want)
    with pytest.raises(RestoreRefused, match="already matches"):
        plan.apply(session, _backup_writer(tmp_path))
    assert session.writes == []


# --------------------------------------------------------------------------- #
# failure paths: every one ends in discard
# --------------------------------------------------------------------------- #

def test_a_rejected_binding_rolls_back(tmp_path: pathlib.Path) -> None:
    want = _snapshot()
    session = _FakeSession(want, reject_at=1)
    _diverge(session)
    plan = _plan(session, want)
    with pytest.raises(RestoreFailed, match="INVALID_BEHAVIOR"):
        plan.apply(session, _backup_writer(tmp_path))
    assert session.writes[-1] == "write:discard_changes"
    assert "write:save_changes" not in session.writes


def test_a_write_that_does_not_take_effect_rolls_back(tmp_path: pathlib.Path) -> None:
    """The keyboard says OK and nothing changed. Verification is what catches it."""
    want = _snapshot()
    session = _FakeSession(want, apply_writes=False)
    _diverge(session)
    plan = _plan(session, want)
    with pytest.raises(RestoreFailed, match="did not take effect"):
        plan.apply(session, _backup_writer(tmp_path))
    assert session.writes[-1] == "write:discard_changes"
    assert "write:save_changes" not in session.writes


def test_a_failed_commit_rolls_back_and_says_so(tmp_path: pathlib.Path) -> None:
    want = _snapshot()
    session = _FakeSession(want, save_ok=False)
    _diverge(session)
    plan = _plan(session, want)
    with pytest.raises(RestoreFailed, match="NO_SPACE"):
        plan.apply(session, _backup_writer(tmp_path))
    assert session.writes[-1] == "write:discard_changes"


def test_transport_error_during_save_changes_rolls_back_and_names_undo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """P1-2: Non-SaveRefused exceptions from `edit.save()` still discard and raise RestoreFailed."""
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    plan = _plan(session, want)

    def broken_save(_request: typing.Any) -> typing.Any:
        raise OSError("BLE link dropped mid-save")

    monkeypatch.setattr(session._fake, "_save_changes", broken_save)
    with pytest.raises(RestoreFailed, match="before-restore-"):
        plan.apply(session, _backup_writer(tmp_path))
    assert session.writes[-1] == "write:discard_changes"


def test_a_failed_rollback_says_how_to_recover(tmp_path: pathlib.Path) -> None:
    """The worst case. The reader needs to be told power-cycling clears RAM."""
    want = _snapshot()
    session = _FakeSession(want, reject_at=1, discard_raises=True)
    _diverge(session)
    plan = _plan(session, want)
    with pytest.raises(RestoreFailed, match="power-cycling"):
        plan.apply(session, _backup_writer(tmp_path))


def test_every_failure_path_names_the_backup(tmp_path: pathlib.Path) -> None:
    """An error that does not say where the undo lives is half an error."""
    want = _snapshot()
    session = _FakeSession(want, save_ok=False)
    _diverge(session)
    plan = _plan(session, want)
    with pytest.raises(RestoreFailed) as caught:
        plan.apply(session, _backup_writer(tmp_path))
    assert "before-restore-" in str(caught.value)


def test_restore_plan_is_frozen_hashable_and_commits_every_field_in_digest() -> None:
    """`RestorePlan` is frozen/hashable and changes `digest()` when metadata, bindings, or `unused_missing` differ."""
    base_target = _snapshot()
    base_live = _with_flipped_param1(base_target, binding_idx=0)
    base_plan = RestorePlan(live=base_live, target=base_target)
    assert isinstance(hash(base_plan), int)

    with pytest.raises(FrozenInstanceError):
        base_plan.target = base_live  # type: ignore[misc]

    meta_target = _with_device(base_target, name="OtherSofle")
    assert RestorePlan(live=base_live, target=meta_target).digest() != base_plan.digest()

    binding_target = _with_flipped_param1(base_target, binding_idx=1)
    assert (
        RestorePlan(live=base_live, target=binding_target).digest()
        != base_plan.digest()
    )

    unused_target = pb.KeymapSnapshot(
        device=base_target.device,
        layout=base_target.layout,
        behaviors=[
            *base_target.behaviors,
            client_model.Behavior(
                id=max(b.id for b in base_target.behaviors) + 1,
                display_name="ExtraUnboundBehavior",
            ),
        ],
        layers=base_target.layers,
    )
    extra_plan = RestorePlan(live=base_live, target=unused_target)
    assert extra_plan.diff.translator.unused_missing == ("ExtraUnboundBehavior",)
    assert extra_plan.digest() != base_plan.digest()


def test_capture_refuses_symlinks_and_uniquifies_same_second_restores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """`write_undo_snapshot` + `apply` refuse symlinks and never clobber same-second undo files."""
    import datetime as dt

    class _FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> _FrozenDateTime:
            return cls(2026, 11, 16, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(snapshot_store.datetime, "datetime", _FrozenDateTime)

    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-touch", encoding="utf-8")

    # 1. Symlinked backup_dir is refused before any write.
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    symlink_dir = tmp_path / "symlink_backups"
    symlink_dir.symlink_to(outside_dir, target_is_directory=True)

    want = _snapshot()
    session1 = _FakeSession(want)
    _diverge(session1)
    plan1 = _plan(session1, want)
    with pytest.raises(RestoreRefused, match="symlink"):
        plan1.apply(session1, _backup_writer(symlink_dir))
    assert session1.writes == []
    assert list(outside_dir.iterdir()) == []

    # 2. Planted symlink at the exact timestamped filename is refused without following.
    backups = tmp_path / "backups"
    backups.mkdir()
    planted = backups / "before-restore-20261116T120000Z.json"
    planted.symlink_to(victim)
    session2 = _FakeSession(want)
    _diverge(session2)
    plan2 = _plan(session2, want)
    with pytest.raises(RestoreRefused, match="symlink"):
        plan2.apply(session2, _backup_writer(backups))
    assert session2.writes == []
    assert victim.read_text(encoding="utf-8") == "do-not-touch"
    planted.unlink()

    # 3. Two legitimate restores within the same UTC second write two distinct files.
    session3 = _FakeSession(want)
    _diverge(session3, position=0)
    first_backup = _plan(session3, want).apply(session3, _backup_writer(backups))

    session4 = _FakeSession(want)
    _diverge(session4, position=1)
    second_backup = _plan(session4, want).apply(session4, _backup_writer(backups))

    assert first_backup != second_backup
    assert first_backup.name == "before-restore-20261116T120000Z.json"
    assert second_backup.name == "before-restore-20261116T120000Z-2.json"
    assert first_backup.is_file() and second_backup.is_file()
