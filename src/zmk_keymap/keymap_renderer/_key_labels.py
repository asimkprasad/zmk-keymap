"""Internal keycap label formatting owned by `zmk_keymap.renderer`.

`decoder.py` turns packed 32-bit parameters into ZMK devicetree tokens (`LG(C)`,
`BSPC`, `LS(LBKT)`, `&bt Select Profile 0`). Those are exact, diffable, and the
right thing to show in text dumps - and too verbose for a 1u keycap, where
`LC(LS(LG(N4)))` shrinks to near-illegible type while `⌃⇧⌘4` reads at full size.

This private helper maps each `Binding` to both forms (`KeyLabel`) for `KeymapRenderer`.
Nothing here touches `zmk_studio_client`'s runtime; it reads `keymap_snapshot.Binding` only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re

from .. import keymap_snapshot as pb

__all__ = [
    "GLYPH_NONE",
    "GLYPH_TRANS",
    "KeyLabel",
    "friendly_binding_summary",
    "friendly_keycode",
    "key_label",
    "short_raw_expr",
]

GLYPH_TRANS = "▽"   # falls through to the layer below
GLYPH_NONE = "✕"    # dead key

_MOD_WRAPPER = re.compile(r"^([LR][CSAG])\((.*)\)$")

#: Canonical modifier glyph display order (Ctrl, Alt/Option, Shift, GUI/Cmd).
_MOD_ORDER: tuple[tuple[str, str], ...] = (
    ("C", "⌃"),
    ("A", "⌥"),
    ("S", "⇧"),
    ("G", "⌘"),
)

#: Unshifted HID key names -> compact human-friendly keycap glyphs/labels.
_KEY_GLYPHS: dict[str, str] = {
    "N1": "1", "N2": "2", "N3": "3", "N4": "4", "N5": "5",
    "N6": "6", "N7": "7", "N8": "8", "N9": "9", "N0": "0",
    "RET": "⏎",
    "ESC": "Esc",
    "BSPC": "⌫",
    "TAB": "⇥",
    "SPACE": "␣",
    "DEL": "⌦",
    "CAPS": "⇪",
    "INS": "Ins",
    "HOME": "Home",
    "END": "End",
    "PG_UP": "PgUp",
    "PG_DN": "PgDn",
    "PSCRN": "PrtSc",
    "SLCK": "ScrLk",
    "PAUSE_BREAK": "Pause",
    "K_APP": "Menu",
    "UP": "↑",
    "DOWN": "↓",
    "LEFT": "←",
    "RIGHT": "→",
    "MINUS": "-",
    "EQUAL": "=",
    "LBKT": "[",
    "RBKT": "]",
    "BSLH": "\\",
    "SEMI": ";",
    "SQT": "'",
    "GRAVE": "`",
    "COMMA": ",",
    "DOT": ".",
    "FSLH": "/",
    "KP_DOT": ".",
    "KP_COMMA": ",",
    "LCTRL": "⌃", "RCTRL": "⌃",
    "LSHFT": "⇧", "RSHFT": "⇧",
    "LALT": "⌥", "RALT": "⌥",
    "LGUI": "⌘", "RGUI": "⌘",
    "C_VOL_UP": "Vol +",
    "C_VOL_DN": "Vol −",
    "C_MUTE": "Mute",
    "C_PP": "Play",
    "C_NEXT": "Next",
    "C_PREV": "Prev",
    "C_STOP": "Stop",
    "C_AC_HOME": "Home",
    "C_AC_BACK": "Back",
    "C_AC_FORWARD": "Fwd",
}

#: US-layout shifted symbols, applied ONLY when Shift is the sole modifier.
#: (`LS(N4)` is `$`, whereas `LC(LS(LG(N4)))` is `⌃⇧⌘4`.)
_SHIFTED_KEYS: dict[str, str] = {
    "N1": "!", "N2": "@", "N3": "#", "N4": "$", "N5": "%",
    "N6": "^", "N7": "&", "N8": "*", "N9": "(", "N0": ")",
    "MINUS": "_",
    "EQUAL": "+",
    "LBKT": "{",
    "RBKT": "}",
    "BSLH": "|",
    "SEMI": ":",
    "SQT": '"',
    "GRAVE": "~",
    "COMMA": "<",
    "DOT": ">",
    "FSLH": "?",
}

_ZERO_PARAM_FRIENDLY: dict[str, str] = {
    "Caps Word": "Caps Wd",
    "Key Repeat": "Repeat",
    "Bootloader": "Boot",
    "Reset": "Reset",
    "Studio Unlock": "Unlock",
}

_BT_COMMANDS: dict[str, str] = {
    "Select Profile": "BT",
    "Clear All Profiles": "BT Clr All",
    "Clear Selected Profile": "BT Clr",
    "Next Profile": "BT Next",
    "Previous Profile": "BT Prev",
    "Disconnect Profile": "BT Disc",
}

_OUT_COMMANDS: dict[str, str] = {
    "Toggle Outputs": "Out Tog",
    "Select USB": "Out USB",
    "Select BLE": "Out BLE",
}

_RGB_COMMANDS: dict[str, str] = {
    "Toggle On/Off": "RGB Tog",
    "On": "RGB On",
    "Off": "RGB Off",
    "Next Effect": "RGB Eff+",
    "Previous Effect": "RGB Eff−",
    "Hue Up": "RGB Hue+",
    "Hue Down": "RGB Hue−",
    "Saturation Up": "RGB Sat+",
    "Saturation Down": "RGB Sat−",
    "Brightness Up": "RGB Bri+",
    "Brightness Down": "RGB Bri−",
    "Speed Up": "RGB Spd+",
    "Speed Down": "RGB Spd−",
}


#: Maximum nested ZMK modifier wrappers (`LC(LS(LA(LG(...))))`) unwrapped per token.
_MAX_MOD_DEPTH = 8


@dataclass(frozen=True)
class KeyLabel:
    """Both friendly and raw ZMK labels for one keycap face."""

    main: str
    hold: str = ""
    raw_main: str = ""
    raw_hold: str = ""
    muted: bool = False

    @property
    def summary(self) -> str:
        """Compact single-line friendly summary (`hold / main` or `main`)."""
        return f"{self.hold} / {self.main}" if self.hold else self.main


def friendly_keycode(decoded: str) -> str:
    """Turn a decoded ZMK HID token like ``LC(LS(LG(N4)))`` into ``⌃⇧⌘4``."""
    mods: set[str] = set()
    inner = decoded.strip()
    for _ in range(_MAX_MOD_DEPTH):
        match = _MOD_WRAPPER.match(inner)
        if not match:
            break
        mod_token, rest = match.groups()
        mods.add(mod_token[1])  # C, S, A, or G
        inner = rest

    if mods == {"S"} and inner in _SHIFTED_KEYS:
        return _SHIFTED_KEYS[inner]

    base = _KEY_GLYPHS.get(inner, inner)
    if not mods:
        return base

    prefix = "".join(glyph for code, glyph in _MOD_ORDER if code in mods)
    return f"{prefix}{base}"


def _raw_slot(arg: pb.BehaviorArgument) -> str:
    shown = arg.decoded or str(arg.raw)
    return f"L{shown}" if arg.kind == pb.ARGUMENT_KIND_LAYER_ID else shown


def _friendly_layer_name(arg: pb.BehaviorArgument, layers: Mapping[int, str] | None) -> str:
    if layers is not None:
        name = layers.get(int(arg.raw), "").strip()
        if name:
            return name
    shown = arg.decoded or str(arg.raw)
    return f"L{shown}"


def _friendly_slot(arg: pb.BehaviorArgument, layers: Mapping[int, str] | None) -> str:
    if arg.kind == pb.ARGUMENT_KIND_LAYER_ID:
        return _friendly_layer_name(arg, layers)
    shown = arg.decoded or str(arg.raw)
    if arg.kind == pb.ARGUMENT_KIND_HID_USAGE:
        return friendly_keycode(shown)
    return shown


def _raw_pair(binding: pb.KeyBinding) -> tuple[str, str]:
    """Return `(raw_main, raw_hold)` using exact ZMK argument tokens."""
    args = list(binding.arguments)
    if not args:
        bare = (binding.decoded or binding.behavior_name).lstrip("&")
        return bare, ""
    if len(args) == 1:
        return _raw_slot(args[0]), ""
    return _raw_slot(args[1]), _raw_slot(args[0])


def key_label(
    binding: pb.KeyBinding,
    layers: Mapping[int, str] | None = None,
) -> KeyLabel:
    """Derive both friendly and raw ZMK labels for `binding`."""
    bname = binding.behavior_name
    if bname == "Transparent":
        return KeyLabel(main=GLYPH_TRANS, raw_main=GLYPH_TRANS, muted=True)
    if bname == "None":
        return KeyLabel(main=GLYPH_NONE, raw_main=GLYPH_NONE, muted=True)

    raw_main, raw_hold = _raw_pair(binding)
    args = list(binding.arguments)

    # Zero-argument behaviors (&sys_reset, &caps_word, <hyper>, ...)
    if not args:
        if bname in _ZERO_PARAM_FRIENDLY:
            return KeyLabel(main=_ZERO_PARAM_FRIENDLY[bname], raw_main=raw_main)
        cleaned = raw_main.strip("<>").strip() or raw_main
        return KeyLabel(main=cleaned, raw_main=raw_main)

    # Command-style behaviors whose arguments are not hold-vs-tap
    cmd_raw = (binding.decoded or binding.behavior_name).lstrip("&")
    if bname == "Bluetooth":
        cmd = args[0].decoded or str(args[0].raw)
        short = _BT_COMMANDS.get(cmd, f"BT {cmd}")
        if len(args) >= 2:
            arg = args[1].decoded or str(args[1].raw)
            return KeyLabel(main=f"{short} {arg}", raw_main=cmd_raw)
        return KeyLabel(main=short, raw_main=cmd_raw)

    if bname == "Output Selection":
        cmd = args[0].decoded or str(args[0].raw)
        return KeyLabel(
            main=_OUT_COMMANDS.get(cmd, f"Out {cmd}"),
            raw_main=cmd_raw,
        )

    if bname == "Underglow":
        cmd = args[0].decoded or str(args[0].raw)
        short = _RGB_COMMANDS.get(cmd, f"RGB {cmd}")
        if len(args) >= 2:
            arg = args[1].decoded or str(args[1].raw)
            return KeyLabel(main=f"{short} {arg}", raw_main=cmd_raw)
        return KeyLabel(main=short, raw_main=cmd_raw)

    if bname == "Backlight":
        cmd = args[0].decoded or str(args[0].raw)
        if len(args) >= 2:
            arg = args[1].decoded or str(args[1].raw)
            return KeyLabel(main=f"BL {cmd} {arg}", raw_main=cmd_raw)
        return KeyLabel(main=f"BL {cmd}", raw_main=cmd_raw)

    if bname == "External Power":
        arg = args[0].decoded or str(args[0].raw)
        label = {"0": "Ext Off", "1": "Ext On", "2": "Ext Tog"}.get(arg, f"Ext {arg}")
        return KeyLabel(main=label, raw_main=cmd_raw)

    # Dedicated 1-argument layer transitions
    if len(args) == 1 and args[0].kind == pb.ARGUMENT_KIND_LAYER_ID:
        target = _friendly_layer_name(args[0], layers)
        if bname == "To Layer":
            return KeyLabel(main=f"→ {target}", raw_main=raw_main)
        if bname == "Toggle Layer":
            return KeyLabel(main=f"Tog {target}", raw_main=raw_main)
        if bname == "Sticky Layer":
            return KeyLabel(main=f"1× {target}", raw_main=raw_main)
        if bname == "Momentary Layer":
            return KeyLabel(main=target, hold="Hold", raw_main=raw_main)
        return KeyLabel(main=target, raw_main=raw_main)

    if len(args) == 1:
        main = _friendly_slot(args[0], layers)
        if bname == "Sticky Key":
            return KeyLabel(main=f"1× {main}", raw_main=raw_main)
        return KeyLabel(main=main, raw_main=raw_main)

    # Two-argument hold-tap style behaviors (&mt, &lt, etc.)
    return KeyLabel(
        main=_friendly_slot(args[1], layers),
        hold=_friendly_slot(args[0], layers),
        raw_main=raw_main,
        raw_hold=raw_hold,
    )


def friendly_binding_summary(
    binding: pb.KeyBinding,
    layers: Mapping[int, str] | None = None,
) -> str:
    """Compact one-line friendly summary of a binding (for diffs and tables)."""
    return key_label(binding, layers).summary


def short_raw_expr(expr: str) -> str:
    """Compact raw ZMK expression for keycap diff faces (strips leading `&kp ` / `&`)."""
    bare = expr.strip()
    if bare.startswith("&kp "):
        return bare[4:]
    if bare == "&trans":
        return GLYPH_TRANS
    if bare == "&none":
        return GLYPH_NONE
    return bare.lstrip("&")
