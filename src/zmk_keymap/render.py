"""Turn a snapshot into something you can read: text, SVG, or a standalone page.

Nothing here touches a keyboard. Neither `render` nor `model` imports any callable
or session object from `zmk_studio_client`, so there is no call path from a renderer
to `connect()` or any transport - checked by `tests/test_isolation.py`.

Read that claim as a call-path guarantee, not a `sys.modules` one: importing any
`zmk_keymap` submodule executes `zmk_keymap/__init__.py` (which imports the client
to offer `dump()`), and `keymap_snapshot_pb2.py` imports
`zmk_studio_client.proto.keyboard_pb2` (which executes
`zmk_studio_client/__init__.py`). So `zmk_studio_client`'s read package is loaded
into `sys.modules`, while its write surface (`zmk_studio_client.writable` /
`zmk_studio_client.writes`) and the third-party OS transport drivers (`serial`,
`objc`, `Foundation`, `CoreBluetooth`, lazy-imported inside
`zmk_studio_client.connection` only when opening a device) are not, and no
function here can call into it.

Every function is pure: a snapshot in, a string out, byte-stable for a given
input. That is what lets the output be diffed and golden-tested, so it matters
that nothing here reads the clock, the environment, or a set's iteration order.

## Why there is no JavaScript in the page

The board is drawn by this module, in Python, as SVG. The page switches layers
with radio inputs and CSS sibling selectors, and shows a binding's raw
parameters through SVG `<title>`, which browsers render as a tooltip natively.

So every decision about what a key says is made here, where it is testable,
rather than in script that only runs in a browser. The page works from a
`file://` URL with no server, no network and no fonts to fetch, and each layer's
SVG is independently viewable on its own.

The cost is that the page cannot open a different snapshot than the one it was
built from. Re-running the command is cheaper than owning a file-picker.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import html as _html

from . import model as pb

__all__ = ["text", "svg", "page", "FORMATS"]

#: Geometry is in hundredths of a key unit, used directly as SVG user units.
_GAP = 4          # trimmed from each side of a key, so a 1u cap is 92 wide
_PAD = 34         # viewBox padding, enough to clear the rotated thumb cluster

#: Monospace advance width as a fraction of font size, for shrink-to-fit. Close
#: enough across the system stack below; being a little conservative only means
#: a long label is slightly smaller than it had to be.
_ADVANCE = 0.60

#: Floor for shrink-to-fit, and the size a wrapped label is set at.
_MIN_SIZE = 12.0
_WRAP_SIZE = 16.0
_MAX_LINES = 3

_GLYPH_TRANS = "▽"   # ▽ falls through to the layer below
_GLYPH_NONE = "✕"    # ✕ dead

_KIND_NAMES: dict[int, str] = {
    pb.PARAMETER_KIND_UNSPECIFIED: "unspecified",
    pb.PARAMETER_KIND_CONSTANT: "constant",
    pb.PARAMETER_KIND_RANGE: "range",
    pb.PARAMETER_KIND_HID_USAGE: "HID usage",
    pb.PARAMETER_KIND_LAYER_ID: "layer id",
    pb.PARAMETER_KIND_UNDECLARED: "undeclared",
}


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

def text(snapshot: pb.KeymapSnapshot) -> str:
    """Render a snapshot as readable text, one line per key position.

    Shows what Studio's own display cannot: both halves of a mod-tap, and the
    difference between a dead key and a transparent one.
    """
    when = snapshot.capture.captured_at.ToDatetime().isoformat()
    lines = [f"{snapshot.device.name}  ({when}Z)"]
    for layer in snapshot.layers:
        lines.append("")
        lines.append(f"=== layer {layer.id}: {layer.name} ===")
        for binding in layer.bindings:
            lines.append(f"  {binding.position:>3}  {binding.rendered}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #

class _Label:
    """What one key cap says: an optional held value above a main value."""

    __slots__ = ("hold", "main", "muted")

    def __init__(self, main: str, hold: str = "", muted: bool = False) -> None:
        self.main = main
        self.hold = hold
        self.muted = muted


def _slot(param: pb.RenderedParam) -> str:
    """One parameter slot as it should appear on a cap."""
    shown = param.decoded or str(param.raw)
    # A layer index on its own is just a number; "L5" reads as a layer.
    return f"L{shown}" if param.kind == pb.PARAMETER_KIND_LAYER_ID else shown


def label(binding: pb.Binding) -> _Label:
    """Derive a key cap from a binding.

    Driven by the parameter metadata the keyboard reported, not by behaviour
    names - the same principle as `decode`. The two exceptions are the two
    behaviours that have no parameters to show and for which a glyph carries
    more than their name would.

    A two-parameter behaviour is drawn as held-above-tapped. That is a drawing
    convention, not a claim: the snapshot deliberately does not record which
    slot is the hold, because the keyboard never says. For `&mt` and `&lt` the
    convention is right, and it is the only arrangement that shows both halves
    at once, which is the whole reason this is not a screenshot.
    """
    if binding.behavior_name == "Transparent":
        return _Label(_GLYPH_TRANS, muted=True)
    if binding.behavior_name == "None":
        return _Label(_GLYPH_NONE, muted=True)

    params = list(binding.params)
    if not params:
        # e.g. "&caps_word", "&sys_reset" - the rendered form minus its sigil.
        return _Label((binding.rendered or binding.behavior_name).lstrip("&"))
    if len(params) == 1:
        return _Label(_slot(params[0]))
    return _Label(_slot(params[1]), hold=_slot(params[0]))


def tooltip(binding: pb.Binding) -> str:
    """The full truth about a binding, for the browser's own tooltip."""
    parts = [f"position {binding.position}", binding.rendered or binding.behavior_name,
             f"behavior: {binding.behavior_name} (id {binding.behavior_id})"]
    for index, param in enumerate(binding.params, start=1):
        kind = _KIND_NAMES.get(param.kind, "unspecified")
        name = param.name or "unnamed"
        parts.append(
            f"param{index}: {param.decoded or param.raw} "
            f"[{name}, {kind}, raw 0x{param.raw:08x} / {param.raw}]"
        )
    if not binding.params:
        parts.append("no parameters reported")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# svg
