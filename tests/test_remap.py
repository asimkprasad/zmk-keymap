"""Behavior ids are firmware-local; names are what survive a restore.

The failure this guards against is silent: a stale behavior id is individually valid,
the keyboard accepts it, and the key does the wrong thing with no error anywhere. So
these tests are mostly about the shifted-table case, which is the one that cannot be
noticed by looking at a keyboard afterwards.
"""
from __future__ import annotations

import pathlib

import pytest

from zmk_keymap import from_json, model as pb, remap

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPORA = sorted(DATA.glob("*.json"))


@pytest.fixture(scope="module", params=CORPORA, ids=lambda p: p.stem)
def snapshot(request: pytest.FixtureRequest) -> pb.KeymapSnapshot:
    return from_json(request.param.read_text(encoding="utf-8"))


class _Behavior:
    """Stands in for a live behavior; `plan` needs only .id and .display_name."""

    def __init__(self, id: int, display_name: str) -> None:
        self.id, self.display_name = id, display_name


def _live_from(snapshot, shift: int = 0, drop: str | None = None, rename=None):
    """The snapshot's own table as if read from a keyboard, optionally perturbed."""
    out = []
    for behavior in snapshot.behaviors:
        if behavior.display_name == drop:
            continue
        name = behavior.display_name
        if rename and name in rename:
            name = rename[name]
        out.append(_Behavior(behavior.id + shift, name))
    return out


# --------------------------------------------------------------------------- #
# the ordinary case: same firmware, nothing to translate
# --------------------------------------------------------------------------- #

def test_the_same_table_needs_no_translation(snapshot) -> None:
    plan = remap.plan(snapshot, _live_from(snapshot))
    assert plan.is_identity
    assert not plan.unused_and_missing


def test_every_bound_behavior_is_planned_for(snapshot) -> None:
    plan = remap.plan(snapshot, _live_from(snapshot))
    bound = {b.behavior_id for layer in snapshot.layers for b in layer.bindings}
    assert set(plan.ids) == bound


# --------------------------------------------------------------------------- #
# the silent-corruption case
# --------------------------------------------------------------------------- #

def test_a_shifted_table_is_translated_not_replayed(snapshot) -> None:
    """The whole point. Every id moved by 100; names unchanged."""
    plan = remap.plan(snapshot, _live_from(snapshot, shift=100))
    assert not plan.is_identity
    for source, target in plan.ids.items():
        assert target == source + 100


def test_translation_follows_the_name_not_the_position(snapshot) -> None:
    """Reverse the live table's ids. A name-keyed map is unaffected by order."""
    live = _live_from(snapshot)
    names = [b.display_name for b in live]
    reversed_ids = [b.id for b in live][::-1]
    shuffled = [_Behavior(i, n) for i, n in zip(reversed_ids, names)]
    plan = remap.plan(snapshot, shuffled)
    live_by_name = {b.display_name: b.id for b in shuffled}
    stored = {b.id: b.display_name for b in snapshot.behaviors}
    for source, target in plan.ids.items():
        assert target == live_by_name[stored[source]]


def test_a_missing_behavior_aborts_and_names_the_keys(snapshot) -> None:
    """'Key Press is missing' is a question; naming a key makes it an answer."""
    used = {b.behavior_name for layer in snapshot.layers for b in layer.bindings}
    victim = sorted(used)[0]
    with pytest.raises(remap.BehaviorMissing) as caught:
        remap.plan(snapshot, _live_from(snapshot, drop=victim))
    message = str(caught.value)
    assert victim in message
    assert "position" in message
    assert "nothing" in message and "written" in message


def test_a_behavior_the_snapshot_never_binds_may_be_absent(snapshot) -> None:
    """A firmware that trimmed an unused behavior can still take the restore."""
    bound = {b.behavior_id for layer in snapshot.layers for b in layer.bindings}
    spare = [b for b in snapshot.behaviors if b.id not in bound]
    if not spare:
        pytest.skip("every behavior in this corpus is bound")
    plan = remap.plan(snapshot, _live_from(snapshot, drop=spare[0].display_name))
    assert spare[0].display_name in plan.unused_and_missing
    assert plan.is_identity


# --------------------------------------------------------------------------- #
# ambiguity
# --------------------------------------------------------------------------- #

def test_duplicate_names_on_the_keyboard_are_refused(snapshot) -> None:
    live = _live_from(snapshot)
    live.append(_Behavior(999, live[0].display_name))
    with pytest.raises(remap.BehaviorAmbiguous, match="keyboard"):
        remap.plan(snapshot, live)


def test_duplicate_names_in_the_snapshot_are_refused(snapshot) -> None:
    copy = pb.KeymapSnapshot()
    copy.CopyFrom(snapshot)
    extra = copy.behaviors.add()
    extra.id, extra.display_name = 999, copy.behaviors[0].display_name
    with pytest.raises(remap.BehaviorAmbiguous, match="snapshot"):
        remap.plan(copy, _live_from(snapshot))


def test_a_binding_with_no_entry_in_its_own_table_is_refused(snapshot) -> None:
    """A truncated or hand-edited file, caught before anything is written."""
    copy = pb.KeymapSnapshot()
    copy.CopyFrom(snapshot)
    copy.layers[0].bindings[0].behavior_id = 31337
    with pytest.raises(remap.BehaviorMissing, match="its own behavior table"):
        remap.plan(copy, _live_from(snapshot))


# --------------------------------------------------------------------------- #
# the accessor
# --------------------------------------------------------------------------- #

def test_translate_returns_the_live_id(snapshot) -> None:
    plan = remap.plan(snapshot, _live_from(snapshot, shift=7))
    some = sorted(plan.ids)[0]
    assert plan.translate(some) == some + 7


def test_translate_refuses_an_id_it_never_planned_for(snapshot) -> None:
    """Better a KeyError here than a valid-looking write of an untranslated id."""
    plan = remap.plan(snapshot, _live_from(snapshot))
    with pytest.raises(KeyError):
        plan.translate(31337)
