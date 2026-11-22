"""Rendering a `KeymapSnapshot` as readable text, SVG, diff SVG, or standalone HTML.

`KeymapRenderer(snapshot)` pre-indexes a snapshot's layer names and per-position
bindings once, and exposes deterministic formatters (`to_text`, `to_svg`, `to_html`,
`layer_svg`, `diff_layer_svg`, `to_diff_svg`) with zero `zmk_studio_client` runtime
imports or I/O — checked by `tests/test_isolation.py`.

## Why there is no JavaScript in `to_html()`

The board is drawn in Python as SVG. The standalone HTML page switches layers with
radio inputs and CSS sibling selectors, toggles Friendly vs. Raw ZMK labels and
Single vs. All-Layers layout with CSS `:checked` selectors, and shows raw binding
parameters through native SVG `<title>` tooltips.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import html as _html
import math
import re
from types import MappingProxyType
import typing

from . import _key_labels
from .. import keymap_diff, keymap_snapshot as pb

__all__ = [
    "KeymapRenderer",
]

#: XML 1.0 forbids C0 controls other than TAB/LF/CR, plus U+FFFE/U+FFFF; also strip DEL.
_ILLEGAL_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def _esc(value: str) -> str:
    """Single XML/HTML escaping chokepoint: strip XML-illegal control bytes, then escape."""
    return _html.escape(_ILLEGAL_XML_CHARS_RE.sub("", value), quote=True)

#: Geometry is in hundredths of a key unit, used directly as SVG user units.
_GAP = 4          # trimmed from each side of a key, so a 1u cap is 92 wide
_PAD = 34         # viewBox padding, enough to clear the rotated thumb cluster

#: Monospace advance width as a fraction of font size, for shrink-to-fit.
_ADVANCE = 0.60

#: Floor for shrink-to-fit, and the size a wrapped label is set at.
_MIN_SIZE = 12.0
_WRAP_SIZE = 16.0
_MAX_LINES = 3

_GLYPH_TRANS = _key_labels.GLYPH_TRANS
_GLYPH_NONE = _key_labels.GLYPH_NONE

_SVG_STYLE = """
.key text { text-anchor: middle; font-family: ui-monospace, SFMono-Regular, Menlo,
  Consolas, "DejaVu Sans Mono", monospace; }