# --------------------------------------------------------------------------- #

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
    """Break a label across lines so a long one stays inside its cap.

    Shrinking alone is not enough. The keyboard reports some parameters as whole
    phrases - "Clear Selected Profile", "Toggle On/Off" - and scaling those to fit
    a 1u cap on one line leaves them unreadable or, below the floor, overflowing
    into the next key. Wrapping on spaces keeps them legible.

    A single word is never broken: a mangled `Saturation` is worse than a small
    one, and `_fit` will shrink it instead.
    """
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


def _text_block(css_class: str, centre_x: float, centre_y: float, string: str,
                available: float, size: float) -> str:
    """One or more centred lines of text, as a vertically centred block."""
    lines = _wrap(string, available, size)
    if len(lines) > 1:
        size = min(_fit(line, available, _WRAP_SIZE) for line in lines)
    else:
        size = _fit(lines[0], available, size)
    height = size * 1.15
    # 0.36em lifts the block so it is optically centred rather than sitting on
    # the midline, which is where a single baseline would put it.
    first = centre_y - (len(lines) - 1) * height / 2 + size * 0.36
    return "".join(
        f"<text class=\"{css_class}\" x=\"{_num(centre_x)}\" "
        f"y=\"{_num(round(first + index * height, 2))}\" "
        f"font-size=\"{_num(size)}\">{_html.escape(line)}</text>"
        for index, line in enumerate(lines)
    )


def _viewbox(keys: Iterable[pb.KeyPosition]) -> str:
    key_list = list(keys)
    if not key_list:
        return "0 0 100 100"
    left = min(k.x for k in key_list) - _PAD
    top = min(k.y for k in key_list) - _PAD
    right = max(k.x + k.width for k in key_list) + _PAD
    bottom = max(k.y + k.height for k in key_list) + _PAD
    return f"{_num(left)} {_num(top)} {_num(right - left)} {_num(bottom - top)}"


def _key_group(key: pb.KeyPosition, binding: pb.Binding | None) -> str:
    cap = label(binding) if binding is not None else _Label("?", muted=True)
    classes = "cap"
    if binding is not None and binding.behavior_name == "Transparent":
        classes = "cap trans"
    elif binding is not None and binding.behavior_name == "None":
        classes = "cap dead"

    opens = "<g class=\"key\">"
    if key.rotation:
        opens = (f"<g class=\"key\" transform=\"rotate({_num(key.rotation / 100)},"
                 f"{_num(key.rotation_x)},{_num(key.rotation_y)})\">")

    out = [opens]
    if binding is not None:
        out.append(f"<title>{_html.escape(tooltip(binding))}</title>")
    out.append(
        f"<rect class=\"{classes}\" x=\"{_num(key.x + _GAP)}\" y=\"{_num(key.y + _GAP)}\" "
        f"width=\"{_num(key.width - _GAP * 2)}\" height=\"{_num(key.height - _GAP * 2)}\" "
        f"rx=\"11\"/>"
    )
    out.append(
        f"<text class=\"pos\" x=\"{_num(key.x + 13)}\" y=\"{_num(key.y + 25)}\">"
        f"{key.position}</text>"
    )

    centre_x = key.x + key.width / 2
    centre_y = key.y + key.height / 2
    available = key.width - 18

    main_class = "main muted" if cap.muted else "main"
    if cap.hold:
        # Held above tapped, each block centred in its own half of the cap.
        out.append(_text_block("hold", centre_x, centre_y - key.height * 0.21,
                               cap.hold, available - 4, 23))
        out.append(_text_block(main_class, centre_x, centre_y + key.height * 0.16,
                               cap.main, available, 31))
    else:
        out.append(_text_block(main_class, centre_x, centre_y, cap.main,
                               available, 34 if cap.muted else 31))
    out.append("</g>")
    return "".join(out)


