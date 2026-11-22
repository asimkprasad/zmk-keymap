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

from zmk_keymap import keymap_renderer as renderer, keymap_snapshot as pb
from zmk_keymap.keymap_renderer import KeymapRenderer, _key_labels
from zmk_studio_client import model as client_model

DATA = pathlib.Path(__file__).resolve().parent / "data"
CORPORA = sorted(DATA.glob("*.json"))
SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module", params=CORPORA, ids=lambda p: p.stem)
def snapshot(request: pytest.FixtureRequest) -> pb.KeymapSnapshot:
    return pb.KeymapSnapshot.from_json(request.param.read_text(encoding="utf-8"))


def _tiny(first_param1: int = 458756) -> pb.KeymapSnapshot:
    """A two-key snapshot, small enough that its SVG can be read by eye."""
    kp = client_model.Behavior(id=5, display_name="Key Press")
    pset = kp.parameter_sets.add()
    slot = pset.param1.add(name="Key")
    slot.hid_usage.keyboard_max = 0xFF
    trans = client_model.Behavior(id=22, display_name="Transparent")
    trans.parameter_sets.add()

    return pb.KeymapSnapshot(
        device=pb.Device(name="Tiny"),
        behaviors=[kp, trans],
        layout=pb.PhysicalLayout(
            name="Tiny",
            keys=[
                pb.KeyPosition(position=0, x=0, y=0, width=100, height=100),
                pb.KeyPosition(position=1, x=100, y=0, width=100, height=100),
            ],
        ),
        layers=[(0, "BASE", [(0, 5, first_param1, 0), (1, 22, 0, 0)])],
    )


# --------------------------------------------------------------------------- #
# determinism - the property every other use of the output depends on
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", KeymapRenderer.FORMATS)
def test_rendering_twice_gives_the_same_bytes(snapshot, fmt) -> None:
    """Output is diffable and golden-testable only if it is stable."""
    once = KeymapRenderer(snapshot).render(fmt)
    assert once == KeymapRenderer(snapshot).render(fmt)


