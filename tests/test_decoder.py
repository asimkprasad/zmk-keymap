"""Binding decoding into `KeyBinding` & `BehaviorArgument`, driven by behavior metadata.

`test_golden_corpus.py` checks the committed real-hardware snapshot; these cover
shapes and edge cases a single keymap happens not to contain.
"""
from __future__ import annotations

import pytest

from zmk_keymap import keymap_snapshot as pb
from zmk_keymap.keymap_snapshot import _decoder
from zmk_studio_client import model


def behavior(display_name: str, *sets) -> model.Behavior:
    """Build a Behavior whose parameter sets are given as (param1, param2) specs.

    Each spec is a list of (name, kind, value) describing one declared slot, where
    kind is a ParameterValue oneof field name.
    """
    out = model.Behavior(display_name=display_name)
    for slot1, slot2 in sets:
        parameter_set = out.parameter_sets.add()
        for declared, specs in ((parameter_set.param1, slot1), (parameter_set.param2, slot2)):
            for name, kind, value in specs:
                described = declared.add()
                described.name = name
                if kind == "constant":
                    described.constant = value
                elif kind == "range":
                    described.range.min, described.range.max = value
                elif kind == "hid_usage":
                    described.hid_usage.keyboard_max = value
                elif kind == "layer_id":
                    described.layer_id.SetInParent()
                elif kind == "no_value":
                    described.no_value.SetInParent()
    return out


KEY = ("Key", "hid_usage", 0xFF)
LAYER = ("Layer", "layer_id", 0)

KEY_PRESS = behavior("Key Press", ([KEY], []))
MOD_TAP = behavior("Mod-Tap", ([KEY], [KEY]))
LAYER_TAP = behavior("Layer-Tap", ([LAYER], [KEY]))
TRANSPARENT = behavior("Transparent", ([], []))
BLUETOOTH = behavior(
    "Bluetooth",
    ([("Select Profile", "constant", 3)], [("Profile", "range", (0, 5))]),
    ([("Clear All Profiles", "constant", 4)], []),
)


@pytest.mark.parametrize("name,behave,p1,p2,expected", [
    ("key press", KEY_PRESS, 0x00070006, 0, "&kp C"),
    ("mod-tap", MOD_TAP, 0x08070006, 0x00070006, "&mt LG(C) C"),
    ("layer-tap", LAYER_TAP, 5, 0x000700E0, "&lt 5 LCTRL"),
    ("transparent", TRANSPARENT, 0, 0, "&trans"),
])
def test_common_bindings_decode_without_a_special_case(name, behave, p1, p2, expected):
    assert pb.decode_binding(behave, p1, p2).decoded == expected


def test_a_profile_of_zero_is_not_dropped() -> None:
    """&bt BT_SEL 0 selects the first Bluetooth profile; 0 is a valid range value."""
    binding = pb.decode_binding(BLUETOOTH, 3, 0, position=7, behavior_id=25)
    assert isinstance(binding, pb.KeyBinding)
    assert binding.position == 7
    assert binding.behavior_id == 25
    assert binding.decoded == "&bt Select Profile 0"
    assert [(a.name, a.raw, a.kind) for a in binding.arguments] == [
        ("Select Profile", 3, pb.ARGUMENT_KIND_CONSTANT),
        ("Profile", 0, pb.ARGUMENT_KIND_RANGE),
    ]


def test_the_matching_parameter_set_is_chosen() -> None:
    """Bluetooth declares two sets; only one admits a lone parameter 4."""
    assert pb.decode_binding(BLUETOOTH, 4, 0).decoded == "&bt Clear All Profiles"


def test_a_value_no_declared_set_admits_falls_back_to_raw() -> None:
    # 9 is neither of Bluetooth's declared constants.
    binding = pb.decode_binding(BLUETOOTH, 9, 0)
    assert binding.decoded == "&bt 9"
    assert [a.kind for a in binding.arguments] == [pb.ARGUMENT_KIND_UNDECLARED]


def test_a_behavior_reporting_no_metadata_passes_its_parameters_through() -> None:
    """Some ZMK behaviors (e.g. External Power) report no parameter metadata."""
    ext_power = model.Behavior(display_name="External Power")
    binding = pb.decode_binding(ext_power, 2, 0)
    assert binding.decoded == "&ext_power 2"
    assert [(a.name, a.raw, a.decoded, a.kind) for a in binding.arguments] == [
        ("", 2, "2", pb.ARGUMENT_KIND_UNDECLARED)
    ]


def test_an_undeclared_leading_zero_survives() -> None:
    """Only trailing zeros are dropped: a set second slot proves the first is real."""
    unknown = model.Behavior(display_name="Key Press")
    assert pb.decode_binding(unknown, 0, 7).decoded == "&kp 0 7"


def test_an_unknown_behavior_cannot_be_mistaken_for_keymap_syntax() -> None:
    """A keymap-defined behavior has no devicetree label this tool can know."""
    binding = pb.decode_binding(model.Behavior(display_name="hyper"), 458756, 0)
    assert binding.decoded == "<hyper 458756>"
    assert not binding.decoded.startswith("&")


def test_a_keymap_defined_behavior_declaring_no_parameters_decodes_bare() -> None:
    """An empty parameter set is the keyboard saying "none", not saying nothing."""
    hyper = behavior("hyper", ([], []))
    binding = pb.decode_binding(hyper, 0, 0)
    assert binding.decoded == "<hyper>"
    assert list(binding.arguments) == []


@pytest.mark.parametrize("param,expected", [
    (0x00070006, "C"),
    (0x08070006, "LG(C)"),
    (0x0407002A, "LA(BSPC)"),
    (0x02070035, "LS(GRAVE)"),
    (0x03070004, "LC(LS(A))"),
    (0x000C00E2, "C_MUTE"),
    (0x000C0223, "C_AC_HOME"),
    (0x00070041, "F8"),
    (0, "-"),
    (0x00990001, "0x00990001"),
])
def test_keycode_unpacking(param, expected) -> None:
    assert _decoder.keycode(param) == expected
