"""Command line entry point: read the keymap and write it out."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
import sys

from . import describe, dump, to_json
from .snapshot import InvalidLayout
from zmk_studio_client import KeyboardLocked, NotReachable, RpcError


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or ""
        if enc.lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _default_path() -> str:
    """snapshots/keymap.json in the enclosing repository, else the current directory.

    One file rather than a dated one: the output is deterministic apart from the capture
    time, so git already records what changed on the keyboard and when. Dated filenames
    would duplicate that as a pile of near-identical blobs.
    """
    directory = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(directory, ".git")):
            return os.path.join(directory, "snapshots", "keymap.json")
        parent = os.path.dirname(directory)
        if parent == directory:
            return "keymap.json"
        directory = parent


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="keymap-backup",
        description="Read a ZMK keyboard's live keymap. Never modifies the keyboard.")
    parser.add_argument("-o", "--out", metavar="PATH",
                        help="write JSON here (default: snapshots/keymap.json in the "
                             "repository, overwritten so git holds the history)")
    parser.add_argument("-t", "--text", action="store_true",
                        help="also print the keymap as readable text")
    parser.add_argument("--include-serial", action="store_true",
                        help="record the hardware serial number as well as its digest; "
                             "off by default since snapshots are often published")
    args = parser.parse_args(argv)

    try:
        snapshot = dump(include_serial=args.include_serial)
    except (NotReachable, KeyboardLocked, RpcError, InvalidLayout) as exc:
        # Nothing is written on failure: the snapshot path holds the last good dump,
        # and overwriting it with a partial one is the worst thing this tool could do.
        print(f"{exc}", file=sys.stderr)
        return 1

    path = args.out or _default_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(to_json(snapshot))

    print(f"{snapshot.device.name}: {len(snapshot.layers)} layers -> {path}")
    if args.text:
        print()
        print(describe(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