def test_the_only_timestamp_is_the_capture_time(snapshot) -> None:
    """A render clock would make every output differ from the last for no reason."""
    when = snapshot.metadata.captured_at.ToDatetime().isoformat()
    page = KeymapRenderer(snapshot).to_html()
    stamps = set(re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*", page))
    assert stamps <= {when}


# --------------------------------------------------------------------------- #
# svg
# --------------------------------------------------------------------------- #

def test_svg_is_well_formed_xml(snapshot) -> None:
    root = ET.fromstring(KeymapRenderer(snapshot).to_svg().split("?>", 1)[1])
    assert root.tag == f"{SVG_NS}svg"


def test_svg_draws_every_key_in_the_layout(snapshot) -> None:
    root = ET.fromstring(KeymapRenderer(snapshot).to_svg().split("?>", 1)[1])
    groups = root.findall(f"{SVG_NS}g")
    assert len(groups) == len(snapshot.layout.keys)


def test_svg_carries_the_rotations_the_keyboard_reported(snapshot) -> None:
    """The thumb cluster is the part a hand-drawn layout gets wrong."""
    root = ET.fromstring(KeymapRenderer(snapshot).to_svg().split("?>", 1)[1])
    rotated = [
        t for g in root.findall(f"{SVG_NS}g")
        if (t := g.get("transform")) is not None
    ]
    expected = sum(1 for key in snapshot.layout.keys if key.rotation)
    assert len(rotated) == expected
    for key in snapshot.layout.keys:
        if key.rotation:
            wanted = f"rotate({renderer._num(key.rotation / 100)},"
            assert any(t.startswith(wanted) for t in rotated), wanted


def test_svg_rejects_a_layer_the_snapshot_does_not_have(snapshot) -> None:
    with pytest.raises(ValueError, match="no layer with id 99"):
        KeymapRenderer(snapshot).to_svg(99)


def test_svg_defaults_to_display_order_not_id_order(snapshot) -> None:
    """FUN is id 5 and displays fourth; 'the first layer' means the first shown."""
    first = snapshot.layers[0]
    assert f"layer {first.id}: {first.name}" in KeymapRenderer(snapshot).to_svg()


def test_tiny_snapshot_renders_exactly_this() -> None:
    """A golden small enough to read, so a change to the drawing is visible here."""
    body = KeymapRenderer(_tiny()).to_svg().split("?>", 1)[1].strip()
    body = body.split("</style>", 1)[1]  # the stylesheet has its own test
    assert body == (
        '<g class="key" data-pos="0"><title>position 0\n&amp;kp A\nbehavior: Key Press (id 5)\n'
        'param1: A [Key, HID usage, raw 0x00070004 / 458756]</title>'
        '<rect class="cap" x="4" y="4" width="92" height="92" rx="11"/>'
        '<text class="pos" x="13" y="25">0</text>'
        '<g class="lbl-friendly"><text class="main" x="50" y="60.8" font-size="30">A</text></g>'
        '<g class="lbl-raw"><text class="main" x="50" y="60.8" font-size="30">A</text></g></g>'
        '<g class="key" data-pos="1"><title>position 1\n&amp;trans\n'
        'behavior: Transparent (id 22)\nno parameters reported</title>'
        '<rect class="cap trans" x="104" y="4" width="92" height="92" rx="11"/>'
        '<text class="pos" x="113" y="25">1</text>'
        '<g class="lbl-friendly"><text class="main muted" x="150" y="62.24" font-size="34">▽</text></g>'
        '<g class="lbl-raw"><text class="main muted" x="150" y="62.24" font-size="34">▽</text></g></g>'
        '</svg>'
    )


def test_diff_svg_highlights_changed_keys_and_dims_unchanged_ones() -> None:
    before = _tiny()
    after = _tiny(first_param1=0x08070006)
    diff = before.diff(after)
    raw_svg = KeymapRenderer(after).to_svg(diff=diff)
    root = ET.fromstring(raw_svg.split("?>", 1)[1])
    groups = root.findall(f"{SVG_NS}g")
    assert len(groups) == 2
    assert groups[0].get("class") == "key changed"
    assert groups[0].get("data-pos") == "0"
    assert groups[1].get("class") == "key unchanged"
    assert groups[1].get("data-pos") == "1"
    assert "− A" in raw_svg
    assert "+ ⌘C" in raw_svg
    assert "+ LG(C)" in raw_svg


def test_the_page_offers_pure_css_raw_and_all_layers_toggles(snapshot) -> None:
    page = KeymapRenderer(snapshot).to_html()
    assert 'id="ctl-raw"' in page
    assert 'id="ctl-all"' in page
    assert "#ctl-raw:checked ~ main .lbl-raw" in page
    assert "#ctl-all:checked ~ main .pane" in page


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #

def test_the_page_has_no_javascript(snapshot) -> None:
    """The whole reason layout is computed here: the page is inert."""
    page = KeymapRenderer(snapshot).to_html().lower()
    assert "<script" not in page
    assert "javascript:" not in page
    assert "onclick" not in page and "onload" not in page


def test_the_page_fetches_nothing(snapshot) -> None:
    """Self-contained: it has to work from file:// with no network."""
    page = KeymapRenderer(snapshot).to_html()
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in page
    assert "@import" not in page


def test_the_page_shows_every_layer(snapshot) -> None:
    page = KeymapRenderer(snapshot).to_html()
    for index, _ in enumerate(snapshot.layers):
        assert f'id="pane-{index}"' in page
        assert f'id="tab-{index}"' in page
        assert f"#tab-{index}:checked ~ main #pane-{index}{{display:block}}" in page


def test_exactly_one_tab_starts_checked(snapshot) -> None:
    assert KeymapRenderer(snapshot).to_html().count(" checked>") == 1


def test_the_page_states_every_binding_in_full(snapshot) -> None:
    """The board shrinks labels to fit; the text view is the lossless copy."""
    page = KeymapRenderer(snapshot).to_html()
    for layer in snapshot.layers:
        for binding in layer.bindings:
            assert _escape(binding.decoded) in page


def test_every_key_carries_its_raw_parameters(snapshot) -> None:
    """The tooltip is where a reader checks the decoding rather than trusting it."""
    page = KeymapRenderer(snapshot).to_html()
    for layer in snapshot.layers:
        for binding in layer.bindings:
            for index, arg in enumerate(binding.arguments, start=1):
                assert f"param{index}: " in page
                assert f"raw 0x{arg.raw:08x} / {arg.raw}" in page


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #

def test_a_two_parameter_binding_shows_both_halves(snapshot) -> None:
    """The thing a screenshot of Studio cannot show."""
    for layer in snapshot.layers:
        for binding in layer.bindings:
            if len(binding.arguments) == 2:
                cap = _key_labels.key_label(binding)
                assert cap.hold and cap.main
                return
    pytest.skip("no two-parameter binding in this corpus")


def test_a_layer_parameter_reads_as_a_layer(snapshot) -> None:
    for layer in snapshot.layers:
        for binding in layer.bindings:
            if any(a.kind == pb.ARGUMENT_KIND_LAYER_ID for a in binding.arguments):
                cap = _key_labels.key_label(binding)
                assert cap.raw_main.startswith("L") or cap.raw_hold.startswith("L")
                return
    pytest.skip("no layer parameter in this corpus")


def test_dead_and_transparent_are_drawn_differently(snapshot) -> None:
    """Studio draws these identically; telling them apart is a reason this exists."""
    dead = pb.KeyBinding(behavior_name="None")
    through = pb.KeyBinding(behavior_name="Transparent")
    assert _key_labels.key_label(dead).main != _key_labels.key_label(through).main


def test_every_cap_has_something_on_it(snapshot) -> None:
    for layer in snapshot.layers:
        for binding in layer.bindings:
            assert _key_labels.key_label(binding).main.strip()


# --------------------------------------------------------------------------- #
# wrapping
# --------------------------------------------------------------------------- #

def test_a_long_phrase_wraps_rather_than_vanishing() -> None:
    """"Clear Selected Profile" at one line either overflows or is unreadable."""
    lines = renderer._wrap("Clear Selected Profile", 82, 31)
    assert len(lines) > 1
    assert " ".join(lines) == "Clear Selected Profile"


def test_a_single_word_is_never_broken() -> None:
    """A mangled 'Saturation' is worse than a small one; _fit shrinks it instead."""
    assert renderer._wrap("Saturation", 82, 31) == ["Saturation"]


def test_a_short_label_stays_on_one_line() -> None:
    assert renderer._wrap("C_PP", 82, 31) == ["C_PP"]


def test_single_escaping_chokepoint_strips_c0_and_parses_as_strict_xml(snapshot) -> None:
    """P1-6 + escaping chokepoint: standalone SVG stays valid XML under C0 controls & quotes."""
    import inspect
    import xml.etree.ElementTree as ET

    src = inspect.getsource(renderer)
    # `_html.escape` must appear solely inside `_esc()`.
    assert src.count("_html.escape(") == 1

    hostile_layer = pb.Layer(
        id=snapshot.layers[0].id,
        display_order=0,
        name="BASE\x00\x01\x07\x0b\x1f\x7f<>&\"'",
        bindings=snapshot.layers[0].bindings,
    )
    hostile = pb.KeymapSnapshot(
        device=pb.Device(name="Sofle\x00\x02<svg>", id=snapshot.device.id),
        layout=snapshot.layout,
        behaviors=snapshot.behaviors,
        layers=[hostile_layer, *list(snapshot.layers)[1:]],
    )
    svg_doc = KeymapRenderer(hostile).to_svg(hostile_layer.id)
    root = ET.fromstring(svg_doc)
    assert root.tag.endswith("svg")


def test_duplicate_layer_ids_render_each_layers_own_bindings_in_html(snapshot) -> None:
    """P1-3 regression: `layer_svg(layer)` uses `layer.bindings`, not last-wins `layer.id`."""
    layer_a = snapshot.layers[0]
    layer_b = pb.Layer(
        id=layer_a.id,
        display_order=1,
        name="SECOND_SAME_ID",
        bindings=snapshot.layers[1].bindings,
    )
    dup_snap = pb.KeymapSnapshot(
        device=snapshot.device,
        layout=snapshot.layout,
        behaviors=snapshot.behaviors,
        layers=[layer_a, layer_b],
    )
    pres = KeymapRenderer(dup_snap)
    assert pres.layer_svg(layer_a) != pres.layer_svg(layer_b)


def test_viewbox_encloses_rotated_key_corners() -> None:
    """P1-7 regression: `_viewbox()` rotates key corners around `(rotation_x, rotation_y)`."""
    anchor = pb.KeyPosition(position=0, x=0, y=0, width=100, height=100)
    rotated = pb.KeyPosition(
        position=1,
        x=100,
        y=100,
        width=100,
        height=100,
        rotation=9000,  # +90 deg clockwise in SVG (Y-down) around (0, 500) -> x in [300, 400], y in [600, 700]
        rotation_x=0,
        rotation_y=500,
    )
    left, top, width, height = [
        float(v) for v in renderer._viewbox([anchor, rotated]).split()
    ]
    assert left <= -renderer._PAD
    assert top <= -renderer._PAD
    assert left + width >= 400 + renderer._PAD
    assert top + height >= 700 + renderer._PAD


def test_wrapping_never_exceeds_the_line_budget() -> None:
    assert len(renderer._wrap("a b c d e f g h i j k", 82, 31)) <= renderer._MAX_LINES
