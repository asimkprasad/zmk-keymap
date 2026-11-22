# Floor 10: `cli/` (`keymap-*` Entry Points) Design Decisions

This subpackage (`cli/`) provides the four console scripts declared in `pyproject.toml`: `keymap-backup`, `keymap-render`, `keymap-restore`, and `keymap-ui`.

## Why It Is Structured This Way

1. **Plain `main(argv) -> int` entry functions (no artificial CLI classes):**
   - Each command parses arguments, invokes lower floors, prints diagnostics to `sys.stderr`, and returns an integer exit code (`raise SystemExit(main())` appears solely under `if __name__ == "__main__":`).
2. **Strict per-command capability boundaries (enforced in `tests/test_isolation.py`):**
   - `cli/__init__.py` is intentionally empty so running `keymap-render` or `keymap-backup` never imports write-capable modules (`restore`, `ui.server`).
   - `cli/_common.py` (`CliError`, `utf8_stdio`, `load_snapshot_arg`) imports only standard-library modules and Floor 6 `KeymapSnapshot`, keeping `cli/render.py` 100% free of `zmk_studio_client` runtime callables and `cli/restore.py` 100% free of `KeymapRenderer`.
   - `cli/backup.py` writes atomically via `SnapshotStore.write_snapshot()` (refusing symlinks and never truncating an existing snapshot on write failure) and reaches zero `zmk_studio_client.writable` modules.
3. **`keymap-restore` scripting exit-code contract:**
   - `0`: Keyboard already matches, or `--commit` applied and verified cleanly.
   - `1`: Refused before sending any writes (bad input file, unreachable/locked keyboard, unsaved studio edits, layout/behavior mismatch, different device without `--force-different-device`, or undo-snapshot write failure).
   - `2`: Failed after writes began (`BindingRejected`, post-write verification mismatch, `SaveRefused`, or `EditAbandoned` on close; pending RAM edits discarded).
   - `3`: Dry-run preview succeeded and changes are pending (re-run with `--commit` to apply).
