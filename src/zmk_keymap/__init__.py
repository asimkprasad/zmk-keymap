"""Read a ZMK keyboard's live keymap.

ZMK Studio can edit a keymap but cannot export one, so a layout built up through
Studio exists only in the keyboard's settings partition with no backup anywhere.
This reads it back out over the same RPC Studio uses.

    from zmk_keymap import dump, from_json, to_json
    snapshot = dump()                 # a zmk_keymap.model.KeymapSnapshot
    open("keymap.json", "w", encoding="utf-8").write(to_json(snapshot))

The capture path only ever reads. See `zmk_studio_client.READS`.

**This module deliberately imports neither `restore` nor `zmk_studio_client.writable`.**
Keeping the write path off the package surface ensures `keymap-backup` and read-only
callers cannot reach a mutating request through a re-exported name; enforced by
`tests/test_isolation.py`.
"""
from __future__ import annotations

from . import model, snapshot as _snapshot
from .model import from_json, to_json  # re-exported: see __all__
from .render import page, svg, text  # re-exported: see __all__
from zmk_studio_client import (  # re-exported: see __all__
    READS,
    KeyboardLocked,
    NotReachable,
    RpcError,
    WriteAttempted,
    connect,
)

__all__ = [
    "model",
    "dump",
    "to_json",
    "from_json",
    "text",
    "svg",
    "page",
    "describe",
    "NotReachable",
    "WriteAttempted",
    "KeyboardLocked",
    "RpcError",
    "READS",
]

#: Kept as the name the CLI and callers already use. `render.text` is canonical.
describe = text


def dump(include_serial: bool = False) -> model.KeymapSnapshot:
    """Read the keyboard and return a KeymapSnapshot.

    Connecting, choosing a transport and closing it are handled internally; callers
    get the snapshot or an exception.

    The hardware serial number is not needed to restore a keymap and a snapshot is
    likely to be committed somewhere public, so the device is identified by a digest
    of the serial instead. Pass include_serial=True to record the raw value as well.
    """
    keyboard = connect()
    try:
        info = keyboard.device_info()
        keymap = keyboard.keymap()
        layouts = keyboard.physical_layouts()
        behaviors = keyboard.behaviors()
    finally:
        keyboard.close()
    return _snapshot.build(info, keymap, layouts, behaviors, include_serial)
