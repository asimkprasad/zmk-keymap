# Test corpus

Frozen snapshots from real keyboards. Every decoder change is checked against them.

A file here is a `zmk_keymap.v1.KeymapSnapshot` as `keymap-backup` writes it (with the
hardware serial digest replaced by a synthetic test digest): real bindings, real behavior
tables, real parameter metadata. That makes them a regression corpus no fixture generator
could produce, because the values are whatever the firmware actually reported.

## Why these are copies, not `snapshots/keymap.json`

`snapshots/keymap.json` is a user's *live record* — `keymap-backup` overwrites it on
every capture, by design, so that git carries the history of what changed on a keyboard.
A test fixture must do the opposite and never move. Keeping frozen captures under
`tests/data/` ensures routine backups never redefine what the test suite checks.

## Adding one

Name it `<board>-<YYYY-MM-DD>.json`, matching the capture date in its own
`capture.captured_at` (and replace `device.id` with a synthetic 12-hex-character digest
before committing). Drop it in; `test_golden_corpus.py` globs this directory and
parametrises over everything it finds, so a new board needs no test changes.

Prefer adding a file to replacing one. A corpus that only ever holds the newest capture
cannot catch a regression against an older firmware's way of reporting itself.

| file | board | firmware notes |
|---|---|---|
| `sofle-2026-11-10.json` | Sofle, nice!nano v2 | 6 layers, 60 keys, `CONFIG_ZMK_STUDIO_LOCKING=n`. 27 behaviours, three of them keymap-defined (`hyper`, `rgb_encoder`, `enc_key_press`), and `External Power` reports no parameter metadata at all — which exercises the decoder's fallback path. |
