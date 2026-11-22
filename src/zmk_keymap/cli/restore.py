"""Command line entry point: write a snapshot's bindings back to the keyboard.

The only command in this package that changes anything. Two deliberate frictions:

    keymap-restore snapshot.json            shows what would change, sends no writes
    keymap-restore snapshot.json --commit   actually does it

The dry run is the default because a restore is not reversible once committed, and
because a diff nobody reads is not a safeguard. Running without --commit is a normal
thing to do and costs a few seconds of reads.

Exit codes are meant for scripting: 0 nothing to do or applied cleanly, 1 refused
before any write, 2 failed after writing began (read the message - it says what state
the keyboard is in), 3 changes are pending and were shown but not applied.
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import pathlib
import sys

from ._common import CliError, load_snapshot_arg, utf8_stdio
from ..keymap_snapshot import KeymapSnapshot
from ..live_keyboard import InvalidLayout, UnsavedChanges, read_keyboard
from ..restore_plan import RestoreFailed, RestorePlan, RestoreRefused
from ..snapshot_store import SnapshotStore
from zmk_studio_client import Keyboard, KeyboardLocked, NotReachable, RpcError, connect
from zmk_studio_client.writable import EditAbandoned, WritableKeyboard, connect_writable

#: Default undo snapshot directory name (at the enclosing git root when present,
#: else beside the snapshot being restored).
_DEFAULT_UNDO_DIR = "undo-snapshots"


def _default_undo_dir(snapshot_path: pathlib.Path) -> pathlib.Path:
    parent = snapshot_path.resolve().parent
    git_root = SnapshotStore.enclosing_git_root(parent)
    return (git_root or parent) / _DEFAULT_UNDO_DIR


def _report(plan: RestorePlan) -> None:
    """Print the plan the way someone deciding whether to run it would want it."""
    if plan.identity_warning:
        print(f"warning: {plan.identity_warning}", file=sys.stderr)
    if not plan.diff.translator.is_identity:
        print(f"note: the keyboard's behavior ids differ from the snapshot's; "
              f"{plan.diff.translator.matched_count} behaviors were matched by name",
              file=sys.stderr)
    if plan.diff.translator.unused_missing:
        print(f"note: the keyboard lacks "
              f"{', '.join(plan.diff.translator.unused_missing)}, which the "
              f"snapshot carries but never binds", file=sys.stderr)

    compared = plan.diff.compared
    if plan.is_empty:
        print(f"{plan.device_name}: already matches ({compared} keys compared)")
        return
    print(f"{plan.device_name}: {len(plan.diff)} of {compared} keys "
          f"would change")
    for key_diff in plan.diff:
        print(f"  {key_diff}")


def main(argv: Sequence[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="keymap-restore",
        description="Write a saved snapshot's bindings back to the keyboard. "
                    "Shows what would change and exits unless --commit is given.")
    parser.add_argument("snapshot", type=pathlib.Path,
                        help="a JSON snapshot written by keymap-backup")
    parser.add_argument("--commit", action="store_true",
                        help="actually write the changes (default: show them only)")
    parser.add_argument("--undo-dir", type=pathlib.Path,
                        help=f"where to write the pre-flight undo snapshot "
                             f"(default: {_DEFAULT_UNDO_DIR}/ in the enclosing "
                             f"repository, else beside the snapshot)")
    parser.add_argument("--force-different-device", action="store_true",
                        help="proceed even if the keyboard is not the one the "
                             "snapshot came from")
    args = parser.parse_args(argv)

    try:
        want = load_snapshot_arg(args.snapshot)
    except CliError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if not want.layers:
        print(f"refused: {args.snapshot} has no layers to restore", file=sys.stderr)
        return 1

    undo_dir: pathlib.Path = args.undo_dir or _default_undo_dir(args.snapshot)

    if not args.commit:
        try:
            ro_session = connect()
        except NotReachable as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        try:
            return _run_dry_run(ro_session, want, args)
        finally:
            ro_session.close()

    try:
        session = connect_writable()
    except NotReachable as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    in_flight = False
    exit_code = 1
    try:
        exit_code = _run_session(session, want, args, undo_dir)
    except BaseException:
        in_flight = True
        raise
    finally:
        try:
            session.close()
        except EditAbandoned as exc:
            # Should be unreachable: apply() settles its edit on every path. If it
            # ever is reached, printing "failed:" while exiting 0 would mislead any
            # caller checking the exit status, so upgrade to exit code 2 without
            # swallowing an in-flight exception. Nothing was committed - close()
            # discarded the pending edits.
            print(f"failed: {exc}", file=sys.stderr)
            if not in_flight:
                exit_code = 2
    return exit_code


def _plan_and_check(
    session: Keyboard,
    want: KeymapSnapshot,
    args: argparse.Namespace,
) -> tuple[RestorePlan | None, int]:
    try:
        live = read_keyboard(session, require_saved=True)
        plan = RestorePlan(live=live, target=want)
    except (
        RestoreRefused,
        UnsavedChanges,
        InvalidLayout,
        KeyboardLocked,
        RpcError,
    ) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return None, 1

    _report(plan)

    if plan.identity_warning and not args.force_different_device:
        print("refused: this is a different keyboard. Pass "
              "--force-different-device if that is what you meant.",
              file=sys.stderr)
        return None, 1

    return plan, 0


def _run_dry_run(
    session: Keyboard,
    want: KeymapSnapshot,
    args: argparse.Namespace,
) -> int:
    plan, status = _plan_and_check(session, want, args)
    if plan is None:
        return status
    if plan.is_empty:
        return 0
    print("\nnothing was written. Re-run with --commit to apply.")
    return 3


def _run_session(
    session: WritableKeyboard,
    want: KeymapSnapshot,
    args: argparse.Namespace,
    undo_dir: pathlib.Path,
) -> int:
    plan, status = _plan_and_check(session, want, args)
    if plan is None:
        return status
    if plan.is_empty:
        return 0

    try:
        undo_path = plan.apply(
            session,
            lambda current: SnapshotStore.write_undo_snapshot(undo_dir, current),
        )
    except RestoreRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except RestoreFailed as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2

    print(f"\nwrote {len(plan.diff)} binding(s) and saved them.")
    print(f"undo snapshot: {undo_path}")
    print(f"to undo: keymap-restore {undo_path} --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
