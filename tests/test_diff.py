"""A restore writes the difference, so the difference has to be exactly right.

Two properties carry the weight: restoring a snapshot onto the keyboard it came from
writes nothing, and a snapshot that does not fit the keyboard is refused rather than
partially applied.
"""
from __future__ import annotations

import pathlib

import pytest

from zmk_keymap import diff, from_json, model as pb, remap
from zmk_studio_client import model as client_model

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPORA = sorted(DATA.glob("*.json"))


@pytest.fixture(scope="module", params=CORPORA, ids=lambda p: p.stem)
def snapshot(request: pytest.FixtureRequest) -> pb.KeymapSnapshot:
    return from_json(request.param.read_text(encoding="utf-8"))


def _live_behaviors(snapshot: pb.KeymapSnapshot) -> list[client_model.Behavior]:
    return [
        client_model.Behavior(id=b.id, display_name=b.display_name)
        for b in snapshot.behaviors
    ]


def _identity(snapshot: pb.KeymapSnapshot) -> remap.Remapping:
    return remap.plan(snapshot, _live_behaviors(snapshot))


def _live_keymap(snapshot: pb.KeymapSnapshot) -> client_model.Keymap:
    """Rebuild the live `zmk_studio_client.model.Keymap` the snapshot was captured from.

    Built from the client's published `model.Keymap` rather than ZMK's wire message,
    because `model.Keymap` is what `Keyboard.keymap()` actually hands `diff.plan()`
    in production.

    Note the shape change: the snapshot stores position on each Binding, while the
    live message uses the INDEX in a repeated field. Building the fixture this way
    rather than by copying keeps the test honest about that join.
    """
    keymap = client_model.Keymap()
    for layer in snapshot.layers:
        out = keymap.layers.add()
        out.id, out.name = layer.id, layer.name
        width = max(b.position for b in layer.bindings) + 1
        slots: list[pb.Binding | None] = [None] * width
        for binding in layer.bindings:
            slots[binding.position] = binding
        for binding in slots:
            entry = out.bindings.add()
            if binding is not None:
                entry.behavior_id = binding.behavior_id
                entry.param1, entry.param2 = binding.param1, binding.param2
    return keymap


# --------------------------------------------------------------------------- #
# the property that makes a restore safe to run
# --------------------------------------------------------------------------- #

def test_restoring_onto_itself_writes_nothing(snapshot) -> None:
    plan = diff.plan(snapshot, _live_keymap(snapshot), _identity(snapshot))
    assert plan.is_empty
    assert len(plan) == 0


def test_every_key_was_actually_compared(snapshot) -> None:
    """Without this, a silent mismatch would look identical to 'already matches'."""
    bound = sum(len(layer.bindings) for layer in snapshot.layers)
    plan = diff.plan(snapshot, _live_keymap(snapshot), _identity(snapshot))
    assert plan.compared == bound


def test_a_single_changed_key_yields_a_single_write(snapshot) -> None:
    live = _live_keymap(snapshot)
    live.layers[0].bindings[0].param1 ^= 0xFF
    plan = diff.plan(snapshot, live, _identity(snapshot))
    assert len(plan) == 1
    assert plan.changes[0].position == 0
    assert plan.changes[0].layer_id == snapshot.layers[0].id


def test_a_changed_behavior_is_noticed_not_just_a_parameter(snapshot) -> None:
    live = _live_keymap(snapshot)
    live.layers[0].bindings[0].behavior_id += 1
    assert len(diff.plan(snapshot, live, _identity(snapshot))) == 1


# --------------------------------------------------------------------------- #
# the shifted-table case, end to end with remap
# --------------------------------------------------------------------------- #

