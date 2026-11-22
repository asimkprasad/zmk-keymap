"""Plan and commit restoring a `KeymapSnapshot`'s bindings onto a keyboard.

This is the only module in the package that changes a keyboard, and its shape is
dictated by one fact: there is no undo once `save_changes` lands. Everything here
exists to make the irreversible step the last one, and to make it reachable only
after every check has passed.

The sequence, in order, because the order is the design:

    1. refuse if the board already holds unsaved edits      - live_keyboard(require_saved=True)
    2. read the live keymap, behaviors and layout           - live_keyboard.read_keyboard()
    3. verify active physical layout matches the snapshot   - RestorePlan(live, target)
    4. translate behavior ids by name (BehaviorTranslator)  - RestorePlan(live, target)
    5. compute the difference (live.diff(target))           - RestorePlan(live, target)
    6. STOP unless the caller explicitly asked to commit    - the default
    7. re-read, verify no drift, and persist undo snapshot  - RestorePlan.apply()
    8. send one set_binding per changed key                 - reversible in RAM
    9. re-read and verify every change took                 - still reversible
   10. save_changes, or discard_changes on any doubt        - the point of no return

When firmware is built with `CONFIG_ZMK_STUDIO_LOCKING=n`, the keyboard accepts
writes with no unlock gesture. Because there may be no hardware interlock, the checks
below are the whole safeguard - which is why `save_undo` is mandatory rather than
an option.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import pathlib
import secrets

from . import keymap_diff, keymap_snapshot as pb, live_keyboard
from zmk_studio_client import writable

__all__ = [
    "RestoreFailed",
    "RestorePlan",
    "RestoreRefused",
]


class RestoreRefused(Exception):
    """A precondition failed. Nothing was sent."""


class RestoreFailed(Exception):
    """Something went wrong after writing began. Says what state the keyboard is in."""


def _check_layout(
    want_layout: pb.PhysicalLayout,
    live_layout: pb.PhysicalLayout,
) -> None:
    """Refuse when the baseline's active physical layout disagrees with `want_layout`.

    Checked against `PhysicalLayout` (name and key count) rather than the number of
    bindings on a layer, so a legitimately sparse snapshot remains valid while a
    42-key snapshot restored onto a 60-key layout - or ANSI onto ISO, or a snapshot
    whose `layout` was stripped - is caught before any key position can be misaddressed.
    """
    want_has_layout = bool(want_layout.name or want_layout.keys)
    live_has_layout = bool(
        live_layout.name or live_layout.keys or live_layout.available
    )
    if not want_has_layout and not live_has_layout:
        return

    if not want_has_layout and live_has_layout:
        live_desc = (
            f"{live_layout.name!r} ({len(live_layout.keys)} keys)"
            if live_layout.name
            else f"{len(live_layout.keys)} keys"
        )
        raise RestoreRefused(
            f"the snapshot carries no physical layout, while the keyboard's active "
            f"layout is {live_desc}; refusing to guess key geometry. Nothing was written."
        )

    if not live_has_layout:
        described = (
            f"{want_layout.name!r}"
            if want_layout.name
            else f"{len(want_layout.keys)} keys"
        )
        raise RestoreRefused(
            f"the snapshot is for physical layout {described}, but the keyboard "
            f"reported no physical layouts. Nothing was written."
        )

    if (want_layout.name or live_layout.name) and live_layout.name != want_layout.name:
        hint = (
            " Switch to that layout in ZMK Studio first."
            if want_layout.name and want_layout.name in live_layout.available
            else " This snapshot is from a different keyboard or layout."
        )
        raise RestoreRefused(
            f"the snapshot is for physical layout {want_layout.name!r}, but the "
            f"keyboard's active layout is {live_layout.name!r}.{hint} "
            f"Nothing was written."
        )

    if (want_layout.keys or live_layout.keys) and len(live_layout.keys) != len(
        want_layout.keys
    ):
        label = (
            repr(live_layout.name)
            if live_layout.name
            else f"index {live_layout.active_index}"
        )
        raise RestoreRefused(
            f"the snapshot's physical layout has {len(want_layout.keys)} keys, but "
            f"the keyboard's active layout ({label}) has {len(live_layout.keys)} keys. "
            f"Restoring across different key geometries would misplace bindings. "
            f"Nothing was written."
        )


@dataclass(frozen=True)
class RestorePlan:
    """Validated dry-run plan binding a `live` baseline snapshot to a `target` snapshot."""

    live: pb.KeymapSnapshot
    target: pb.KeymapSnapshot
    diff: keymap_diff.KeymapDiff = field(init=False)

    def __post_init__(self) -> None:
        _check_layout(self.target.layout, self.live.layout)
        try:
            computed = self.live.diff(self.target)
        except (
            keymap_diff.BehaviorMissing,
            keymap_diff.BehaviorAmbiguous,
            keymap_diff.LayoutMismatch,
        ) as exc:
            raise RestoreRefused(str(exc)) from exc
        object.__setattr__(self, "diff", computed)

    @property
    def device_name(self) -> str:
        """Name reported by the connected baseline keyboard."""
        return self.live.device.name

    @property
    def identity_warning(self) -> str | None:
        """Present when `target` omits `device.id` or names a different hardware digest than `live`."""
        want = self.target.device
        live = self.live.device
        if not live.id or want.id == live.id:
            return None
        if not want.id:
            return (
                f"this snapshot carries no device id (device {want.name or 'unnamed'!r}), "
                f"and the connected keyboard is {live.name!r} (id {live.id})"
            )
        return (
            f"this snapshot came from {want.name!r} (id {want.id}), and the "
            f"connected keyboard is {live.name!r} (id {live.id})"
        )

    @property
    def is_empty(self) -> bool:
        """True when the keyboard already matches `target` on every compared key."""
        return self.diff.is_empty

    def digest(self) -> str:
        """Deterministic SHA-256 commitment token over `target` and the planned writes."""
        canonical = {
            "target_sha256": self.target.sha256(),
            "device_name": self.device_name,
            "identity_warning": self.identity_warning,
            "compared": self.diff.compared,
            "remapping": sorted(self.diff.translator.ids.items()),
            "unused_and_missing": list(self.diff.translator.unused_missing),
            "changes": [
                [
                    kd.layer_id,
                    kd.position,
                    kd.before_binding.behavior_id,
                    kd.before_binding.param1,
                    kd.before_binding.param2,
                    kd.behavior_id,
                    kd.param1,
                    kd.param2,
                    kd.before,
                    kd.after,
                ]
                for kd in self.diff
            ],
        }
        encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()

    def apply(
        self,
        session: writable.WritableKeyboard,
        save_undo: Callable[[pb.KeymapSnapshot], pathlib.Path],
    ) -> pathlib.Path:
        """Perform the restore. Irreversible at the final step.

        Re-reads `session` (`require_saved=True`), verifies its state still matches
        this plan's `digest()`, and persists the undo snapshot through `save_undo`
        before sending the first write. On any failure after writing starts,
        `discard_changes` is sent and `RestoreFailed` is raised naming `undo_path`.
        """
        if self.is_empty:
            raise RestoreRefused(
                "nothing to do: the keyboard already matches the snapshot"
            )

        try:
            current = live_keyboard.read_keyboard(
                session, include_serial=False, require_saved=True
            )
        except (live_keyboard.UnsavedChanges, live_keyboard.InvalidLayout) as exc:
            raise RestoreRefused(str(exc)) from exc

        fresh = RestorePlan(live=current, target=self.target)
        if not secrets.compare_digest(fresh.digest(), self.digest()):
            raise RestoreRefused(
                "the keyboard state changed since the plan was computed; "
                "re-plan before applying. Nothing was written."
            )

        try:
            undo_path = save_undo(current)
        except RestoreRefused:
            raise
        except Exception as exc:
            raise RestoreRefused(f"{exc}. Nothing was written.") from exc

        edit = session.edit()
        try:
            for key_diff in self.diff:
                edit.set_binding(
                    layer_id=key_diff.layer_id,
                    position=key_diff.position,
                    behavior_id=key_diff.behavior_id,
                    param1=key_diff.param1,
                    param2=key_diff.param2,
                )

            # Verify BEFORE committing, while discard still undoes everything.
            remaining = live_keyboard.read_keyboard(
                session, include_serial=False
            ).diff(self.target)
            if not remaining.is_empty:
                raise RestoreFailed(
                    f"{len(remaining)} of {len(self.diff)} changes did not take "
                    f"effect: {'; '.join(str(kd) for kd in remaining.key_diffs[:3])}"
                )
        except writable.BindingRejected as rejection:
            _rollback(edit, rejection, undo_path)
            raise RestoreFailed(str(rejection)) from rejection
        except Exception as first:
            _rollback(edit, first, undo_path)
            raise

        try:
            edit.save()
        except writable.SaveRefused as refusal:
            _rollback(edit, None, undo_path)
            raise RestoreFailed(
                f"{refusal}. They were discarded; the keymap is unchanged. "
                f"Undo snapshot: {undo_path}"
            ) from refusal
        except Exception as exc:
            _rollback(edit, exc, undo_path)
            raise RestoreFailed(
                f"commit failed ({exc}); pending edits were discarded. "
                f"Undo snapshot: {undo_path}"
            ) from exc
        return undo_path


def _rollback(
    edit: writable.Edit,
    cause: BaseException | None,
    undo_path: pathlib.Path,
) -> None:
    """Discard pending writes, and say plainly if that failed too."""
    pending = edit.pending
    try:
        edit.discard()
    except Exception as second:
        raise RestoreFailed(
            f"{cause or 'the commit failed'}, and the rollback also failed "
            f"({second}). {pending} binding(s) may be pending on the keyboard; they "
            f"are in RAM, so power-cycling it discards them. Undo snapshot: {undo_path}"
        ) from cause
