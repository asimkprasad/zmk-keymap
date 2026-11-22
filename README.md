# zmk-keymap

Back up, restore and draw the keymap living on a ZMK keyboard.

**Unofficial.** Not affiliated with, endorsed by, or maintained by the ZMK project.

ZMK Studio can edit a keymap on a running keyboard, but cannot export one — so a layout
built up through Studio exists only in the keyboard's settings partition, with no copy
anywhere. This reads it out over the same RPC Studio uses, writes it to a file git can
hold, renders it without a browser framework, and puts it back when you need it — from
either the command line or a local browser UI.

```text
keymap-backup            keyboard  ->  snapshots/keymap.json   (never writes to the keyboard)
keymap-render  FILE      a file    ->  HTML, SVG or text       (no keyboard needed)
keymap-restore FILE      a file    ->  keyboard                (dry-run unless --commit)
keymap-ui                browser   <-> workspace & keyboard    (loopback-only local UI)
```

## Installing

```bash
uv tool install "git+https://github.com/asimkprasad/zmk-keymap.git"
```

Or inside a project or virtual environment:

```bash
pip install "git+https://github.com/asimkprasad/zmk-keymap.git"
```

Install into a virtual environment or isolated tool environment rather than with
`pip install --user`: `keymap-ui` runs its hardware workers under `python -I` (isolated
mode), which disables user site-packages.

## Usage

### Local browser workspace (`keymap-ui`)

```bash
keymap-ui
```

Starts a loopback-only Python `http.server` (`127.0.0.1`) rooted at the enclosing git
repository (or `--dir PATH`) and opens the workspace in your browser:

- **Inspect & compare:** Human-friendly keycap legends (`⌃⇧⌘4`, `⌫`, `⏎`, `BT 0`, named
  layers) with a one-key toggle (`R`) to exact ZMK tokens (`LC(LS(LG(N4)))`, `BSPC`,
  `L4`), single-layer tabs or an all-layers stacked view (`A`), offline snapshot-to-snapshot
  visual diffs, and a click-to-inspect key drawer showing any key across every layer alongside
  its raw 32-bit parameter hex values.
- **Back up & restore:** Read the live keyboard without writing, save snapshots to
  `snapshots/`, preview a live restore right on the SVG keycaps (`− before` / `+ after`)
  before a single byte is written, commit with automatic pre-flight undo capture to
  `undo-snapshots/`, and stage an undo preview in one click.
- **Security model:** `keymap-ui` binds exclusively to loopback (`127.0.0.1` / `localhost` /
  `::1`) on an OS-assigned port by default, validates the `Host` header against DNS
  rebinding on every request, emits zero CORS headers, serves a per-response nonce
  `Content-Security-Policy` with `X-Frame-Options: DENY` (`frame-ancestors 'none'`) on the
  HTML shell, requires a per-process random `X-Keymap-Token` CSRF header on `/api/workspace`
  and every `POST` (plus `Content-Type: application/json` on `POST`), confines file access
  to validated `.json` basenames inside `snapshots/` and `undo-snapshots/`, and verifies a
  SHA-256 `plan_digest` before committing a restore. There is no user login: any local
  process on the same machine — including another OS user account on a shared or multi-user
  host — can connect to loopback TCP (`127.0.0.1` / `::1`) and reach `keymap-ui`, even if
  that user could not open the USB serial port or BLE GATT session directly. Run
  `keymap-ui` only on a trusted single-user workstation.

### Back up from the command line

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

### Render a static snapshot

```bash
keymap-render snapshots/keymap.json -o keymap.html
keymap-render snapshots/keymap.json -f svg --layer 0 -o base.svg
keymap-render snapshots/keymap.json -f text
```

`keymap-render` reads the JSON file and never opens or calls a transport. The HTML output
is a single self-contained file with inline SVG, CSS-driven layer tabs, pure-CSS Friendly /
Raw ZMK and All-Layers toggles, dark-mode support, and zero JavaScript or network requests —
it opens directly from a `file://` URL.

### Restore from the command line

```bash
keymap-restore snapshots/keymap.json            # dry run: prints the diff, sends no writes
keymap-restore snapshots/keymap.json --commit   # writes only changed keys, verifies, saves
```

Restoring remaps behavior IDs by display name against the live firmware's behavior table,
refuses if the keyboard has unsaved Studio edits or an incompatible active physical
layout, writes a timestamped pre-flight undo snapshot to `undo-snapshots/` before touching
RAM, re-reads the keymap to verify every change took effect, and only then commits to
flash (`save_changes`) — rolling back via `discard_changes` on any failure.

### Python API

```python
from zmk_keymap.keymap_renderer import KeymapRenderer
from zmk_keymap.keymap_snapshot import KeymapSnapshot
from zmk_keymap.live_keyboard import read_keyboard

snapshot = read_keyboard()                     # live device -> immutable KeymapSnapshot
raw = snapshot.to_json()                       # canonical ProtoJSON
loaded = KeymapSnapshot.from_json(raw)         # offline domain model
diff = snapshot.diff(loaded)                   # KeymapDiff
html = KeymapRenderer(loaded).to_html()        # standalone HTML / SVG / text
```

The snapshot archive schema (`zmk_keymap.v1`) ships inside the wheel as
`zmk_keymap/proto/keymap_snapshot.proto` (source at
[`src/zmk_keymap/proto/keymap_snapshot.proto`](src/zmk_keymap/proto/keymap_snapshot.proto)).

## What it is not

Not an editor, and not a firmware compiler. ZMK Studio is the editor; this is version
control for what Studio produced. Nor does it capture rotary encoder bindings or behavior
timing parameters (`tapping-term-ms`, flavor, macros' inner steps), because the ZMK
Studio RPC protocol does not expose them — those still live in firmware devicetree source.

## How it talks to the keyboard, and platform support

All device communication goes through
[`zmk-studio-client`](https://github.com/asimkprasad/zmk-studio-client), which owns the
transports, framing, request correlation, and read/write allowlists. No source module in
this repository opens a serial port, frames a wire byte, or references ZMK's wire protobufs
(`zmk_studio_client._pb`) directly — and [`tests/test_isolation.py`](tests/test_isolation.py)
checks both the static import graph and runtime `sys.modules` to verify that only
`keymap-restore` and `keymap-ui` load the write surface (`zmk_studio_client.writable`),
`keymap-backup` is strictly read-only, and `keymap-render` has no call path to `connect()`
or any hardware transport. (`zmk_keymap/__init__.py` is import-free so submodule imports
have zero cross-floor side effects; importing `zmk_keymap.keymap_snapshot` loads
`zmk_studio_client`'s read package into `sys.modules` only because the snapshot protobuf
schema reuses `zmk_studio_client.v1.Behavior`.)

- **`keymap-render`** (and offline browsing/diffing in **`keymap-ui`**) is OS-independent
  and runs on macOS, Linux, and Windows.
- **Live keyboard operations in `keymap-backup`, `keymap-restore`, and `keymap-ui`** call
  `zmk-studio-client`'s automatic USB serial and CoreBluetooth GATT discovery, which is
  currently **macOS only** out of the box. On Linux or Windows, pass a three-method
  `Connection` to `zmk-studio-client`'s `Keyboard` / `WritableKeyboard` and call
  `zmk_keymap.live_keyboard.read_keyboard` or `zmk_keymap.restore_plan.RestorePlan`
  directly.

## Licence

MIT — see [`LICENSE`](LICENSE).