.lbl-raw { display: none; }
.cap { fill: #ebebe8; stroke: #c3c5cf; stroke-width: 3; }
.cap.trans { fill: none; stroke: #d9dae0; stroke-dasharray: 7 7; }
.cap.dead { fill: none; stroke: #d9dae0; stroke-width: 2; }
.cap.changed { fill: #eef1fd; stroke: #4a5ee0; stroke-width: 4; }
.key.unchanged { opacity: .36; }
.key.selected .cap { stroke: #4a5ee0; stroke-width: 5; stroke-dasharray: none; }
.main { fill: #17191c; font-weight: 500; }
.main.muted { fill: #9aa0b0; font-weight: 400; }
.hold { fill: #a65c26; font-weight: 600; }
.diff-old { fill: #8c4b3e; font-weight: 500; }
.diff-new { fill: #2438b8; font-weight: 700; }
.diff-dot { fill: #4a5ee0; }
.pos { fill: #9aa0b0; font-size: 15px; opacity: .55; text-anchor: start; }
@media (prefers-color-scheme: dark) {
  .cap { fill: #23262e; stroke: #3a3f4c; }
  .cap.trans, .cap.dead { fill: none; stroke: #2b2f39; }
  .cap.changed { fill: #1f2640; stroke: #8fa0f8; stroke-width: 4; }
  .key.selected .cap { stroke: #8fa0f8; stroke-width: 5; stroke-dasharray: none; }
  .main { fill: #e8eaef; }
  .main.muted, .pos { fill: #636a7c; }
  .hold { fill: #e0a26a; }
  .diff-old { fill: #d88c7e; }
  .diff-new { fill: #a8b7ff; }
  .diff-dot { fill: #8fa0f8; }
}
"""

_PAGE_STYLE = """
:root {
  --bg:#f3f3f1; --panel:#fff; --panel2:#ebebe8; --ink:#17191c; --dim:#63687a;
  --faint:#9aa0b0; --line:#d9dae0; --line2:#c3c5cf; --accent:#4a5ee0; --hold:#a65c26;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#111317; --panel:#1a1d23; --panel2:#23262e; --ink:#e8eaef; --dim:#99a0b2;
    --faint:#636a7c; --line:#2b2f39; --line2:#3a3f4c; --accent:#8fa0f8; --hold:#e0a26a;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:15px; line-height:1.5; }
.wrap { max-width:1680px; margin:0 auto; padding:26px 16px 60px; }
h1 { font-size:1.6rem; margin:0 0 4px; letter-spacing:-.02em; }
.lede { margin:0 0 16px; color:var(--dim); font-size:.92rem; max-width:66ch; }
.meta { display:flex; flex-wrap:wrap; gap:6px 9px; font-family:var(--mono);
  font-size:.72rem; color:var(--dim); margin:0 0 22px; padding:0; list-style:none; }
.meta li { background:var(--panel); border:1px solid var(--line); border-radius:999px;
  padding:3px 10px; white-space:nowrap; }
.meta b { color:var(--ink); font-weight:600; }
.tab-radio, .ctl-check { position:absolute; width:1px; height:1px; opacity:0; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
.tabs label, .view-toggles label { font-size:.82rem; font-weight:600; background:var(--panel);
  color:var(--dim); border:1px solid var(--line); border-radius:7px; padding:7px 12px;
  cursor:pointer; display:inline-flex; align-items:baseline; gap:7px; user-select:none; }
.tabs label .lid { font-family:var(--mono); font-size:.68rem; color:var(--faint);
  font-weight:500; }
.tabs label:hover, .view-toggles label:hover { color:var(--ink); border-color:var(--line2); }
.view-toggles { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }
.pane { display:none; }
.pane-title { display:none; font-size:.95rem; font-weight:650; margin:0 0 8px; }
.pane-title span { font-family:var(--mono); font-size:.76rem; color:var(--faint);
  font-weight:500; margin-left:6px; }
#ctl-raw:checked ~ main .lbl-friendly { display:none; }
#ctl-raw:checked ~ main .lbl-raw { display:inline; }
#ctl-raw:checked ~ .view-toggles label[for="ctl-raw"] {
  background:var(--accent); border-color:var(--accent); color:#fff; }
#ctl-all:checked ~ main .pane { display:block; margin-bottom:28px; }
#ctl-all:checked ~ main .pane-title { display:block; }
#ctl-all:checked ~ .tabs { opacity:.45; pointer-events:none; }
#ctl-all:checked ~ .view-toggles label[for="ctl-all"] {
  background:var(--accent); border-color:var(--accent); color:#fff; }
.board { background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:14px 12px; overflow-x:auto; margin-bottom:14px; }
.board svg { display:block; width:100%; min-width:620px; height:auto; }
.legend { display:flex; flex-wrap:wrap; gap:8px 18px; font-size:.78rem; color:var(--dim);
  margin:0 0 20px; padding:0; list-style:none; align-items:center; }
.legend i { display:inline-block; width:11px; height:11px; border-radius:3px;
  margin-right:6px; vertical-align:-1px; }
.sw-tap { background:var(--ink); }
.sw-hold { background:var(--hold); }
.sw-trans { border:1.5px dashed var(--line2); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:15px 16px; margin-bottom:16px; }
.card h2 { font-size:.74rem; text-transform:uppercase; letter-spacing:.07em;
  color:var(--dim); margin:0 0 11px; font-weight:600; }
pre { font-family:var(--mono); font-size:.76rem; line-height:1.6; margin:0;
  max-height:460px; overflow:auto; white-space:pre; }
table { border-collapse:collapse; width:100%; font-size:.8rem; }
th, td { text-align:left; padding:6px 9px; border-bottom:1px solid var(--line); }
th { font-size:.66rem; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); }
td { font-family:var(--mono); }
tr:last-child td { border-bottom:none; }
.note { border-left:3px solid var(--accent); background:var(--panel); border-radius:8px;
  padding:13px 15px; margin-bottom:16px; }
.note p { margin:0; font-size:.86rem; max-width:72ch; color:var(--dim); }
.note p + p { margin-top:8px; }
code { font-family:var(--mono); font-size:.88em; }
footer { color:var(--faint); font-size:.76rem; max-width:72ch; margin-top:26px; }
@media (max-width: 520px) { .wrap { padding-top:18px; } h1 { font-size:1.3rem; } }
"""


def _num(value: float) -> str:
    """Format a coordinate so the same input always produces the same bytes."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fit(string: str, available: float, size: float) -> float:
    """Shrink a label until it fits the cap. LC(LA(RIGHT)) is wider than 1u."""
    width = len(string) * size * _ADVANCE
    if width > available and width > 0:
        size = max(_MIN_SIZE, size * available / width)
    return round(size, 2)


def _wrap(string: str, available: float, size: float) -> list[str]:
    """Break a label across lines so a long one stays inside its cap."""
    if len(string) * size * _ADVANCE <= available or " " not in string:
        return [string]
    per_line = max(1, int(available / (_WRAP_SIZE * _ADVANCE)))
    lines: list[str] = []
    for word in string.split(" "):
        if lines and len(lines[-1]) + 1 + len(word) <= per_line:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines[:_MAX_LINES]


def _text_block(
    css_class: str,
    centre_x: float,
    centre_y: float,
    string: str,
    available: float,
    size: float,
) -> str:
    """One or more centred lines of text, as a vertically centred block."""
    lines = _wrap(string, available, size)
    if len(lines) > 1:
        size = min(_fit(line, available, _WRAP_SIZE) for line in lines)
    else:
        size = _fit(lines[0], available, size)
    height = size * 1.15
    first = centre_y - (len(lines) - 1) * height / 2 + size * 0.36
    return "".join(
        f"<text class=\"{css_class}\" x=\"{_num(centre_x)}\" "
        f"y=\"{_num(round(first + index * height, 2))}\" "
        f"font-size=\"{_num(size)}\">{_esc(line)}</text>"
        for index, line in enumerate(lines)
    )


def _cap_face(
    key: pb.KeyPosition,
    main: str,
    hold: str,
    muted: bool,
) -> str:
    centre_x = key.x + key.width / 2
    centre_y = key.y + key.height / 2
    available = key.width - 18
    main_class = "main muted" if muted else "main"
    if hold:
        return (
            _text_block("hold", centre_x, centre_y - key.height * 0.21, hold, available - 4, 22)
            + _text_block(main_class, centre_x, centre_y + key.height * 0.17, main, available, 30)
        )
    return _text_block(main_class, centre_x, centre_y, main, available, 34 if muted else 30)


def _diff_face(
    key: pb.KeyPosition,
    before_str: str,
    after_str: str,
) -> str:
    centre_x = key.x + key.width / 2
    centre_y = key.y + key.height / 2
    available = key.width - 16
    return (
        _text_block("diff-old", centre_x, centre_y - key.height * 0.20,
                    f"− {before_str}", available - 2, 18)
        + _text_block("diff-new", centre_x, centre_y + key.height * 0.19,
                      f"+ {after_str}", available, 23)
    )


def _key_corners(key: pb.KeyPosition) -> list[tuple[float, float]]:
    corners = [
        (float(key.x), float(key.y)),
        (float(key.x + key.width), float(key.y)),
        (float(key.x), float(key.y + key.height)),
        (float(key.x + key.width), float(key.y + key.height)),
    ]
    if not key.rotation:
        return corners
    theta = math.radians(key.rotation / 100.0)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    rx = float(key.rotation_x)
    ry = float(key.rotation_y)
    return [
        (
            rx + (cx - rx) * cos_t - (cy - ry) * sin_t,
            ry + (cx - rx) * sin_t + (cy - ry) * cos_t,
        )
        for cx, cy in corners
    ]


def _viewbox(keys: Iterable[pb.KeyPosition]) -> str:
    key_list = list(keys)
    if not key_list:
        return "0 0 100 100"
    xs: list[float] = []
    ys: list[float] = []
    for key in key_list:
        for cx, cy in _key_corners(key):
            xs.append(cx)
            ys.append(cy)
    left = min(xs) - _PAD
    top = min(ys) - _PAD
    right = max(xs) + _PAD
    bottom = max(ys) + _PAD
    return f"{_num(left)} {_num(top)} {_num(right - left)} {_num(bottom - top)}"


def _key_open_tag(key: pb.KeyPosition, extra_class: str = "") -> str:
    cls = f"key {extra_class}".strip()
    attrs = f"class=\"{cls}\" data-pos=\"{key.position}\""
    if key.rotation:
        attrs += (
            f" transform=\"rotate({_num(key.rotation / 100)},"
            f"{_num(key.rotation_x)},{_num(key.rotation_y)})\""
        )
    return f"<g {attrs}>"


def _tooltip(binding: pb.KeyBinding) -> str:
    """Full argument details for a binding, used in native SVG `<title>` tooltips."""
    parts = [
        f"position {binding.position}",
        binding.decoded or binding.behavior_name,
        f"behavior: {binding.behavior_name} (id {binding.behavior_id})",
    ]
    for index, arg in enumerate(binding.arguments, start=1):
        kind = pb.argument_kind_name(arg.kind)
        name = arg.name or "unnamed"
        parts.append(
            f"param{index}: {arg.decoded or arg.raw} "
            f"[{name}, {kind}, raw 0x{arg.raw:08x} / {arg.raw}]"
        )
    if not binding.arguments:
        parts.append("no parameters reported")
    return "\n".join(parts)


def _key_group(
    key: pb.KeyPosition,
    binding: pb.KeyBinding | None,
    layer_names: Mapping[int, str],
    extra_class: str = "",
) -> str:
    cap = (
        _key_labels.key_label(binding, layer_names)
        if binding is not None
        else _key_labels.KeyLabel(main="?", raw_main="?", muted=True)
    )
    classes = "cap"
    if binding is not None and binding.behavior_name == "Transparent":
        classes = "cap trans"
    elif binding is not None and binding.behavior_name == "None":
        classes = "cap dead"

    out = [_key_open_tag(key, extra_class)]
    if binding is not None:
        out.append(f"<title>{_esc(_tooltip(binding))}</title>")
    out.append(
        f"<rect class=\"{classes}\" x=\"{_num(key.x + _GAP)}\" y=\"{_num(key.y + _GAP)}\" "
        f"width=\"{_num(key.width - _GAP * 2)}\" height=\"{_num(key.height - _GAP * 2)}\" "
        f"rx=\"11\"/>"
    )
    out.append(
        f"<text class=\"pos\" x=\"{_num(key.x + 13)}\" y=\"{_num(key.y + 25)}\">"
        f"{key.position}</text>"
    )
    out.append(f"<g class=\"lbl-friendly\">{_cap_face(key, cap.main, cap.hold, cap.muted)}</g>")
    out.append(
        f"<g class=\"lbl-raw\">{_cap_face(key, cap.raw_main or cap.main, cap.raw_hold, cap.muted)}</g>"
    )
    out.append("</g>")
    return "".join(out)


def _diff_key_group(
    key: pb.KeyPosition,
    target_binding: pb.KeyBinding | None,
    key_diff: keymap_diff.KeymapDiff.KeyDiff | None,
    target_layers: Mapping[int, str],
    baseline_layers: Mapping[int, str],
) -> str:
    if key_diff is None:
        return _key_group(key, target_binding, target_layers, "unchanged")

    tip_lines = [
        f"CHANGED — position {key.position}",
        f"before: {key_diff.before}",
        f"after:  {key_diff.after}",
    ]
    if target_binding is not None:
        tip_lines.append(
            f"behavior: {target_binding.behavior_name} (live id {key_diff.behavior_id})"
        )

    before_friendly = _key_labels.friendly_binding_summary(
        key_diff.before_binding, baseline_layers
    )
    effective_after = target_binding or key_diff.after_binding
    after_friendly = _key_labels.friendly_binding_summary(
        effective_after, target_layers
    )
    before_raw = _key_labels.short_raw_expr(key_diff.before)
    after_raw = _key_labels.short_raw_expr(key_diff.after)

    out = [
        _key_open_tag(key, "changed"),
        f"<title>{_esc(chr(10).join(tip_lines))}</title>",
        f"<rect class=\"cap changed\" x=\"{_num(key.x + _GAP)}\" y=\"{_num(key.y + _GAP)}\" "
        f"width=\"{_num(key.width - _GAP * 2)}\" height=\"{_num(key.height - _GAP * 2)}\" "
        f"rx=\"11\"/>",
        f"<text class=\"pos\" x=\"{_num(key.x + 13)}\" y=\"{_num(key.y + 25)}\">"
        f"{key.position}</text>",
        f"<circle class=\"diff-dot\" cx=\"{_num(key.x + key.width - 16)}\" "
        f"cy=\"{_num(key.y + 18)}\" r=\"4.5\"/>",
        f"<g class=\"lbl-friendly\">{_diff_face(key, before_friendly, after_friendly)}</g>",
        f"<g class=\"lbl-raw\">{_diff_face(key, before_raw, after_raw)}</g>",
        "</g>",
    ]
    return "".join(out)


class KeymapRenderer:
    """Deterministic text, SVG, diff SVG, and standalone HTML presenter for a `KeymapSnapshot`."""

    FORMATS: typing.ClassVar[tuple[str, ...]] = ("html", "svg", "text")
    SVG_STYLE: typing.ClassVar[str] = _SVG_STYLE

    def __init__(self, snapshot: pb.KeymapSnapshot) -> None:
        self.snapshot = snapshot
        self.layer_names: Mapping[int, str] = snapshot.layer_names
        self._layers_by_id: Mapping[int, pb.Layer] = MappingProxyType(
            {layer.id: layer for layer in snapshot.layers}
        )
        self._bindings_by_layer: Mapping[int, Mapping[int, pb.KeyBinding]] = MappingProxyType(
            {
                layer.id: MappingProxyType({b.position: b for b in layer.bindings})
                for layer in snapshot.layers
            }
        )
        self._viewbox = _viewbox(snapshot.layout.keys)

    def binding_summary(self, binding: pb.KeyBinding) -> str:
        """Compact one-line friendly summary of `binding` using this snapshot's layer names."""
        return _key_labels.friendly_binding_summary(binding, self.layer_names)

    def key_diff_summary(
        self,
        key_diff: keymap_diff.KeymapDiff.KeyDiff,
        diff: keymap_diff.KeymapDiff,
    ) -> tuple[str, str]:
        """Return `(before_friendly, after_friendly)` for `key_diff`."""
        baseline_layers = diff.baseline_layer_names or self.layer_names
        before_friendly = _key_labels.friendly_binding_summary(
            key_diff.before_binding, baseline_layers
        )
        target_binding = (
            self._bindings_by_layer.get(key_diff.layer_id, {}).get(key_diff.position)
            or key_diff.after_binding
        )
        after_friendly = _key_labels.friendly_binding_summary(
            target_binding, self.layer_names
        )
        return before_friendly, after_friendly

    def _select_layer(self, layer_id: int | None = None) -> pb.Layer:
        """Select `layer_id`, or default to the first layer in firmware display order."""
        layers = list(self.snapshot.layers)
        if not layers:
            raise ValueError("snapshot has no layers")
        if layer_id is None:
            return layers[0]
        chosen = self._layers_by_id.get(layer_id)
        if chosen is None:
            known = ", ".join(str(layer.id) for layer in layers)
            raise ValueError(f"no layer with id {layer_id}; this snapshot has {known}")
        return chosen

    def render(self, fmt: str, *, layer_id: int | None = None) -> str:
        """Render in `fmt` (`'html'`, `'svg'`, or `'text'`)."""
        if fmt == "text":
            return self.to_text()
        if fmt == "html":
            return self.to_html()
        if fmt == "svg":
            return self.to_svg(layer_id)
        raise ValueError(f"unsupported format {fmt!r}; expected one of {self.FORMATS}")

    def to_text(self) -> str:
        """Render the snapshot as readable text, one line per key position."""
        when = self.snapshot.captured_iso or "1970-01-01T00:00:00Z"
        lines = [f"{self.snapshot.device.name}  ({when})"]
        for layer in self.snapshot.layers:
            lines.append("")
            lines.append(f"=== layer {layer.id}: {layer.name} ===")
            for binding in layer.bindings:
                lines.append(f"  {binding.position:>3}  {binding.decoded}")
        return "\n".join(lines)

    def layer_svg(
        self,
        layer: pb.Layer,
        *,
        diff: keymap_diff.KeymapDiff | None = None,
        standalone: bool = False,
    ) -> str:
        """Render one layer's `<svg>` element, optionally highlighting `diff`."""
        bindings = {b.position: b for b in layer.bindings}
        if diff is None:
            groups = "".join(
                _key_group(key, bindings.get(key.position), self.layer_names)
                for key in self.snapshot.layout.keys
            )
            title = f"{self.snapshot.device.name} layer {layer.id}: {layer.name}"
        else:
            baseline_layers = diff.baseline_layer_names or self.layer_names
            layer_diffs = diff.for_layer(layer.id)
            groups = "".join(
                _diff_key_group(
                    key,
                    bindings.get(key.position),
                    layer_diffs.get(key.position),
                    self.layer_names,
                    baseline_layers,
                )
                for key in self.snapshot.layout.keys
            )
            title = (
                f"{self.snapshot.device.name} layer {layer.id}: {layer.name} "
                f"({len(layer_diffs)} changed)"
            )
        opening = (
            f"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"{self._viewbox}\" "
            f"role=\"img\" aria-label=\"{_esc(title)}\">"
        )
        style = f"<style>{_SVG_STYLE}</style>" if standalone else ""
        return f"{opening}{style}{groups}</svg>"

    def to_svg(
        self,
        layer_id: int | None = None,
        *,
        diff: keymap_diff.KeymapDiff | None = None,
    ) -> str:
        """One layer (optionally with `diff` overlay) as a standalone SVG document."""
        chosen = self._select_layer(layer_id)
        body = self.layer_svg(chosen, diff=diff, standalone=True)
        return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{body}\n"

    def _pane(self, layer: pb.Layer, index: int) -> str:
        lines = "\n".join(
            f"{b.position:>3}  {_esc(b.decoded or b.behavior_name)}"
            for b in layer.bindings
        )
        return (
            f"<section class=\"pane\" id=\"pane-{index}\" aria-label=\"{_esc(layer.name)}\">"
            f"<h2 class=\"pane-title\">{_esc(layer.name)}<span>layer id {layer.id}</span></h2>"
            f"<div class=\"board\">{self.layer_svg(layer)}</div>"
            f"<ul class=\"legend\">"
            f"<li><i class=\"sw-tap\"></i>tap / press</li>"
            f"<li><i class=\"sw-hold\"></i>held (mod-tap, layer-tap)</li>"
            f"<li><i class=\"sw-trans\"></i><code>&amp;trans</code> &mdash; falls through</li>"
            f"<li><code>{_GLYPH_NONE}</code> <code>&amp;none</code> &mdash; dead</li>"
            f"<li>hover a key for its raw parameters</li>"
            f"</ul>"
            f"<div class=\"card\"><h2>Layer {layer.id}: {_esc(layer.name)} as text</h2>"
            f"<pre>{lines}</pre></div>"
            f"</section>"
        )

    def to_html(self) -> str:
        """Render the whole snapshot as one self-contained, zero-JS HTML file."""
        snap = self.snapshot
        layers = list(snap.layers)
        when = snap.captured_iso or "1970-01-01T00:00:00Z"

        radios = "".join(
            f"<input class=\"tab-radio\" type=\"radio\" name=\"layer\" id=\"tab-{i}\""
            f"{' checked' if i == 0 else ''}>"
            for i in range(len(layers))
        )
        controls = (
            "<input class=\"ctl-check\" type=\"checkbox\" id=\"ctl-raw\">"
            "<input class=\"ctl-check\" type=\"checkbox\" id=\"ctl-all\">"
        )
        tabs = "".join(
            f"<label for=\"tab-{i}\">{_esc(layer.name)}"
            f"<span class=\"lid\">id {layer.id}</span></label>"
            for i, layer in enumerate(layers)
        )
        toggles = (
            "<div class=\"view-toggles\">"
            "<label for=\"ctl-raw\">Raw ZMK</label>"
            "<label for=\"ctl-all\">All layers</label>"
            "</div>"
        )
        panes = "".join(self._pane(layer, i) for i, layer in enumerate(layers))

        rules = "".join(
            f"#tab-{i}:checked ~ main #pane-{i}{{display:block}}"
            f"#tab-{i}:checked ~ .tabs label[for=\"tab-{i}\"]"
            f"{{background:var(--accent);border-color:var(--accent);color:#fff}}"
            f"#tab-{i}:checked ~ .tabs label[for=\"tab-{i}\"] .lid"
            f"{{color:rgba(255,255,255,.72)}}"
            f"#tab-{i}:focus-visible ~ .tabs label[for=\"tab-{i}\"]"
            f"{{outline:2px solid var(--accent);outline-offset:2px}}"
            for i in range(len(layers))
        )

        facts = [
            ("device", snap.device.name),
            ("id", snap.device.id),
            ("captured", when),
            ("layers", str(len(layers))),
            ("keys", str(len(snap.layout.keys))),
            ("layout", snap.layout.name),
            ("behaviors", str(len(snap.behaviors))),
            ("tool", f"v{snap.metadata.tool_version}"),
        ]
        meta = "".join(
            f"<li>{_esc(k)} <b>{_esc(v)}</b></li>" for k, v in facts
        )

        behaviors = "".join(
            f"<tr><td>{b.id}</td><td>{_esc(b.display_name)}</td>"
            f"<td>{len(b.parameter_sets)}</td></tr>"
            for b in snap.behaviors
        )

        revision = snap.metadata.studio_messages_revision or "unknown"
        title = f"{snap.device.name} keymap — {when[:10]}"

        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_PAGE_STYLE}{_SVG_STYLE}{rules}</style>
</head>
<body>
<div class="wrap">
<h1>{_esc(snap.device.name)} keymap</h1>
<p class="lede">Every binding as the keyboard itself reported it over the ZMK Studio RPC,
read on {_esc(when[:10])}. Not parsed from firmware source.</p>
<ul class="meta">{meta}</ul>
{radios}{controls}
<nav class="tabs">{tabs}</nav>
{toggles}
<main>{panes}</main>
<div class="note">
<p><b>Not in this snapshot: the encoders.</b> The Studio RPC exposes no sensor bindings,
so no tool built on this protocol can capture what a rotary encoder does. Encoder
bindings live only in firmware source. A snapshot restores keys, not encoders.</p>
<p>A key drawn with two values shows the held value above the tapped one. The keyboard
does not report which parameter slot is the hold, so that is this page&rsquo;s drawing
convention and not something the firmware said.</p>
</div>
<div class="card">
<h2>Behavior table &mdash; {len(snap.behaviors)} entries</h2>
<table><tr><th>id</th><th>reported name</th><th>parameter sets</th></tr>{behaviors}</table>
</div>
<footer>Key geometry is the physical layout the keyboard reports about itself, rotations
included; nothing here is hand-drawn. Decoded through ZMK Studio message definitions
pinned at <code>{_esc(revision[:12])}</code>. Generated by
<code>keymap-render</code> v{_esc(snap.metadata.tool_version)} &mdash; this file
is self-contained and needs no network.</footer>
</div>
</body>
</html>
"""