_SVG_STYLE = """
.key text { text-anchor: middle; font-family: ui-monospace, SFMono-Regular, Menlo,
  Consolas, "DejaVu Sans Mono", monospace; }
.cap { fill: #ebebe8; stroke: #c3c5cf; stroke-width: 3; }
.cap.trans { fill: none; stroke: #d9dae0; stroke-dasharray: 7 7; }
.cap.dead { fill: none; stroke: #d9dae0; stroke-width: 2; }
.main { fill: #17191c; font-weight: 500; }
.main.muted { fill: #9aa0b0; font-weight: 400; }
.hold { fill: #a65c26; font-weight: 600; }
.pos { fill: #9aa0b0; font-size: 15px; opacity: .55; text-anchor: start; }
@media (prefers-color-scheme: dark) {
  .cap { fill: #23262e; stroke: #3a3f4c; }
  .cap.trans, .cap.dead { fill: none; stroke: #2b2f39; }
  .main { fill: #e8eaef; }
  .main.muted, .pos { fill: #636a7c; }
  .hold { fill: #e0a26a; }
}
"""


def _board(snapshot: pb.KeymapSnapshot, layer: pb.Layer, standalone: bool) -> str:
    bindings = {b.position: b for b in layer.bindings}
    groups = "".join(
        _key_group(key, bindings.get(key.position)) for key in snapshot.layout.keys
    )
    title = f"{snapshot.device.name} layer {layer.id}: {layer.name}"
    opening = (
        f"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"{_viewbox(snapshot.layout.keys)}\" "
        f"role=\"img\" aria-label=\"{_html.escape(title)}\">"
    )
    style = f"<style>{_SVG_STYLE}</style>" if standalone else ""
    return f"{opening}{style}{groups}</svg>"


def svg(snapshot: pb.KeymapSnapshot, layer_id: int | None = None) -> str:
    """One layer as a standalone SVG document.

    Defaults to the first layer in the keyboard's own display order, which is
    not necessarily id 0.
    """
    layers = list(snapshot.layers)
    if not layers:
        raise ValueError("snapshot has no layers")
    if layer_id is None:
        chosen = layers[0]
    else:
        matches = [layer for layer in layers if layer.id == layer_id]
        if not matches:
            known = ", ".join(str(layer.id) for layer in layers)
            raise ValueError(f"no layer with id {layer_id}; this snapshot has {known}")
        chosen = matches[0]
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{_board(snapshot, chosen, True)}\n"


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #

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
.wrap { max-width:1180px; margin:0 auto; padding:26px 16px 60px; }
h1 { font-size:1.6rem; margin:0 0 4px; letter-spacing:-.02em; }
.lede { margin:0 0 16px; color:var(--dim); font-size:.92rem; max-width:66ch; }
.meta { display:flex; flex-wrap:wrap; gap:6px 9px; font-family:var(--mono);
  font-size:.72rem; color:var(--dim); margin:0 0 22px; padding:0; list-style:none; }
.meta li { background:var(--panel); border:1px solid var(--line); border-radius:999px;
  padding:3px 10px; white-space:nowrap; }
.meta b { color:var(--ink); font-weight:600; }
.tab-radio { position:absolute; width:1px; height:1px; opacity:0; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }
.tabs label { font-size:.82rem; font-weight:600; background:var(--panel); color:var(--dim);
  border:1px solid var(--line); border-radius:7px; padding:7px 12px; cursor:pointer;
  display:flex; align-items:baseline; gap:7px; }
.tabs label .lid { font-family:var(--mono); font-size:.68rem; color:var(--faint);
  font-weight:500; }
