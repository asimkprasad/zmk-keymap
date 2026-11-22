"""Localhost web workspace (`keymap-ui`) package.

Intentionally keeps `__init__.py` free of write-surface imports so the read-only
hardware worker (`python -I -m zmk_keymap.ui._hw_read`) never loads `restore_plan`
or `zmk_studio_client.writable`. Import `UiServer` from `zmk_keymap.ui.server`.
"""
from __future__ import annotations