def test_a_shifted_behavior_table_still_writes_nothing(snapshot) -> None:
    """The keyboard is the same, only its ids moved. A restore must see no work.

    If this produced writes, every firmware update would make a stored backup look
    like a keyboard that needs rewriting from scratch.
    """
    shifted = [
        client_model.Behavior(id=b.id + 100, display_name=b.display_name)
        for b in snapshot.behaviors
    ]
    mapping = remap.plan(snapshot, shifted)
    live = _live_keymap(snapshot)
    for layer in live.layers:
        for entry in layer.bindings:
            if entry.behavior_id:
                entry.behavior_id += 100
    plan = diff.plan(snapshot, live, mapping)
    assert plan.is_empty


def test_a_change_carries_the_live_id_not_the_snapshot_id(snapshot) -> None:
    """What goes on the wire must be translated; the stored id must not leak."""
    shifted = [
        client_model.Behavior(id=b.id + 100, display_name=b.display_name)
        for b in snapshot.behaviors
    ]
    mapping = remap.plan(snapshot, shifted)
    live = _live_keymap(snapshot)
    for layer in live.layers:
        for entry in layer.bindings:
            if entry.behavior_id:
                entry.behavior_id += 100
    live.layers[0].bindings[0].param1 ^= 0xFF
    change = diff.plan(snapshot, live, mapping).changes[0]
    stored = snapshot.layers[0].bindings[0].behavior_id
    assert change.behavior_id == stored + 100


# --------------------------------------------------------------------------- #
# refusing a snapshot that does not fit
# --------------------------------------------------------------------------- #

def test_a_missing_layer_is_refused(snapshot) -> None:
    live = _live_keymap(snapshot)
    del live.layers[-1]
    with pytest.raises(diff.LayoutMismatch, match="no layer matching"):
        diff.plan(snapshot, live, _identity(snapshot))


def test_a_shorter_layer_is_refused(snapshot) -> None:
    """A snapshot from a different physical layout. Half-applying it is the worst case."""
    live = _live_keymap(snapshot)
    del live.layers[0].bindings[-1]
    with pytest.raises(diff.LayoutMismatch, match="different physical layout"):
        diff.plan(snapshot, live, _identity(snapshot))


def test_a_duplicate_live_layer_id_is_refused(snapshot) -> None:
    live = _live_keymap(snapshot)
    extra = live.layers.add()
    extra.CopyFrom(live.layers[0])
    with pytest.raises(diff.LayoutMismatch, match="twice"):
        diff.plan(snapshot, live, _identity(snapshot))


def test_a_refusal_says_nothing_was_written(snapshot) -> None:
    """The reader's first question on any abort mid-restore."""
    live = _live_keymap(snapshot)
    del live.layers[-1]
    with pytest.raises(diff.LayoutMismatch, match="Nothing was written"):
        diff.plan(snapshot, live, _identity(snapshot))


# --------------------------------------------------------------------------- #
# legibility of the dry run
# --------------------------------------------------------------------------- #

def test_a_change_reads_as_before_and_after(snapshot) -> None:
    live = _live_keymap(snapshot)
    live.layers[0].bindings[0].param1 ^= 0xFF
    table = {b.id: b for b in snapshot.behaviors}
    change = diff.plan(snapshot, live, _identity(snapshot), table).changes[0]
    assert change.before and change.after
    assert change.before != change.after
    assert "->" in str(change)
    assert str(change).startswith(snapshot.layers[0].name or "")


def test_the_target_is_the_snapshots_own_rendering(snapshot) -> None:
    """The snapshot already says what each key should read as; reuse it."""
    live = _live_keymap(snapshot)
    live.layers[0].bindings[0].param1 ^= 0xFF
    change = diff.plan(snapshot, live, _identity(snapshot)).changes[0]
    assert change.after == snapshot.layers[0].bindings[0].rendered


def test_without_a_behavior_table_the_current_state_is_still_honest(snapshot) -> None:
    """A raw fallback beats a guess; it must not pretend to have decoded anything."""
    live = _live_keymap(snapshot)
    live.layers[0].bindings[0].param1 ^= 0xFF
    change = diff.plan(snapshot, live, _identity(snapshot)).changes[0]
    assert change.before.startswith("behavior ")
    assert "0x" in change.before
