# Floor 9b: `ui/` (`keymap-ui`) Design Decisions

This subpackage (`ui/`) implements the localhost browser workspace (`keymap-ui`), sitting above Floor 6 (`KeymapSnapshot`, `KeymapDiff`), Floor 7 (`KeymapRenderer`), Floor 8 (`live_keyboard`, `RestorePlan`), and `SnapshotStore`.

## Why It Is Structured This Way

1. **Single public class `UiServer` (`ui/server.py`, `__all__ = ["UiServer"]`):**
   - `ui/__init__.py` stays empty so `python -I -m zmk_keymap.ui._hw_read` never executes `server.py` or loads `zmk_studio_client.writable`.
   - Static shell assembly (`_load_assets`, `_render_app_shell`) and JSON view-model serialization (`_serialize_snapshot`, `_format_diff_payload`) stay private inside `server.py`.
2. **Why hardware runs in short-lived child processes (`_hw_read.py` vs. `_hw.py`):**
   - **CoreBluetooth main-thread `NSRunLoop`:** `zmk_studio_client.connection.find_bluetooth()` pumps `NSRunLoop.currentRunLoop()` on the main dispatch queue, which never fires on `ThreadingHTTPServer` request threads.
   - **PyObjC global class registration:** `find_bluetooth()` defines `class Delegate(NSObject)` dynamically; running each hardware call in a fresh short-lived child process avoids Objective-C duplicate-class crashes and releases the BLE GATT link immediately.
   - **Strict read/write process split:**
     - `ui._hw_read` (`execute_read`) handles **all** read-only operations: `/api/keyboard/read`, `/api/backup`, **and** `/api/restore/plan` (`require_saved=True`), so dry-run restore previews never even load `restore_plan` or `zmk_studio_client.writable` into the worker's `sys.modules`. `UiServer` then constructs `RestorePlan(live, want)` in memory.
     - `ui._hw` (`execute_restore_commit`) exists **solely** for `/api/restore/commit`, verifying `RestorePlan.digest()` via `secrets.compare_digest` before calling `plan.apply()`.

## Security Invariants to Preserve

- **Loopback-only binding & DNS-rebinding check:** Refuse non-loopback `--host` values and validate `Host` against loopback + `server_port` on every `GET` and `POST`.
- **Per-response CSP nonce & CSRF token:** Every `GET /` generates a fresh `script-src 'nonce-...'` (`X-Frame-Options: DENY`, `frame-ancestors 'none'`, zero CORS headers), and every `POST` requires `Content-Type: application/json` plus constant-time `X-Keymap-Token` verification.
