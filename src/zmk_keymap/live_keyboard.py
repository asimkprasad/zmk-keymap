"""Read a connected ZMK keyboard's live keymap into a `KeymapSnapshot`.

ZMK Studio can edit a keymap on a running keyboard, but cannot export one, so a
layout built up through Studio exists only in the keyboard's settings partition.
This module reads it back out over the same RPC Studio uses and returns an
in-memory `KeymapSnapshot` (performing zero writes and zero filesystem I/O).

**This module deliberately never imports `restore_plan` or `zmk_studio_client.writable`.**
Keeping the write surface off the read module ensures `keymap-backup` and
`keymap-ui`'s read worker (`ui._hw_read`) cannot reach a mutating request;
enforced by `tests/test_isolation.py`.
"""
from __future__ import annotations

from collections.abc import Mapping
import datetime
import hashlib

from . import keymap_snapshot as pb
from zmk_studio_client import (
    STUDIO_MESSAGES_REVISION,
    Keyboard,
    connect,
    model as client_model,
)

__all__ = [
    "InvalidLayout",
    "UnsavedChanges",
    "read_keyboard",
]


class InvalidLayout(ValueError):
    """The keyboard reported an active physical layout index outside its own list."""


class UnsavedChanges(Exception):
    """The keyboard holds uncommitted edits in RAM (e.g. from an open ZMK Studio session)."""


def _device_id(info: client_model.DeviceInfo) -> str:
    """Stable 12-hex-char SHA-256 prefix over the raw hardware serial bytes."""
    return hashlib.sha256(info.serial_number).hexdigest()[:12]


def _snapshot_from_reads(
    info: client_model.DeviceInfo,
    keymap: client_model.Keymap,
    layouts: client_model.PhysicalLayouts,
    behaviors: Mapping[int, client_model.Behavior],
    *,
    include_serial: bool = False,
) -> pb.KeymapSnapshot:
    """Populate an immutable `KeymapSnapshot` from the four `Keyboard` reads."""
    if layouts.layouts and layouts.active_index >= len(layouts.layouts):
        raise InvalidLayout(
            f"keyboard reports active layout {layouts.active_index} of "
            f"{len(layouts.layouts)}; refusing to guess which layout the key "
            "positions belong to"
        )

    device = pb.Device(
        name=info.name,
        id=_device_id(info),
        serial_number=info.serial_number.hex() if include_serial else "",
    )

    behavior_entries: list[client_model.Behavior] = []
    for bid, details in sorted(behaviors.items()):
        entry = client_model.Behavior()
        entry.CopyFrom(details)
        entry.id = bid
        behavior_entries.append(entry)

    layers = [
        (
            layer.id,
            layer.name,
            [
                (position, bound.behavior_id, bound.param1, bound.param2)
                for position, bound in enumerate(layer.bindings)
            ],
        )
        for layer in keymap.layers
    ]

    layout: pb.PhysicalLayout | None = None
    if layouts.layouts:
        active = layouts.layouts[layouts.active_index]
        layout = pb.PhysicalLayout(
            name=active.name,
            active_index=layouts.active_index,
            available=[one.name for one in layouts.layouts],
            keys=[
                pb.KeyPosition(
                    position=position,
                    x=key.x,
                    y=key.y,
                    width=key.width,
                    height=key.height,
                    rotation=key.rotation,
                    rotation_x=key.rotation_x,
                    rotation_y=key.rotation_y,
                )
                for position, key in enumerate(active.keys)
            ],
        )

    return pb.KeymapSnapshot(
        device=device,
        layout=layout,
        behaviors=behavior_entries,
        layers=layers,
        captured_at=datetime.datetime.now(datetime.timezone.utc),
        studio_messages_revision=STUDIO_MESSAGES_REVISION,
    )


def read_keyboard(
    keyboard: Keyboard | None = None,
    *,
    include_serial: bool = False,
    require_saved: bool = False,
) -> pb.KeymapSnapshot:
    """Read a live ZMK keyboard into an immutable `KeymapSnapshot` (`0` writes).

    When `keyboard` is omitted, opens a read-only `connect()` session and closes it
    before returning. When an already-open `Keyboard` is supplied, reads from it and
    leaves the session open for the caller.

    Pass `require_saved=True` to raise `UnsavedChanges` immediately if the keyboard
    is holding uncommitted Studio edits in RAM.
    """
    if keyboard is None:
        opened = connect()
        try:
            return read_keyboard(
                opened,
                include_serial=include_serial,
                require_saved=require_saved,
            )
        finally:
            opened.close()

    if require_saved and keyboard.unsaved_changes():
        raise UnsavedChanges(
            "the keyboard is holding unsaved changes, probably from an open ZMK "
            "Studio session. Save or discard them there first; this tool will not "
            "commit edits it did not make."
        )

    layouts = keyboard.physical_layouts()
    if layouts.layouts and layouts.active_index >= len(layouts.layouts):
        raise InvalidLayout(
            f"keyboard reports active layout {layouts.active_index} of "
            f"{len(layouts.layouts)}; refusing to guess which layout the key "
            "positions belong to"
        )

    return _snapshot_from_reads(
        keyboard.device_info(),
        keyboard.keymap(),
        layouts,
        keyboard.behaviors(),
        include_serial=include_serial,
    )
