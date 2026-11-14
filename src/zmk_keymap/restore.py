"""Write a snapshot's bindings back to the keyboard it came from.

This is the only module in the package that changes a keyboard, and the shape of it
is dictated by one fact: there is no undo once `save_changes` lands. Everything here
exists to make the irreversible step the last one, and to make it reachable only
after every check has passed.

The sequence, in order, because the order is the design:

    1. read the live keymap, behaviors and layout          - nothing written
    2. refuse if the board already holds unsaved edits      - not ours to commit
    3. verify active physical layout matches the snapshot   - nothing written
    4. translate behavior ids by name (remap)               - nothing written
    5. compute the difference (diff)                        - nothing written
    6. STOP unless the caller explicitly asked to commit    - the default
    7. capture the current state to a file                  - the undo
    8. send one set_binding per changed key                 - reversible in RAM
    9. re-read and verify every change took                 - still reversible
   10. save_changes, or discard_changes on any doubt        - the point of no return

Steps 1-6 are what `plan()` does, and it is what runs by default. `apply()` is steps
7-10 and has to be asked for.

When a firmware is built with `CONFIG_ZMK_STUDIO_LOCKING=n`, the keyboard accepts
writes with no unlock gesture. Because there may be no hardware interlock, the checks
below are the whole safeguard - which is why the pre-flight capture is mandatory
rather than a flag.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import pathlib
import typing

from . import diff, model as pb, remap, snapshot as _snapshot
from zmk_studio_client import model as client_model, writable


class _HasSerial(typing.Protocol):
    @property
    def serial_number(self) -> bytes: ...


class RestoreRefused(Exception):
    """A precondition failed. Nothing was sent."""


class RestoreFailed(Exception):
    """Something went wrong after writing began. Says what state the keyboard is in."""


@dataclass(frozen=True)
class Proposal:
    """What a restore would do, computed without sending anything."""

    changes: diff.Plan
    remapping: remap.Remapping
    device_name: str

    #: Present when the snapshot names a different board than the one connected.
    identity_warning: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.changes.is_empty


def device_id(info: _HasSerial) -> str:
    """The snapshot's identifier for a keyboard: a short digest of its serial.

    Must match `snapshot.build` exactly, or every restore would warn about identity.
    Pinned by a test rather than by comment.
    """
    return hashlib.sha256(info.serial_number).hexdigest()[:12]


def _identity_warning(info: client_model.DeviceInfo, want: pb.Device) -> str | None:
    """Compare the connected board against the one the snapshot was taken from.

    Returns a message rather than raising: restoring one keyboard's keymap onto a
    second, identical one is a real thing to want, so the caller decides whether a
    mismatch is a mistake or the point.
    """
    live = device_id(info)
    if not want.id or not live or want.id == live:
        return None
    return (f"this snapshot came from {want.name!r} (id {want.id}), and the "
            f"connected keyboard is {info.name!r} (id {live})")


def _check_layout(
    want_layout: pb.PhysicalLayout,
    live_layouts: client_model.PhysicalLayouts,
) -> None:
    """Refuse when the connected keyboard's active layout disagrees with the snapshot.

    Checked against `PhysicalLayout` (name and key count) rather than the number of
    bindings on a layer, so a legitimately sparse snapshot remains valid while a
    42-key snapshot restored onto a 60-key layout - or ANSI onto ISO - is caught
    before any key position can be misaddressed.
    """
    if live_layouts.layouts and live_layouts.active_index >= len(live_layouts.layouts):
        raise RestoreRefused(
            f"the keyboard reports active layout {live_layouts.active_index} of "
            f"{len(live_layouts.layouts)}; refusing to guess which layout is active. "
            f"Nothing was written."
        )

    if not want_layout.name and not want_layout.keys:
        return

    if not live_layouts.layouts:
        described = f"{want_layout.name!r}" if want_layout.name else f"{len(want_layout.keys)} keys"
        raise RestoreRefused(
            f"the snapshot is for physical layout {described}, but the keyboard "
            f"reported no physical layouts. Nothing was written."
        )

    active = live_layouts.layouts[live_layouts.active_index]
    if want_layout.name and active.name != want_layout.name:
        offered = [one.name for one in live_layouts.layouts]
        hint = (
            " Switch to that layout in ZMK Studio first."
            if want_layout.name in offered
            else " This snapshot is from a different keyboard or layout."
        )
        raise RestoreRefused(
            f"the snapshot is for physical layout {want_layout.name!r}, but the "
            f"keyboard's active layout is {active.name!r}.{hint} Nothing was written."
        )

    if want_layout.keys and len(active.keys) != len(want_layout.keys):
        label = repr(active.name) if active.name else f"index {live_layouts.active_index}"
        raise RestoreRefused(
            f"the snapshot's physical layout has {len(want_layout.keys)} keys, but "
            f"the keyboard's active layout ({label}) has {len(active.keys)} keys. "
            f"Restoring across different key geometries would misplace bindings. "
            f"Nothing was written."
        )


def plan(session: writable.WritableKeyboard, want: pb.KeymapSnapshot) -> Proposal:
    """Work out what restoring `want` onto the connected keyboard would change.

    Sends only reads. Raises `RestoreRefused` before writing anything if the keyboard
    is holding unsaved Studio edits, uses an incompatible physical layout, lacks a
    bound behavior, or has layers that do not match the snapshot.
    """
    if session.unsaved_changes():
        raise RestoreRefused(
            "the keyboard is holding unsaved changes, probably from an open ZMK "
            "Studio session. Save or discard them there first; this tool will not "
            "commit edits it did not make.")

    _check_layout(want.layout, session.physical_layouts())

    # Handed to `remap` and `diff` as the library returned it: the snapshot schema
    # imports `zmk_studio_client.v1.Behavior`, so a live behavior and a stored one
    # are the same type.
    behaviors = session.behaviors()
    try:
        mapping = remap.plan(want, list(behaviors.values()))
        changes = diff.plan(want, session.keymap(), mapping, behaviors)
    except (remap.BehaviorMissing, remap.BehaviorAmbiguous, diff.LayoutMismatch) as exc:
        raise RestoreRefused(str(exc)) from exc

    info = session.device_info()
    return Proposal(
        changes=changes,
        remapping=mapping,
        device_name=info.name,
        identity_warning=_identity_warning(info, want.device),
    )


def apply(
    session: writable.WritableKeyboard,
    want: pb.KeymapSnapshot,
    proposal: Proposal,
    backup_dir: pathlib.Path,
) -> pathlib.Path:
    """Perform the restore. Irreversible at the final step.

    Returns the path of the pre-flight capture, which is the undo: restoring that file
    puts the keyboard back. Written before the first write and never conditional,
    because a restore with no way back is not a restore.

    On any failure after writing starts, `discard_changes` is sent and RestoreFailed
    is raised saying whether the rollback succeeded. Pending binding changes live in
    RAM until save_changes, so a discard genuinely undoes them.
    """
    if proposal.is_empty:
        raise RestoreRefused("nothing to do: the keyboard already matches the snapshot")

    safety = _capture(session, backup_dir)

    edit = session.edit()
    try:
        for change in proposal.changes.changes:
            edit.set_binding(
                layer_id=change.layer_id,
                position=change.position,
                behavior_id=change.behavior_id,
                param1=change.param1,
                param2=change.param2,
            )

        # Verify BEFORE committing, while discard still undoes everything.
        remaining = diff.plan(want, session.keymap(), proposal.remapping)
        if not remaining.is_empty:
            raise RestoreFailed(
                f"{len(remaining)} of {len(proposal.changes)} changes did not take "
                f"effect: {'; '.join(str(c) for c in remaining.changes[:3])}")
    except writable.BindingRejected as rejection:
        # Translated rather than re-raised: a caller of restore() should have to
        # know one exception type, not the transport's as well.
        _rollback(edit, rejection, safety)
        raise RestoreFailed(str(rejection)) from rejection
    except Exception as first:
        _rollback(edit, first, safety)
        raise

    try:
        edit.save()
    except writable.SaveRefused as refusal:
        _rollback(edit, None, safety)
        raise RestoreFailed(
            f"{refusal}. They were discarded; the keymap is unchanged. "
            f"Backup: {safety}") from refusal
    return safety


def _rollback(
    edit: writable.Edit,
    cause: BaseException | None,
    safety: pathlib.Path,
) -> None:
    """Discard pending writes, and say plainly if that failed too."""
    pending = edit.pending
    try:
        edit.discard()
    except Exception as second:
        raise RestoreFailed(
            f"{cause or 'the commit failed'}, and the rollback also failed "
            f"({second}). {pending} binding(s) may be pending on the keyboard; they "
            f"are in RAM, so power-cycling it discards them. Backup: {safety}"
        ) from cause


def _capture(session: writable.WritableKeyboard, backup_dir: pathlib.Path) -> pathlib.Path:
    """Snapshot the current keymap to a timestamped file. The undo for this restore."""
    current = _snapshot.build(
        session.device_info(), session.keymap(), session.physical_layouts(),
        session.behaviors(), include_serial=False)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"before-restore-{stamp}.json"
    path.write_text(pb.to_json(current), encoding="utf-8")
    return path
