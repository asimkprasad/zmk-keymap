"""Turning raw bindings into something a person can read.

The keyboard reports each binding as a behavior id and two 32-bit parameters. A
parameter packs a modifier mask, a HID usage page and a usage into one integer:

    0x08_07_0006  ->  mods=LGUI  page=keyboard  usage=C   (Cmd+C)

What those parameters *mean* depends on the behavior, and the keyboard is the
authority on that. Each behavior reports parameter metadata saying how many slots
it takes and what kind of value each holds, so this module asks the metadata rather
than hardcoding one branch per behavior. Key Press, Mod-Tap and Layer-Tap need no
special cases; nor does a keymap-defined behavior nobody here has heard of.

One thing the protocol never reports is the devicetree label - it says
"Bluetooth", not `&bt`. _SIGILS is that translation and nothing else, which is why
it is a flat table of names rather than a branch with logic in it.

A behavior that reports no metadata, or whose metadata admits none of the values
actually bound, falls back to passing the raw integers through. That is honest
about not knowing, and it keeps one unrecognised binding from costing the dump.
"""
from __future__ import annotations

from collections.abc import Iterable
import dataclasses
import typing

#: bit mask -> the modifier-function spelling ZMK uses, e.g. LG(C) for Cmd+C.
_MODIFIERS = [
    (0x01, "LC"), (0x02, "LS"), (0x04, "LA"), (0x08, "LG"),
    (0x10, "RC"), (0x20, "RS"), (0x40, "RA"), (0x80, "RG"),
]

_KEYBOARD: dict[int, str] = {
    0x04: "A", 0x05: "B", 0x06: "C", 0x07: "D", 0x08: "E", 0x09: "F", 0x0A: "G",
    0x0B: "H", 0x0C: "I", 0x0D: "J", 0x0E: "K", 0x0F: "L", 0x10: "M", 0x11: "N",
    0x12: "O", 0x13: "P", 0x14: "Q", 0x15: "R", 0x16: "S", 0x17: "T", 0x18: "U",
    0x19: "V", 0x1A: "W", 0x1B: "X", 0x1C: "Y", 0x1D: "Z",
    0x1E: "N1", 0x1F: "N2", 0x20: "N3", 0x21: "N4", 0x22: "N5", 0x23: "N6",
    0x24: "N7", 0x25: "N8", 0x26: "N9", 0x27: "N0",
    0x28: "RET", 0x29: "ESC", 0x2A: "BSPC", 0x2B: "TAB", 0x2C: "SPACE",
    0x2D: "MINUS", 0x2E: "EQUAL", 0x2F: "LBKT", 0x30: "RBKT", 0x31: "BSLH",
    0x33: "SEMI", 0x34: "SQT", 0x35: "GRAVE", 0x36: "COMMA", 0x37: "DOT", 0x38: "FSLH",
    0x39: "CAPS", 0x46: "PSCRN", 0x47: "SLCK", 0x48: "PAUSE_BREAK",
    0x49: "INS", 0x4A: "HOME", 0x4B: "PG_UP", 0x4C: "DEL", 0x4D: "END", 0x4E: "PG_DN",
    0x4F: "RIGHT", 0x50: "LEFT", 0x51: "DOWN", 0x52: "UP",
    0x65: "K_APP", 0x63: "KP_DOT", 0x85: "KP_COMMA",
    0xE0: "LCTRL", 0xE1: "LSHFT", 0xE2: "LALT", 0xE3: "LGUI",
    0xE4: "RCTRL", 0xE5: "RSHFT", 0xE6: "RALT", 0xE7: "RGUI",
}
_KEYBOARD.update({0x3A + n: f"F{n + 1}" for n in range(12)})

_CONSUMER: dict[int, str] = {
    0xB5: "C_NEXT", 0xB6: "C_PREV", 0xB7: "C_STOP", 0xCD: "C_PP",
    0xE2: "C_MUTE", 0xE9: "C_VOL_UP", 0xEA: "C_VOL_DN",
    0x223: "C_AC_HOME", 0x224: "C_AC_BACK", 0x225: "C_AC_FORWARD",
}


def keycode(param: int) -> str:
    """Render one packed parameter, e.g. ``LG(C)`` or ``RET``."""
    param = int(param or 0)
    mods = (param >> 24) & 0xFF
    page = (param >> 16) & 0xFF
    usage = param & 0xFFFF

    if page == 0x07:
        name = _KEYBOARD.get(usage, f"0x{usage:02X}")
    elif page == 0x0C:
        name = _CONSUMER.get(usage, f"0x{usage:04X}")
    elif param == 0:
        return "-"
    else:
        return f"0x{param:08X}"

    for bit, fn in reversed(_MODIFIERS):
        if mods & bit:
            name = f"{fn}({name})"
    return name


