"""Internal ZMK binding parameter decoding owned by `zmk_keymap.keymap_snapshot`.

The keyboard reports each binding as a behavior id and two 32-bit parameters. A
parameter packs a modifier mask, a HID usage page and a usage into one integer:

    0x08_07_0006  ->  mods=LGUI  page=keyboard  usage=C   (Cmd+C)

What those parameters *mean* depends on the behavior, and the keyboard is the
authority on that. Each behavior reports parameter metadata saying how many slots
it takes and what kind of value each holds, so this module asks the metadata rather
than hardcoding one branch per behavior. Key Press, Mod-Tap and Layer-Tap need no
special cases; nor does a keymap-defined behavior nobody here has heard of.

Constructs protobuf `KeyBinding` and `BehaviorArgument` messages directly so there
are no intermediate wrapper dataclasses.
"""
from __future__ import annotations

from collections.abc import Iterable
import typing

from ..proto.keymap_snapshot_pb2 import (
    ARGUMENT_KIND_CONSTANT,
    ARGUMENT_KIND_HID_USAGE,
    ARGUMENT_KIND_LAYER_ID,
    ARGUMENT_KIND_RANGE,
    ARGUMENT_KIND_UNDECLARED,
    ArgumentKind,
    BehaviorArgument,
    KeyBinding,
)

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
    """Decode one packed HID usage parameter, e.g. ``LG(C)`` or ``RET``."""
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


class _BehaviorLike(typing.Protocol):
    @property
    def display_name(self) -> str: ...
    @property
    def parameter_sets(self) -> Iterable[typing.Any]: ...


def _admits(
    described: Iterable[typing.Any], value: int
) -> tuple[ArgumentKind | None, typing.Any]:
    """Which declared value does `value` satisfy, if any? -> (kind, description).

    Constants are checked before the open-ended kinds because they are exact: a
    behavior that declares both a constant and a range would otherwise report the
    range for a value the constant names.
    """
    items = list(described)
    for description in items:
        kind = description.WhichOneof("kind")
        if kind == "constant" and description.constant == value:
            return ARGUMENT_KIND_CONSTANT, description
    for description in items:
        kind = description.WhichOneof("kind")
        if kind == "range" and description.range.min <= value <= description.range.max:
            return ARGUMENT_KIND_RANGE, description
        if kind == "hid_usage":
            return ARGUMENT_KIND_HID_USAGE, description
        if kind == "layer_id":
            return ARGUMENT_KIND_LAYER_ID, description
    return None, None


def _decode_slot(kind: ArgumentKind, description: typing.Any, raw: int) -> str:
    """Decode one slot's raw integer according to the kind the keyboard declared."""
    if kind == ARGUMENT_KIND_CONSTANT:
        return str(description.name)
    if kind == ARGUMENT_KIND_HID_USAGE:
        return keycode(raw)
    return str(raw)


def _match_set(
    parameter_set: typing.Any, param1: int, param2: int
) -> list[BehaviorArgument] | None:
    """Decode both slots against one declared parameter set, or return None."""
    out: list[BehaviorArgument] = []
    for described, raw in (
        (parameter_set.param1, param1),
        (parameter_set.param2, param2),
    ):
        if not described:
            if raw:
                return None
            continue
        kind, description = _admits(described, raw)
        if kind is None:
            return None
        out.append(
            BehaviorArgument(
                name=str(description.name),
                raw=raw,
                decoded=_decode_slot(kind, description, raw),
                kind=kind,
            )
        )
    return out


def _undeclared(param1: int, param2: int) -> list[BehaviorArgument]:
    """Arguments for a behavior the keyboard described nothing about."""
    raw = [param1, param2]
    while raw and not raw[-1]:
        raw.pop()
    return [
        BehaviorArgument(
            name="",
            raw=value,
            decoded=str(value),
            kind=ARGUMENT_KIND_UNDECLARED,
        )
        for value in raw
    ]


def decode_binding(
    behavior: _BehaviorLike,
    param1: int,
    param2: int,
    *,
    position: int = 0,
    behavior_id: int | None = None,
) -> KeyBinding:
    """Construct a decoded protobuf `KeyBinding` directly from `behavior` and raw parameters."""
    p1, p2 = int(param1 or 0), int(param2 or 0)

    arguments: list[BehaviorArgument] | None = None
    for parameter_set in behavior.parameter_sets:
        arguments = _match_set(parameter_set, p1, p2)
        if arguments is not None:
            break
    if arguments is None:
        arguments = _undeclared(p1, p2)

    name = behavior.display_name
    sigil = _SIGILS.get(name)
    expr = " ".join([f"&{sigil}" if sigil else name] + [arg.decoded for arg in arguments])
    decoded_expr = expr if sigil else f"<{expr}>"
    resolved_id = (
        int(behavior_id)
        if behavior_id is not None
        else int(getattr(behavior, "id", 0) or 0)
    )
    return KeyBinding(
        position=position,
        behavior_id=resolved_id,
        behavior_name=name,
        param1=p1,
        param2=p2,
        decoded=decoded_expr,
        arguments=arguments,
    )
