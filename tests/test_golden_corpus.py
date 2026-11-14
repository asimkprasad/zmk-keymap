"""Frozen snapshots from real keyboards, re-decoded.

A snapshot carries each binding's raw parameters next to the behavior table that
describes them, which makes it a free regression corpus: every future decoder change
can be checked against hundreds of real bindings without a keyboard attached.

The corpus lives in `data/` rather than at `snapshots/keymap.json`, which
`keymap-backup` overwrites on every capture. See `data/README.md`.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from zmk_keymap import decode, from_json, model, to_json

DATA = pathlib.Path(__file__).resolve().parent / "data"

#: Every corpus file, so a new board is added by dropping in a file.
CORPORA = sorted(DATA.glob("*.json"))


def test_the_corpus_is_not_empty() -> None:
    """Without this, every test below would pass by being parametrised over nothing."""
    assert CORPORA, f"no corpus files in {DATA}"


@pytest.fixture(scope="module", params=CORPORA, ids=lambda p: p.stem)
def snapshot(request: pytest.FixtureRequest) -> model.KeymapSnapshot:
    return from_json(request.param.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", CORPORA, ids=lambda p: p.stem)
def test_round_trips_through_the_committed_json(path: pathlib.Path) -> None:
    """A corpus file is byte-for-byte what `to_json` would write for it."""
    raw = path.read_text(encoding="utf-8")
    assert json.loads(to_json(from_json(raw))) == json.loads(raw)


def test_every_binding_resolves_to_a_behavior_in_the_table(snapshot) -> None:
    """The property that makes the table worth shipping inside the snapshot."""
    known = {b.id for b in snapshot.behaviors}
    unresolved = {
        b.behavior_id for layer in snapshot.layers for b in layer.bindings
        if b.behavior_id not in known
    }
    assert not unresolved


def test_every_binding_still_decodes(snapshot) -> None:
    """No raw parameter in a real keymap sends the decoder to an unknown behavior."""
    table = {b.id: b for b in snapshot.behaviors}
    for layer in snapshot.layers:
        for bound in layer.bindings:
            rendered = decode.binding(table[bound.behavior_id], bound.param1, bound.param2)
            assert rendered.text
            assert all(p.decoded for p in rendered.params)


def test_no_binding_renders_as_a_bare_hex_blob(snapshot) -> None:
    """A rendering consisting only of raw integers means decoding gave up."""
    table = {b.id: b for b in snapshot.behaviors}
    gave_up = []
    for layer in snapshot.layers:
        for bound in layer.bindings:
            rendered = decode.binding(table[bound.behavior_id], bound.param1, bound.param2)
            if "0x" in rendered.text:
                gave_up.append(rendered.text)
    assert not gave_up, f"undecoded parameters: {sorted(set(gave_up))}"


def test_every_binding_has_a_key_to_sit_on(snapshot) -> None:
    """A renderer joins bindings to geometry by position; this is that join holding."""
    positions = {k.position for k in snapshot.layout.keys}
    for layer in snapshot.layers:
        missing = {b.position for b in layer.bindings} - positions
        assert not missing, f"layer {layer.id} binds positions with no key: {missing}"
