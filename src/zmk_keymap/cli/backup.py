"""Command line entry point: read the keymap and write it out."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import pathlib
import sys

from ._common import utf8_stdio
from ..keymap_renderer import KeymapRenderer
from ..live_keyboard import InvalidLayout, read_keyboard
from ..snapshot_store import SnapshotStore, SnapshotStoreError
from zmk_studio_client import KeyboardLocked, NotReachable, RpcError


def _default_path() -> pathlib.Path:
    """snapshots/keymap.json in the enclosing repository, else the current directory.

    One file rather than a dated one: the output is deterministic apart from the capture
    time, so git already records what changed on the keyboard and when. Dated filenames
    would duplicate that as a pile of near-identical blobs.
    """
    git_root = SnapshotStore.enclosing_git_root()
    if git_root is not None:
        return git_root / "snapshots" / "keymap.json"
    return pathlib.Path("keymap.json")


def main(argv: Sequence[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="keymap-backup",
        description="Read a ZMK keyboard's live keymap. Never modifies the keyboard.",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=pathlib.Path,
        metavar="PATH",
        help="write JSON here (default: snapshots/keymap.json in the "
        "repository, overwritten so git holds the history)",
    )
    parser.add_argument(
        "-t",
        "--text",
        action="store_true",
        help="also print the keymap as readable text",
    )
    parser.add_argument(
        "--include-serial",
        action="store_true",
        help="record the hardware serial number as well as its digest; "
        "off by default since snapshots are often published",
    )
    args = parser.parse_args(argv)

    try:
        snapshot = read_keyboard(include_serial=args.include_serial)
    except (NotReachable, KeyboardLocked, RpcError, InvalidLayout) as exc:
        # Nothing is written on failure: the snapshot path holds the last good dump,
        # and overwriting it with a partial one is the worst thing this tool could do.
        print(f"{exc}", file=sys.stderr)
        return 1

    path: pathlib.Path = args.out or _default_path()
    try:
        SnapshotStore.write_snapshot(path, snapshot)
    except (SnapshotStoreError, OSError) as exc:
        print(f"cannot write {path}: {exc}", file=sys.stderr)
        return 1

    print(f"{snapshot.device.name}: {len(snapshot.layers)} layers -> {path}")
    if args.text:
        print()
        print(KeymapRenderer(snapshot).to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
