"""`zmk_keymap` — back up, inspect, diff, and restore ZMK Studio keymaps.

Intentionally keeps `__init__.py` free of submodule imports so importing one floor
never loads unrelated floors:

- `zmk_keymap.keymap_snapshot` & `zmk_keymap.keymap_diff` — pure offline domain model
- `zmk_keymap.keymap_renderer` — pure offline SVG / HTML / text presentation
- `zmk_keymap.live_keyboard` — read-only live keyboard capture (`0` writes)
- `zmk_keymap.restore_plan` — guarded live restore orchestration (`RestorePlan`)
- `zmk_keymap.snapshot_store` & `zmk_keymap.ui` — confined local storage & web UI
- `zmk_keymap.cli` — command-line entry points
"""
from __future__ import annotations
