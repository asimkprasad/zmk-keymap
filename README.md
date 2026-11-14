# zmk-keymap

Back up, restore and draw the keymap living on a ZMK keyboard.

**Unofficial.** Not affiliated with, endorsed by, or maintained by the ZMK project.

ZMK Studio can edit a keymap on a running keyboard, but cannot export one — so a layout
built up through Studio exists only in the keyboard's settings partition, with no copy
anywhere. This reads it out over the same RPC Studio uses, writes it to a file git can
hold, renders it without a browser framework, and puts it back when you need it.

```text
keymap-backup            keyboard  ->  snapshots/keymap.json   (never writes to the keyboard)
keymap-render  FILE      a file    ->  HTML, SVG or text       (no keyboard needed)
keymap-restore FILE      a file    ->  keyboard                (dry-run unless --commit)
```

## Installing

```bash
uv tool install "git+https://github.com/asimkprasad/zmk-keymap.git"
```

Or inside a project or virtual environment:

```bash
pip install "git+https://github.com/asimkprasad/zmk-keymap.git"
```

## Usage

### Back up a connected keyboard

Unlock ZMK Studio on the keyboard (`&studio_unlock`) if locking is enabled, close ZMK
Studio so the serial port or BLE GATT session is free, and run:

```bash
keymap-backup
```

Inside a git repository, this writes `snapshots/keymap.json` at the repository root (or
`./keymap.json` outside one); pass `-o PATH` to write elsewhere, or `-t` to print the
decoded bindings as text as well. Hardware serial numbers are replaced by a 12-character
SHA-256 prefix so snapshots are safe to commit publicly (`--include-serial` overrides
this).

### Render a snapshot

```bash
keymap-render snapshots/keymap.json -o keymap.html
keymap-render snapshots/keymap.json -f svg --layer 0 -o base.svg
keymap-render snapshots/keymap.json -f text
```

`keymap-render` reads the JSON file and never opens or calls a transport. The HTML output
is a single self-contained file with inline SVG, CSS-driven layer tabs, dark-mode support,
and zero JavaScript or network requests — it opens directly from a `file://` URL.

### Restore a snapshot

```bash
keymap-restore snapshots/keymap.json            # dry run: prints the diff, sends no writes
keymap-restore snapshots/keymap.json --commit   # writes only changed keys, verifies, saves
```

Restoring remaps behavior IDs by display name against the live firmware's behavior table,
refuses if the keyboard has unsaved Studio edits or an incompatible active physical
layout, writes a timestamped pre-flight backup before touching RAM, re-reads the keymap to
verify every change took effect, and only then commits to flash (`save_changes`) —
rolling back via `discard_changes` on any failure.

### Python API

```python
from zmk_keymap import dump, from_json, page, to_json

snapshot = dump()                      # zmk_keymap.model.KeymapSnapshot
raw = to_json(snapshot)
loaded = from_json(raw)
html = page(loaded)
```

The snapshot archive schema (`zmk_keymap.v1`) ships inside the wheel at
[`src/zmk_keymap/proto/keymap_snapshot.proto`](src/zmk_keymap/proto/keymap_snapshot.proto).

## What it is not

Not an editor, and not a firmware compiler. ZMK Studio is the editor; this is version
control for what Studio produced. Nor does it capture rotary encoder bindings or behavior
timing parameters (`tapping-term-ms`, flavor, macros' inner steps), because the ZMK
Studio RPC protocol does not expose them — those still live in firmware devicetree source.

## How it talks to the keyboard, and platform support

All device communication goes through
[`zmk-studio-client`](https://github.com/asimkprasad/zmk-studio-client), which owns the
transports, framing, request correlation, and read/write allowlists. No source module in
this repository opens a port, frames a byte, or references ZMK's wire protobufs
(`zmk_studio_client._pb`) directly — and [`tests/test_isolation.py`](tests/test_isolation.py)
checks both the static import graph and runtime `sys.modules` to verify that only
`keymap-restore` loads the write surface (`zmk_studio_client.writable`) and that
`keymap-render` has no call path to `connect()` or any hardware transport. (Importing
`zmk_keymap` still loads `zmk_studio_client`'s read package into `sys.modules`, because
`zmk_keymap/__init__.py` offers `dump()` and the snapshot schema reuses
`zmk_studio_client.v1.Behavior`.)

- **`keymap-render`** (and `zmk_keymap.model` / `zmk_keymap.render`) is OS-independent
  and runs on macOS, Linux, and Windows.
- **`keymap-backup` and `keymap-restore`** call `zmk-studio-client`'s automatic USB
  serial and CoreBluetooth GATT discovery, which is currently **macOS only** out of the
  box. On Linux or Windows, pass a three-method `Connection` to `zmk-studio-client`'s
  `Keyboard` / `WritableKeyboard` and call `zmk_keymap.snapshot.build` or
  `zmk_keymap.restore` directly.

## Licence

MIT — see [`LICENSE`](LICENSE).