.tabs label:hover { color:var(--ink); border-color:var(--line2); }
.pane { display:none; }
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


def _pane(snapshot: pb.KeymapSnapshot, layer: pb.Layer, index: int) -> str:
    lines = "\n".join(
        f"{b.position:>3}  {_html.escape(b.rendered or b.behavior_name)}"
        for b in layer.bindings
    )
    return (
        f"<section class=\"pane\" id=\"pane-{index}\" aria-label=\"{_html.escape(layer.name)}\">"
        f"<div class=\"board\">{_board(snapshot, layer, False)}</div>"
        f"<ul class=\"legend\">"
        f"<li><i class=\"sw-tap\"></i>tap / press</li>"
        f"<li><i class=\"sw-hold\"></i>held (mod-tap, layer-tap)</li>"
        f"<li><i class=\"sw-trans\"></i><code>&amp;trans</code> &mdash; falls through</li>"
        f"<li><code>{_GLYPH_NONE}</code> <code>&amp;none</code> &mdash; dead</li>"
        f"<li>hover a key for its raw parameters</li>"
        f"</ul>"
        f"<div class=\"card\"><h2>Layer {layer.id}: {_html.escape(layer.name)} as text</h2>"
        f"<pre>{lines}</pre></div>"
        f"</section>"
    )


def page(snapshot: pb.KeymapSnapshot) -> str:
    """The whole snapshot as one self-contained HTML file.

    No JavaScript, no network: the layer tabs are radio inputs driven by CSS,
    the fonts are whatever the system has, and a key's raw parameters are an
    SVG `<title>` the browser shows by itself. Opens from a `file://` URL.
    """
    layers = list(snapshot.layers)
    when = snapshot.capture.captured_at.ToDatetime().isoformat() + "Z"

    radios = "".join(
        f"<input class=\"tab-radio\" type=\"radio\" name=\"layer\" id=\"tab-{i}\""
        f"{' checked' if i == 0 else ''}>"
        for i in range(len(layers))
    )
    tabs = "".join(
        f"<label for=\"tab-{i}\">{_html.escape(layer.name)}"
        f"<span class=\"lid\">id {layer.id}</span></label>"
        for i, layer in enumerate(layers)
    )
    panes = "".join(_pane(snapshot, layer, i) for i, layer in enumerate(layers))

    # The CSS that makes the radios into tabs, generated per layer so the file
    # stands alone with no script and no fixed layer count.
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
        ("device", snapshot.device.name),
        ("id", snapshot.device.id),
        ("captured", when),
        ("layers", str(len(layers))),
        ("keys", str(len(snapshot.layout.keys))),
        ("layout", snapshot.layout.name),
        ("behaviors", str(len(snapshot.behaviors))),
        ("tool", f"v{snapshot.capture.tool_version}"),
    ]
    meta = "".join(
        f"<li>{_html.escape(k)} <b>{_html.escape(v)}</b></li>" for k, v in facts
    )

    behaviors = "".join(
        f"<tr><td>{b.id}</td><td>{_html.escape(b.display_name)}</td>"
        f"<td>{len(b.parameter_sets)}</td></tr>"
        for b in snapshot.behaviors
    )

    revision = snapshot.capture.studio_messages_revision or "unknown"
    title = f"{snapshot.device.name} keymap — {when[:10]}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
<style>{_PAGE_STYLE}{_SVG_STYLE}{rules}</style>
</head>
<body>
<div class="wrap">
<h1>{_html.escape(snapshot.device.name)} keymap</h1>
<p class="lede">Every binding as the keyboard itself reported it over the ZMK Studio RPC,
read on {_html.escape(when[:10])}. Not parsed from firmware source.</p>
<ul class="meta">{meta}</ul>
{radios}
<nav class="tabs">{tabs}</nav>
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
<h2>Behavior table &mdash; {len(snapshot.behaviors)} entries</h2>
<table><tr><th>id</th><th>reported name</th><th>parameter sets</th></tr>{behaviors}</table>
</div>
<footer>Key geometry is the physical layout the keyboard reports about itself, rotations
included; nothing here is hand-drawn. Decoded through ZMK Studio message definitions
pinned at <code>{_html.escape(revision[:12])}</code>. Generated by
<code>keymap-render</code> v{_html.escape(snapshot.capture.tool_version)} &mdash; this file
is self-contained and needs no network.</footer>
</div>
</body>
</html>
"""


#: Format name -> renderer taking a snapshot and returning a string.
FORMATS: dict[str, Callable[[pb.KeymapSnapshot], str]] = {
    "text": text,
    "html": page,
    "svg": svg,
}
