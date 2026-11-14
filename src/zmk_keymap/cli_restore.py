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

from .model import KeymapSnapshot, from_json
from .restore import Proposal, RestoreFailed, RestoreRefused, apply, plan
from zmk_studio_client import KeyboardLocked, NotReachable, RpcError
from zmk_studio_client.writable import EditAbandoned, WritableKeyboard, connect_writable

#: Where the pre-flight capture goes when no directory is given. Beside the snapshot
#: being restored, so the undo sits next to the thing that needed undoing.
_DEFAULT_BACKUP_DIR = "restore-backups"


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or ""
        if enc.lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _load(path: pathlib.Path) -> KeymapSnapshot:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc.strerror}")
    try:
        return from_json(raw)
    except ValueError as exc:
        raise SystemExit(f"{path} is {exc}")


def _report(proposal: Proposal) -> None:
    """Print the proposal the way someone deciding whether to run it would want it."""
    if proposal.identity_warning:
        print(f"warning: {proposal.identity_warning}", file=sys.stderr)
    if not proposal.remapping.is_identity:
        print(f"note: the keyboard's behavior ids differ from the snapshot's; "
              f"{len(proposal.remapping.ids)} behaviors were matched by name",
              file=sys.stderr)
    if proposal.remapping.unused_and_missing:
        print(f"note: the keyboard lacks "
              f"{', '.join(proposal.remapping.unused_and_missing)}, which the "
              f"snapshot carries but never binds", file=sys.stderr)

    compared = proposal.changes.compared
    if proposal.is_empty:
        print(f"{proposal.device_name}: already matches ({compared} keys compared)")
        return
    print(f"{proposal.device_name}: {len(proposal.changes)} of {compared} keys "
          f"would change")
    for change in proposal.changes.changes:
        print(f"  {change}")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="keymap-restore",
        description="Write a saved snapshot's bindings back to the keyboard. "
                    "Shows what would change and exits unless --commit is given.")
    parser.add_argument("snapshot", type=pathlib.Path,
                        help="a JSON snapshot written by keymap-backup")
    parser.add_argument("--commit", action="store_true",
                        help="actually write the changes (default: show them only)")
    parser.add_argument("--backup-dir", type=pathlib.Path,
                        help=f"where to write the pre-flight capture "
                             f"(default: {_DEFAULT_BACKUP_DIR}/ beside the snapshot)")
    parser.add_argument("--force-different-device", action="store_true",
                        help="proceed even if the keyboard is not the one the "
                             "snapshot came from")
    args = parser.parse_args(argv)

    want = _load(args.snapshot)
    if not want.layers:
        raise SystemExit(f"{args.snapshot} has no layers to restore")

    backup_dir: pathlib.Path = (
        args.backup_dir or args.snapshot.resolve().parent / _DEFAULT_BACKUP_DIR
    )

    try:
        session = connect_writable()
    except NotReachable as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    in_flight = False
    exit_code = 1
    try:
        exit_code = _run_session(session, want, args, backup_dir)
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


def _run_session(
    session: WritableKeyboard,
    want: KeymapSnapshot,
    args: argparse.Namespace,
    backup_dir: pathlib.Path,
) -> int:
    try:
        proposal = plan(session, want)
    except (RestoreRefused, KeyboardLocked, RpcError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    _report(proposal)

    if proposal.identity_warning and not args.force_different_device:
        print("refused: this is a different keyboard. Pass "
              "--force-different-device if that is what you meant.",
              file=sys.stderr)
        return 1

    if proposal.is_empty:
        return 0

    if not args.commit:
        print("\nnothing was written. Re-run with --commit to apply.")
        return 3

    try:
        safety = apply(session, want, proposal, backup_dir)
    except RestoreRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except RestoreFailed as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2

    print(f"\nwrote {len(proposal.changes)} binding(s) and saved them.")
    print(f"previous keymap: {safety}")
    print(f"to undo: keymap-restore {safety} --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
