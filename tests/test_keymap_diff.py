"""`KeymapSnapshot.diff(target)` -> `KeymapDiff` and `BehaviorTranslator`.

Two properties carry the weight:
1. Diffing a snapshot against the same keymap yields zero changes — even when firmware
   behavior IDs shifted or shuffled, because `KeymapDiff.BehaviorTranslator` matches
   behaviors by display name.
2. A snapshot that does not fit the baseline (missing/ambiguous behaviors, incompatible
   layers, sparse baseline gaps, or out-of-bounds positions) is refused rather than
   partially applied.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from zmk_keymap import keymap_diff, keymap_snapshot as pb
from zmk_keymap.keymap_diff import KeymapDiff
from zmk_studio_client import model as client_model

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPORA = sorted(DATA.glob("*.json"))


@pytest.fixture(scope="module", params=CORPORA, ids=lambda p: p.stem)
def snapshot(request: pytest.FixtureRequest) -> pb.KeymapSnapshot:
    return pb.KeymapSnapshot.from_json(request.param.read_text(encoding="utf-8"))


class _Behavior:
    """Stands in for a target behavior; matching needs only `.id` and `.display_name`."""

    def __init__(self, id: int, display_name: str) -> None:
        self.id, self.display_name = id, display_name


def _live_from(
    snapshot: pb.KeymapSnapshot,
    shift: int = 0,
    drop: str | None = None,
    rename: dict[str, str] | None = None,
) -> list[_Behavior]:
    """The snapshot's own behavior table, optionally perturbed."""
    out: list[_Behavior] = []
    for behavior in snapshot.behaviors:
        if behavior.display_name == drop:
            continue
        name = behavior.display_name
        if rename and name in rename:
            name = rename[name]
        out.append(_Behavior(behavior.id + shift, name))
    return out


