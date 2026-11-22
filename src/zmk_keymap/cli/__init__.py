"""Command-line entry points (`keymap-backup`, `keymap-render`, `keymap-restore`, `keymap-ui`).

Kept free of imports so offline (`render`) and read-only (`backup`) commands never
load write-capable modules (`restore`, `ui.server`).
"""
from __future__ import annotations
