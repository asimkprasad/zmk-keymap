"""What the presenter promises about its output.

The point of rendering in Python rather than in a browser is that these
properties are checkable. A page built by script could only be checked by
running a browser.
"""
from __future__ import annotations

import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

from zmk_keymap import from_json, model as pb, render

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPORA = sorted(DATA.glob("*.json"))
SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module", params=CORPORA, ids=lambda p: p.stem)
def snapshot(request: pytest.FixtureRequest) -> pb.KeymapSnapshot:
    return from_json(request.param.read_text(encoding="utf-8"))


def _tiny() -> pb.KeymapSnapshot:
    """A two-key snapshot, small enough that its SVG can be read by eye."""
    snap = pb.KeymapSnapshot()
    snap.device.name = "Tiny"
    snap.layout.name = "Tiny"
    for position, x in ((0, 0), (1, 100)):
        key = snap.layout.keys.add()
        key.position, key.x, key.y, key.width, key.height = position, x, 0, 100, 100
    layer = snap.layers.add()
    layer.id, layer.name = 0, "BASE"
    first = layer.bindings.add()
    first.position, first.behavior_id, first.behavior_name = 0, 5, "Key Press"
    first.param1, first.rendered = 458756, "&kp A"
    param = first.params.add()
    param.name, param.raw, param.decoded = "Key", 458756, "A"
    param.kind = pb.PARAMETER_KIND_HID_USAGE
    second = layer.bindings.add()
    second.position, second.behavior_id = 1, 22
    second.behavior_name, second.rendered = "Transparent", "&trans"
    return snap


# --------------------------------------------------------------------------- #
# determinism - the property every other use of the output depends on
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", sorted(render.FORMATS))
def test_rendering_twice_gives_the_same_bytes(snapshot, fmt) -> None:
    """Output is diffable and golden-testable only if it is stable."""
    once = render.FORMATS[fmt](snapshot)
    assert once == render.FORMATS[fmt](snapshot)