def _with_binding(
    snap: pb.KeymapSnapshot,
    layer_idx: int = 0,
    binding_idx: int = 0,
    *,
    behavior_id: int | None = None,
    param1: int | None = None,
) -> pb.KeymapSnapshot:
    """Build a new `KeymapSnapshot` with one binding replaced and re-decoded."""
    cur = snap.layers[layer_idx].bindings[binding_idx]
    bid = cur.behavior_id if behavior_id is None else behavior_id
    p1 = cur.param1 if param1 is None else param1
    beh = next((b for b in snap.behaviors if b.id == bid), client_model.Behavior())
    redecoded = pb.decode_binding(
        beh, p1, cur.param2, position=cur.position, behavior_id=bid
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


def _shifted_snapshot(snapshot: pb.KeymapSnapshot, shift: int) -> pb.KeymapSnapshot:
    """Copy `snapshot` with every `behavior_id` shifted by `shift`."""
    raw = json.loads(snapshot.to_json())
    for beh in raw["behaviors"]:
        beh["id"] = int(beh["id"]) + shift
    for layer in raw["layers"]:
        for binding in layer["bindings"]:
            if int(binding["behavior_id"]):
                binding["behavior_id"] = int(binding["behavior_id"]) + shift
    return pb.KeymapSnapshot.from_json(json.dumps(raw))


# --------------------------------------------------------------------------- #
# KeymapDiff.BehaviorTranslator: behavior matching across firmware builds
# --------------------------------------------------------------------------- #

def test_the_same_table_needs_no_translation(snapshot) -> None:
    translator = KeymapDiff.BehaviorTranslator.between(snapshot, _live_from(snapshot))
    assert translator.is_identity
    assert not translator.unused_missing


def test_every_bound_behavior_is_planned_for(snapshot) -> None:
    translator = KeymapDiff.BehaviorTranslator.between(snapshot, _live_from(snapshot))
    bound = {b.behavior_id for layer in snapshot.layers for b in layer.bindings}
    assert set(translator.ids) == bound


def test_a_shifted_table_is_translated_not_replayed(snapshot) -> None:
    """Every id moved by 100; names unchanged."""
    translator = KeymapDiff.BehaviorTranslator.between(
        snapshot, _live_from(snapshot, shift=100)
    )
    assert not translator.is_identity
    for source, target in translator.ids.items():
        assert target == source + 100


def test_translation_follows_the_name_not_the_position(snapshot) -> None:
    """Reverse the target table's ids. A name-keyed map is unaffected by order."""
    live = _live_from(snapshot)
    names = [b.display_name for b in live]
    reversed_ids = [b.id for b in live][::-1]
    shuffled = [_Behavior(i, n) for i, n in zip(reversed_ids, names)]
    translator = KeymapDiff.BehaviorTranslator.between(snapshot, shuffled)
    live_by_name = {b.display_name: b.id for b in shuffled}
    stored = {b.id: b.display_name for b in snapshot.behaviors}
    for source, target in translator.ids.items():
        assert target == live_by_name[stored[source]]


def test_a_missing_behavior_aborts_and_names_the_keys(snapshot) -> None:
    """'Key Press is missing' is a question; naming a key makes it an answer."""
    used = {b.behavior_name for layer in snapshot.layers for b in layer.bindings}
    victim = sorted(used)[0]
    with pytest.raises(keymap_diff.BehaviorMissing) as caught:
        KeymapDiff.BehaviorTranslator.between(
            snapshot, _live_from(snapshot, drop=victim)
        )
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
    translator = KeymapDiff.BehaviorTranslator.between(
        snapshot, _live_from(snapshot, drop=spare[0].display_name)
    )
    assert spare[0].display_name in translator.unused_missing
    assert translator.is_identity


def test_duplicate_names_on_the_keyboard_are_refused_when_bound_and_ignored_when_unbound(
    snapshot,
) -> None:
    bound = {b.behavior_id for layer in snapshot.layers for b in layer.bindings}
    bound_beh = next(b for b in snapshot.behaviors if b.id in bound)
    live = _live_from(snapshot)
    live.append(_Behavior(999, bound_beh.display_name))
    with pytest.raises(keymap_diff.BehaviorAmbiguous, match="keyboard"):
        KeymapDiff.BehaviorTranslator.between(snapshot, live)

    # Unbound duplicate display names on either side do not block translating bound keys.
    live_unbound_dup = [
        *_live_from(snapshot),
        _Behavior(998, "UnboundCustomMacro"),
        _Behavior(999, "UnboundCustomMacro"),
    ]
    assert KeymapDiff.BehaviorTranslator.between(snapshot, live_unbound_dup).is_identity

    # Duplicate numeric behavior IDs are always refused.
    live_dup_id = [*_live_from(snapshot), _Behavior(bound_beh.id, "UniqueExtraName")]
    with pytest.raises(keymap_diff.BehaviorAmbiguous, match="more than once"):
        KeymapDiff.BehaviorTranslator.between(snapshot, live_dup_id)


def test_duplicate_names_in_the_snapshot_are_refused(snapshot) -> None:
    bound = {b.behavior_id for layer in snapshot.layers for b in layer.bindings}
    bound_beh = next(b for b in snapshot.behaviors if b.id in bound)
    dup = pb.KeymapSnapshot(
        device=snapshot.device,
        layout=snapshot.layout,
        behaviors=[
            *snapshot.behaviors,
            client_model.Behavior(id=999, display_name=bound_beh.display_name),
        ],
        layers=snapshot.layers,
    )
    with pytest.raises(keymap_diff.BehaviorAmbiguous, match="snapshot"):
        KeymapDiff.BehaviorTranslator.between(dup, _live_from(snapshot))


def test_a_binding_with_no_entry_in_its_own_table_is_refused(snapshot) -> None:
    """A truncated or hand-edited file, caught before anything is written."""
    broken = _with_binding(snapshot, behavior_id=31337)
    with pytest.raises(keymap_diff.BehaviorMissing, match="its own behavior table"):
        KeymapDiff.BehaviorTranslator.between(broken, _live_from(snapshot))


def test_translate_returns_the_live_id(snapshot) -> None:
    translator = KeymapDiff.BehaviorTranslator.between(
        snapshot, _live_from(snapshot, shift=7)
    )
    some = sorted(translator.ids)[0]
    assert translator.translate(some) == some + 7


def test_translate_refuses_an_id_it_never_planned_for(snapshot) -> None:
    """Better a KeyError here than a valid-looking write of an untranslated id."""
    translator = KeymapDiff.BehaviorTranslator.between(snapshot, _live_from(snapshot))
    with pytest.raises(KeyError):
        translator.translate(31337)


def test_keymap_diff_and_translator_are_immutable_and_hashable(snapshot) -> None:
    first = snapshot.layers[0].bindings[0]
    baseline = _with_binding(snapshot, param1=first.param1 ^ 0xFF)
    diff = baseline.diff(snapshot)
    assert diff.translator.matched_count == len(diff.translator.ids)
    assert isinstance(hash(diff.translator), int)
    assert isinstance(hash(diff), int)
    with pytest.raises(TypeError):
        diff.translator.ids[0] = 999  # type: ignore[index]


# --------------------------------------------------------------------------- #
# KeymapSnapshot.diff(target) -> KeymapDiff & KeymapDiff.KeyDiff
# --------------------------------------------------------------------------- #

def test_diff_against_itself_is_empty_and_compares_every_key(snapshot) -> None:
    other = pb.KeymapSnapshot.from_json(snapshot.to_json())
    result = snapshot.diff(other)
    assert result.is_empty
    assert len(result) == 0
    assert result.translator.is_identity
    assert result.compared == sum(len(layer.bindings) for layer in snapshot.layers)


def test_a_single_changed_key_yields_a_single_key_diff(snapshot) -> None:
    first = snapshot.layers[0].bindings[0]
    baseline = _with_binding(snapshot, param1=first.param1 ^ 0xFF)
    diff = baseline.diff(snapshot)
    assert len(diff) == 1
    kd = diff.key_diffs[0]
    assert isinstance(kd, KeymapDiff.KeyDiff)
    assert kd.position == first.position
    assert kd.layer_id == snapshot.layers[0].id
    assert diff.for_layer(snapshot.layers[0].id)[first.position] == kd
    assert kd.after == snapshot.layers[0].bindings[0].decoded
    assert kd.before != kd.after
    assert "->" in str(kd)
    assert str(kd).startswith(snapshot.layers[0].name or "")


def test_a_changed_behavior_is_noticed_not_just_a_parameter(snapshot) -> None:
    first = snapshot.layers[0].bindings[0]
    other_bid = next(b.id for b in snapshot.behaviors if b.id != first.behavior_id)
    baseline = _with_binding(snapshot, behavior_id=other_bid)
    assert len(baseline.diff(snapshot)) == 1


def test_shifted_behavior_ids_yield_empty_diff_and_carry_baseline_id_on_change(
    snapshot,
) -> None:
    shifted_live = _shifted_snapshot(snapshot, 100)
    noop = shifted_live.diff(snapshot)
    assert noop.is_empty
    assert not noop.translator.is_identity

    first = shifted_live.layers[0].bindings[0]
    changed_live = _with_binding(shifted_live, param1=first.param1 ^ 0xFF)
    kd = changed_live.diff(snapshot).key_diffs[0]
    stored = snapshot.layers[0].bindings[0].behavior_id
    assert kd.behavior_id == stored + 100


def test_a_missing_layer_is_refused_and_says_nothing_was_written(snapshot) -> None:
    baseline = pb.KeymapSnapshot(
        device=snapshot.device,
        layout=snapshot.layout,
        behaviors=snapshot.behaviors,
        layers=list(snapshot.layers)[:-1],
    )
    with pytest.raises(keymap_diff.LayoutMismatch, match="no layer matching"):
        baseline.diff(snapshot)
    with pytest.raises(keymap_diff.LayoutMismatch, match="Nothing was written"):
        baseline.diff(snapshot)


def test_a_duplicate_layer_id_is_refused(snapshot) -> None:
    raw = json.loads(snapshot.to_json())
    raw["layers"].append(dict(raw["layers"][0]))
    dup = pb.KeymapSnapshot.from_json(json.dumps(raw))
    with pytest.raises(keymap_diff.LayoutMismatch, match="twice"):
        dup.diff(snapshot)


def test_diff_rejects_incompatible_key_counts(snapshot) -> None:
    smaller = pb.KeymapSnapshot(
        device=snapshot.device,
        layout=pb.PhysicalLayout(
            name=snapshot.layout.name,
            active_index=snapshot.layout.active_index,
            available=snapshot.layout.available,
            keys=list(snapshot.layout.keys)[:-1],
        ),
        behaviors=snapshot.behaviors,
        layers=snapshot.layers,
    )
    with pytest.raises(keymap_diff.LayoutMismatch, match="different physical layouts"):
        smaller.diff(snapshot)


def test_diff_rejects_huge_uint32_position_without_allocating(snapshot) -> None:
    raw_bad = json.loads(snapshot.to_json())
    raw_bad["layers"][0]["bindings"][0]["position"] = 4_000_000_000
    bad = pb.KeymapSnapshot.from_json(json.dumps(raw_bad))

    with pytest.raises(keymap_diff.LayoutMismatch, match="position 4000000000"):
        bad.diff(snapshot)
    with pytest.raises(keymap_diff.LayoutMismatch, match="position 4000000000"):
        snapshot.diff(bad)

    headless_after = pb.KeymapSnapshot(
        device=snapshot.device,
        behaviors=snapshot.behaviors,
        layers=snapshot.layers,
    )
    raw_headless_bad = json.loads(headless_after.to_json())
    raw_headless_bad["layers"][0]["bindings"][0]["position"] = 4_000_000_000
    headless_bad = pb.KeymapSnapshot.from_json(json.dumps(raw_headless_bad))
    with pytest.raises(keymap_diff.LayoutMismatch, match="exceeds the maximum"):
        headless_bad.diff(headless_after)


def test_diff_refuses_sparse_baseline_gap_rather_than_fabricating_zero(snapshot) -> None:
    layers = list(snapshot.layers)
    layers[0] = pb.Layer(
        id=layers[0].id,
        display_order=layers[0].display_order,
        name=layers[0].name,
        bindings=[b for b in layers[0].bindings if b.position != 10],
    )
    sparse_before = pb.KeymapSnapshot(
        device=snapshot.device,
        layout=snapshot.layout,
        behaviors=snapshot.behaviors,
        layers=layers,
    )
    with pytest.raises(keymap_diff.LayoutMismatch, match="absent from the baseline"):
        sparse_before.diff(snapshot)
