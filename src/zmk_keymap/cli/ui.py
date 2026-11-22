"""Command line entry point: start the local `keymap-ui` browser workspace."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import pathlib
import sys
import webbrowser

from ._common import utf8_stdio
from ..snapshot_store import SnapshotStore
from ..ui.server import UiServer


def main(argv: Sequence[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="keymap-ui",
        description=(
            "Start a loopback-only browser UI for inspecting keymaps, backing up "
            "a live ZMK Studio keyboard, diffing snapshots, and running two-step "
            "restores."
        ),
    )
    parser.add_argument(
        "-d",
        "--dir",
        type=pathlib.Path,
        metavar="PATH",
        help="workspace directory containing snapshots/ and undo-snapshots/ "
             "(default: enclosing git repository or current directory)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback interface to bind ('127.0.0.1', 'localhost', or '::1'; "
             "non-loopback hosts are refused)",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=0,
        help="TCP port to listen on (default: 0, let the OS pick a free port)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the URL in the default browser automatically",
    )
    args = parser.parse_args(argv)

    if not (0 <= args.port <= 65535):
        print(f"invalid TCP port {args.port}: expected 0..65535", file=sys.stderr)
        return 1

    store_root = (args.dir or SnapshotStore.default_root()).resolve()
    if not store_root.exists():
        print(f"workspace directory does not exist: {store_root}", file=sys.stderr)
        return 1
    if not store_root.is_dir():
        print(f"workspace path is not a directory: {store_root}", file=sys.stderr)
        return 1

    try:
        server = UiServer(store_root, host=args.host, port=args.port)
    except (ValueError, OSError, OverflowError) as exc:
        print(f"cannot start keymap-ui: {exc}", file=sys.stderr)
        return 1

    print(f"keymap-ui listening on {server.url}  (workspace: {store_root})", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(server.url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping keymap-ui.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
