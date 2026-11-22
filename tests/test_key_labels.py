"""Human-friendly keycap labels (`_key_labels`) and raw ZMK fallbacks."""
from __future__ import annotations

import pathlib
import time

import pytest

from zmk_keymap import keymap_snapshot as pb
from zmk_keymap.keymap_renderer import KeymapRenderer, _key_labels

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPUS = sorted(DATA.glob("*.json"))[0]


@pytest.mark.parametrize(
    "token,expected",
    [
        ("A", "A"),
        ("N4", "4"),
        ("BSPC", "⌫"),
        ("RET", "⏎"),
        ("SPACE", "␣"),
        ("FSLH", "/"),
        ("LS(N4)", "$"),
        ("LS(GRAVE)", "~"),
        ("LS(LBKT)", "{"),
        ("LG(C)", "⌘C"),
        ("LA(LEFT)", "⌥←"),
        ("LC(LS(LG(N4)))", "⌃⇧⌘4"),
        # Shift combined with another modifier stays `⌃⇧4`, never `⌃$`.
        ("LC(LS(N4))", "⌃⇧4"),
        ("C_VOL_UP", "Vol +"),
    ],
)
def test_friendly_keycode_formats_modifiers_and_us_shifted_punctuation(
    token: str, expected: str
) -> None:
    assert _key_labels.friendly_keycode(token) == expected


def test_layer_tap_uses_layer_name_in_friendly_and_layer_id_in_raw() -> None:
    binding = pb.KeyBinding(
        position=38,
        behavior_id=18,
        behavior_name="Layer-Tap",
        decoded="&lt 4 RET",
    )
    p1 = binding.arguments.add(
        name="Layer", raw=4, decoded="4", kind=pb.ARGUMENT_KIND_LAYER_ID
    )
    assert p1.raw == 4
    binding.arguments.add(
        name="Tap", raw=0x070028, decoded="RET", kind=pb.ARGUMENT_KIND_HID_USAGE
    )

    cap = _key_labels.key_label(binding, {0: "BASE", 4: "SYM"})
    assert cap.hold == "SYM"
    assert cap.main == "⏎"
    assert cap.raw_hold == "L4"
    assert cap.raw_main == "RET"
    assert cap.summary == "SYM / ⏎"


def test_command_behaviors_do_not_misrender_first_arg_as_hold() -> None:
    bt = pb.KeyBinding(
        position=1,
        behavior_id=25,
        behavior_name="Bluetooth",
        decoded="&bt Select Profile 0",
    )
    bt.arguments.add(
        name="Command", raw=0, decoded="Select Profile", kind=pb.ARGUMENT_KIND_CONSTANT
    )
    bt.arguments.add(name="Profile", raw=0, decoded="0", kind=pb.ARGUMENT_KIND_RANGE)

    cap = _key_labels.key_label(bt)
    assert cap.main == "BT 0"
    assert cap.hold == ""
    assert cap.raw_main == "bt Select Profile 0"
    assert cap.raw_hold == ""


def test_to_layer_and_sticky_key_use_distinct_prefixes() -> None:
    to_layer = pb.KeyBinding(behavior_name="To Layer", decoded="&to 0")
    to_layer.arguments.add(name="Layer", raw=0, decoded="0", kind=pb.ARGUMENT_KIND_LAYER_ID)
    assert _key_labels.key_label(to_layer, {0: "BASE"}).main == "→ BASE"

    sticky = pb.KeyBinding(behavior_name="Sticky Key", decoded="&sk LSHFT")
    sticky.arguments.add(
        name="Key", raw=0x0700E1, decoded="LSHFT", kind=pb.ARGUMENT_KIND_HID_USAGE
    )
    assert _key_labels.key_label(sticky).main == "1× ⇧"


def test_every_binding_in_real_corpus_has_non_empty_friendly_and_raw_legends() -> None:
    snap = pb.KeymapSnapshot.from_json(CORPUS.read_text(encoding="utf-8"))
    pres = KeymapRenderer(snap)
    checked = 0
    for layer in snap.layers:
        for binding in layer.bindings:
            cap = _key_labels.key_label(binding, pres.layer_names)
            assert cap.main.strip(), f"empty friendly main at {layer.name}#{binding.position}"
            assert cap.raw_main.strip(), f"empty raw main at {layer.name}#{binding.position}"
            checked += 1
    assert checked == 360


def test_deeply_nested_modifier_wrappers_run_in_constant_time() -> None:
    """Regression test: adversarial nested `LC(...)` strings must not go quadratic."""
    deep = "LC(" * 100_000 + "A" + ")" * 100_000
    t0 = time.perf_counter()
    out = _key_labels.friendly_keycode(deep)
    elapsed = time.perf_counter() - t0
    assert out.startswith("⌃")
    assert elapsed < 0.2
