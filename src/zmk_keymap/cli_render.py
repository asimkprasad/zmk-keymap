"""Command line entry point: turn a saved snapshot into something readable.

Separate from `keymap-backup` on purpose. This command takes a path and never a
keyboard: it calls no transport, opens no port, and cannot fail because a
keyboard is asleep, locked or on the wrong endpoint. Rendering a backup from
three years ago works exactly as well as rendering this morning's.

The split also means the two commands fail differently, which is the point. A
capture failing says something about the hardware; a render failing says
something about a file.
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import pathlib
import sys

from .model import KeymapSnapshot, from_json
from .render import FORMATS, svg

#: Extension to use when --out is a directory, paired 1:1 with render.FORMATS.
_SUFFIX = {"text": ".txt", "html": ".html", "svg": ".svg"}


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or ""
        if enc.lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _load(path: pathlib.Path) -> KeymapSnapshot:
    """Parse a committed snapshot back into the message it was written from."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc.strerror}")
    try:
        return from_json(raw)
    except ValueError as exc:
        raise SystemExit(f"{path} is {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="keymap-render",
        description="Render a saved keymap snapshot. Never touches a keyboard.")
    parser.add_argument("snapshot", type=pathlib.Path,
                        help="a JSON snapshot written by keymap-backup")
    parser.add_argument("-f", "--format", choices=sorted(FORMATS), default="html",
                        help="output format (default: html)")
    parser.add_argument("-o", "--out", type=pathlib.Path,
                        help="write here instead of stdout")
    parser.add_argument("--layer", type=int, metavar="ID",
                        help="svg only: which layer id to draw (default: the first "
                             "in the keyboard's display order)")
    args = parser.parse_args(argv)

    if args.layer is not None and args.format != "svg":
        parser.error("--layer applies to --format svg; the other formats "
                     "render every layer")

    snapshot = _load(args.snapshot)
    if not snapshot.layers:
        raise SystemExit(f"{args.snapshot} has no layers to render")

    try:
        output = (
            svg(snapshot, args.layer)
            if args.layer is not None
            else FORMATS[args.format](snapshot)
        )
    except ValueError as exc:
        raise SystemExit(str(exc))

    if args.out is None:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")
        return 0

    path: pathlib.Path = args.out
    if path.is_dir():
        path = path / (args.snapshot.stem + _SUFFIX[args.format])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output, encoding="utf-8")
    print(f"{snapshot.device.name}: {len(snapshot.layers)} layers -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