def test_the_only_timestamp_is_the_capture_time(snapshot) -> None:
    """A render clock would make every output differ from the last for no reason."""
    when = snapshot.capture.captured_at.ToDatetime().isoformat()
    page = render.page(snapshot)
    stamps = set(re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*", page))
    assert stamps <= {when}


# --------------------------------------------------------------------------- #
# svg
# --------------------------------------------------------------------------- #

def test_svg_is_well_formed_xml(snapshot) -> None:
    root = ET.fromstring(render.svg(snapshot).split("?>", 1)[1])
    assert root.tag == f"{SVG_NS}svg"


def test_svg_draws_every_key_in_the_layout(snapshot) -> None:
    root = ET.fromstring(render.svg(snapshot).split("?>", 1)[1])
    groups = root.findall(f"{SVG_NS}g")
    assert len(groups) == len(snapshot.layout.keys)


def test_svg_carries_the_rotations_the_keyboard_reported(snapshot) -> None:
    """The thumb cluster is the part a hand-drawn layout gets wrong."""
    root = ET.fromstring(render.svg(snapshot).split("?>", 1)[1])
    rotated = [
        t for g in root.findall(f"{SVG_NS}g")
        if (t := g.get("transform")) is not None
    ]
    expected = sum(1 for key in snapshot.layout.keys if key.rotation)
    assert len(rotated) == expected
    for key in snapshot.layout.keys:
        if key.rotation:
            wanted = f"rotate({render._num(key.rotation / 100)},"
            assert any(t.startswith(wanted) for t in rotated), wanted


def test_svg_rejects_a_layer_the_snapshot_does_not_have(snapshot) -> None:
    with pytest.raises(ValueError, match="no layer with id 99"):
        render.svg(snapshot, 99)


def test_svg_defaults_to_display_order_not_id_order(snapshot) -> None:
    """FUN is id 5 and displays fourth; "the first layer" means the first shown."""
    first = snapshot.layers[0]
    assert f"layer {first.id}: {first.name}" in render.svg(snapshot)


def test_tiny_snapshot_renders_exactly_this() -> None:
    """A golden small enough to read, so a change to the drawing is visible here."""
    body = render.svg(_tiny()).split("?>", 1)[1].strip()
    body = body.split("</style>", 1)[1]  # the stylesheet has its own test
    assert body == (
        '<g class="key"><title>position 0\n&amp;kp A\nbehavior: Key Press (id 5)\n'
        'param1: A [Key, HID usage, raw 0x00070004 / 458756]</title>'
        '<rect class="cap" x="4" y="4" width="92" height="92" rx="11"/>'
        '<text class="pos" x="13" y="25">0</text>'
        '<text class="main" x="50" y="61.16" font-size="31">A</text></g>'
        '<g class="key"><title>position 1\n&amp;trans\n'
        'behavior: Transparent (id 22)\nno parameters reported</title>'
        '<rect class="cap trans" x="104" y="4" width="92" height="92" rx="11"/>'
        '<text class="pos" x="113" y="25">1</text>'
        '<text class="main muted" x="150" y="62.24" font-size="34">▽</text></g>'
        '</svg>'
    )


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #

def test_the_page_has_no_javascript(snapshot) -> None:
    """The whole reason layout is computed here: the page is inert."""
    page = render.page(snapshot).lower()
    assert "<script" not in page
    assert "javascript:" not in page
    assert "onclick" not in page and "onload" not in page


def test_the_page_fetches_nothing(snapshot) -> None:
    """Self-contained: it has to work from file:// with no network."""
    page = render.page(snapshot)
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in page
    assert "@import" not in page


def test_the_page_shows_every_layer(snapshot) -> None:
    page = render.page(snapshot)
    for index, _ in enumerate(snapshot.layers):
        assert f'id="pane-{index}"' in page
        assert f'id="tab-{index}"' in page
        # the CSS that makes the radio into a tab, without which it is invisible
        assert f"#tab-{index}:checked ~ main #pane-{index}{{display:block}}" in page


def test_exactly_one_tab_starts_checked(snapshot) -> None:
    assert render.page(snapshot).count(" checked>") == 1


def test_the_page_states_every_binding_in_full(snapshot) -> None:
    """The board shrinks labels to fit; the text view is the lossless copy."""
    page = render.page(snapshot)
    for layer in snapshot.layers:
        for binding in layer.bindings:
            assert _escape(binding.rendered) in page


def test_every_key_carries_its_raw_parameters(snapshot) -> None:
    """The tooltip is where a reader checks the decoding rather than trusting it."""
    page = render.page(snapshot)
    for layer in snapshot.layers:
        for binding in layer.bindings:
            for index, param in enumerate(binding.params, start=1):
                assert f"param{index}: " in page
                assert f"raw 0x{param.raw:08x} / {param.raw}" in page


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #

def test_a_two_parameter_binding_shows_both_halves(snapshot) -> None:
    """The thing a screenshot of Studio cannot show."""
    for layer in snapshot.layers:
        for binding in layer.bindings:
            if len(binding.params) == 2:
                cap = render.label(binding)
                assert cap.hold and cap.main
                return
    pytest.skip("no two-parameter binding in this corpus")


def test_a_layer_parameter_reads_as_a_layer(snapshot) -> None:
    for layer in snapshot.layers:
        for binding in layer.bindings:
            for param in binding.params:
                if param.kind == pb.PARAMETER_KIND_LAYER_ID:
                    assert render._slot(param).startswith("L")
                    return
    pytest.skip("no layer parameter in this corpus")


def test_dead_and_transparent_are_drawn_differently(snapshot) -> None:
    """Studio draws these identically; telling them apart is a reason this exists."""
    dead = pb.Binding(behavior_name="None")
    through = pb.Binding(behavior_name="Transparent")
    assert render.label(dead).main != render.label(through).main


def test_every_cap_has_something_on_it(snapshot) -> None:
    for layer in snapshot.layers:
        for binding in layer.bindings:
            assert render.label(binding).main.strip()


# --------------------------------------------------------------------------- #
# wrapping
# --------------------------------------------------------------------------- #

def test_a_long_phrase_wraps_rather_than_vanishing() -> None:
    """"Clear Selected Profile" at one line either overflows or is unreadable."""
    lines = render._wrap("Clear Selected Profile", 82, 31)
    assert len(lines) > 1
    assert " ".join(lines) == "Clear Selected Profile"


def test_a_single_word_is_never_broken() -> None:
    """A mangled "Saturation" is worse than a small one; _fit shrinks it instead."""
    assert render._wrap("Saturation", 82, 31) == ["Saturation"]


def test_a_short_label_stays_on_one_line() -> None:
    assert render._wrap("C_PP", 82, 31) == ["C_PP"]


def test_wrapping_never_exceeds_the_line_budget() -> None:
    assert len(render._wrap("a b c d e f g h i j k", 82, 31)) <= render._MAX_LINES
