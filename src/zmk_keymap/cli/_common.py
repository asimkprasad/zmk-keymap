"""Shared CLI startup and snapshot-file loading helpers.

Imports only standard-library modules and `zmk_keymap.keymap_snapshot` so offline
commands (`keymap-render`) remain free of `zmk_studio_client` runtime callables and
non-rendering commands (`keymap-restore`) remain free of `zmk_keymap.keymap_renderer`.
"""
from __future__ import annotations

import pathlib
import sys

from ..keymap_snapshot import KeymapSnapshot


class CliError(Exception):
    """User-facing CLI input/file error reported to `stderr` with exit status `1`."""


def utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 when the host locale defaults elsewhere."""
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or ""
        if enc.lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def load_snapshot_arg(path: pathlib.Path) -> KeymapSnapshot:
    """Read and parse a snapshot file from a CLI path argument, or raise `CliError`."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        return KeymapSnapshot.from_json(raw)
    except ValueError as exc:
        raise CliError(f"{path} is {exc}") from exc