#: Behavior display name -> devicetree label. The protocol reports the former and
#: a keymap is written in the latter, and nothing on the wire bridges them.
_SIGILS: dict[str, str] = {
    "Key Press": "kp",
    "Mod-Tap": "mt",
    "Layer-Tap": "lt",
    "Momentary Layer": "mo",
    "To Layer": "to",
    "Toggle Layer": "tog",
    "Sticky Layer": "sl",
    "Sticky Key": "sk",
    "Transparent": "trans",
    "None": "none",
    "Caps Word": "caps_word",
    "Key Repeat": "key_repeat",
    "Bootloader": "bootloader",
    "Reset": "sys_reset",
    "Bluetooth": "bt",
    "Output Selection": "out",
    "External Power": "ext_power",
    "Underglow": "rgb_ug",
    "Backlight": "bl",
    "Mouse Key Press": "mkp",
    "Studio Unlock": "studio_unlock",
}

#: Parameter kinds, spelled as ParameterKind in the snapshot schema.
CONSTANT, RANGE, HID_USAGE, LAYER_ID, UNDECLARED = (
    "constant", "range", "hid_usage", "layer_id", "undeclared")


@dataclasses.dataclass(frozen=True)
class Param:
    """One decoded parameter slot of one binding."""

    name: str
    raw: int
    decoded: str
    kind: str


@dataclasses.dataclass(frozen=True)
class Rendered:
    """A binding, decoded both as a line to read and as its separate slots."""

    text: str
    params: tuple[Param, ...]


class _BehaviorLike(typing.Protocol):
    @property
    def display_name(self) -> str: ...
    @property
    def parameter_sets(self) -> Iterable[typing.Any]: ...


def _admits(described: Iterable[typing.Any], value: int) -> tuple[str | None, typing.Any]:
    """Which declared value does `value` satisfy, if any? -> (kind, description).

    Constants are checked before the open-ended kinds because they are exact: a
    behavior that declares both a constant and a range would otherwise report the
    range for a value the constant names.
    """
    items = list(described)
    for description in items:
        kind = description.WhichOneof("kind")
        if kind == "constant" and description.constant == value:
            return CONSTANT, description
    for description in items:
        kind = description.WhichOneof("kind")
        if kind == "range" and description.range.min <= value <= description.range.max:
            return RANGE, description
        if kind in (HID_USAGE, LAYER_ID):
            return kind, description
    return None, None


def _decode(kind: str, description: typing.Any, raw: int) -> str:
    """Render one slot's value according to the kind the keyboard declared."""
    if kind == CONSTANT:
        # The keyboard's label is all there is: it reports "Select Profile", never
        # the BT_SEL the keymap would be written with.
        return str(description.name)
    if kind == HID_USAGE:
        return keycode(raw)
    return str(raw)


def _match_set(parameter_set: typing.Any, param1: int, param2: int) -> tuple[Param, ...] | None:
    """Decode both slots against one declared parameter set, or return None.

    A behavior may declare several alternative sets - Underglow's colour commands
    take a value where its on/off commands take none - so a set that does not admit
    the values actually bound is not an error, it is the wrong set.
    """
    out: list[Param] = []
    for described, raw in ((parameter_set.param1, param1), (parameter_set.param2, param2)):
        if not described:
            # The set declares nothing for this slot. A value here means some other
            # set was meant; zero means the slot is genuinely unused.
            if raw:
                return None
            continue
        kind, description = _admits(described, raw)
        if kind is None:
            return None
        out.append(Param(str(description.name), raw, _decode(kind, description, raw), kind))
    return tuple(out)


def _undeclared(param1: int, param2: int) -> tuple[Param, ...]:
    """Slots for a behavior the keyboard described nothing about.

    Trailing zeros are dropped because arity is exactly what is unknown here, and a
    behavior taking one parameter is far more common than one taking two. A zero in
    the *first* slot is kept: the second being set proves the first is real.
    """
    raw = [param1, param2]
    while raw and not raw[-1]:
        raw.pop()
    return tuple(Param("", value, str(value), UNDECLARED) for value in raw)


def binding(behavior: _BehaviorLike, param1: int, param2: int) -> Rendered:
    """Render a binding, using what the keyboard said about its behavior.

    `behavior` is a snapshot Behavior: a display name plus the parameter metadata
    the keyboard reported for it.
    """
    p1, p2 = int(param1 or 0), int(param2 or 0)

    params: tuple[Param, ...] | None = None
    for parameter_set in behavior.parameter_sets:
        params = _match_set(parameter_set, p1, p2)
        if params is not None:
            break
    if params is None:
        params = _undeclared(p1, p2)

    name = behavior.display_name
    sigil = _SIGILS.get(name)
    rendered = " ".join([f"&{sigil}" if sigil else name] + [p.decoded for p in params])
    # A behavior with no known devicetree label is wrapped, so a line that is not
    # keymap syntax cannot be mistaken for a line that is.
    return Rendered(rendered if sigil else f"<{rendered}>", params)
