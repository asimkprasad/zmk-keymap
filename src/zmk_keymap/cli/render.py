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

from ._common import CliError, load_snapshot_arg, utf8_stdio
from ..keymap_renderer import KeymapRenderer

#: Extension to use when --out is a directory, paired 1:1 with KeymapRenderer.FORMATS.
_SUFFIX = {"text": ".txt", "html": ".html", "svg": ".svg"}


def main(argv: Sequence[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="keymap-render",
        description="Render a saved keymap snapshot. Never touches a keyboard.")
    parser.add_argument("snapshot", type=pathlib.Path,
                        help="a JSON snapshot written by keymap-backup")
    parser.add_argument("-f", "--format", choices=KeymapRenderer.FORMATS, default="html",
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

    try:
        snapshot = load_snapshot_arg(args.snapshot)
    except CliError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    if not snapshot.layers:
        print(f"{args.snapshot} has no layers to render", file=sys.stderr)
        return 1

    try:
        output = KeymapRenderer(snapshot).render(args.format, layer_id=args.layer)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    if args.out is None:
        try:
            sys.stdout.write(output if output.endswith("\n") else output + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return 0
        return 0

    path: pathlib.Path = args.out
    if path.is_dir():
        path = path / (args.snapshot.stem + _SUFFIX[args.format])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    except OSError as exc:
        print(f"cannot write {path}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    print(f"{snapshot.device.name}: {len(snapshot.layers)} layers -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
