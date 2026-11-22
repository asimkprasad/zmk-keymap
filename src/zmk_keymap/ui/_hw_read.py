"""Read-only main-thread hardware worker for `keymap-ui`.

Why `keymap-ui` runs live hardware operations in a short-lived child process:

1. **CoreBluetooth main-thread `NSRunLoop`:** `zmk_studio_client.connection.find_bluetooth`
   initializes `CBCentralManager` with `queue=None` (meaning the main dispatch queue)
   and pumps `NSRunLoop.currentRunLoop()`. Inside `ThreadingHTTPServer`, request
   handlers execute on worker threads whose run loop never receives main-queue
   CoreBluetooth callbacks. A child process runs on its own main thread, matching
   `keymap-backup` and `keymap-restore`.
2. **PyObjC global class registration:** `find_bluetooth()` defines
   `class Delegate(NSObject):` inside its function body. Objective-C registers classes
   process-globally, so a second `find_bluetooth()` call in the same long-lived Python
   process raises `objc.error: Delegate is overriding existing Objective-C class`.
   Exiting the worker process after each hardware operation gives every call a clean
   Objective-C runtime and guarantees CoreBluetooth releases the GATT link immediately.
3. **Read-only capability boundary:** Live reads, snapshot backups, and dry-run
   restore previews invoke this module (`python -m zmk_keymap.ui._hw_read`) rather
   than `ui._hw`, so the child process never imports `restore_plan` or
   `zmk_studio_client.writable` / `.writes` — enforced statically and at runtime in
   `tests/test_isolation.py`.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import os
import subprocess
import sys
import threading
import typing

from .. import live_keyboard
from zmk_studio_client import Keyboard, KeyboardLocked, NotReachable, RpcError, connect

STDOUT_MARKER = "__ZMK_KEYMAP_HW_JSON__"
DEFAULT_TIMEOUT_SECONDS = 60.0


def execute_read(
    *,
    include_serial: bool = False,
    require_saved: bool = False,
    connect_fn: Callable[[], Keyboard] = connect,
) -> dict[str, typing.Any]:
    """Read the live keymap once over a read-only `Keyboard` session (`0` writes)."""
    try:
        session = connect_fn()
    except (NotReachable, KeyboardLocked, RpcError) as exc:
        return {"ok": False, "kind": "conflict", "error": str(exc)}

    try:
        snap = live_keyboard.read_keyboard(
            session,
            include_serial=include_serial,
            require_saved=require_saved,
        )
    except (
        NotReachable,
        KeyboardLocked,
        RpcError,
        live_keyboard.InvalidLayout,
        live_keyboard.UnsavedChanges,
    ) as exc:
        return {"ok": False, "kind": "conflict", "error": str(exc)}
    finally:
        session.close()

    return {"ok": True, "snapshot_json": snap.to_json()}


def execute_self_check(*, register_objc_delegate: bool = False) -> dict[str, typing.Any]:
    """Verify main-thread execution and fresh Objective-C class table in tests."""
    if register_objc_delegate and sys.platform == "darwin":
        from Foundation import NSObject  # type: ignore[import-untyped]

        class Delegate(NSObject):  # type: ignore[misc]
            pass

        _ = Delegate
    return {
        "ok": True,
        "pid": os.getpid(),
        "is_main_thread": threading.current_thread() is threading.main_thread(),
    }


def invoke_worker(
    module: str,
    request: dict[str, typing.Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, typing.Any]:
    """Run `request` in an isolated main-thread `python -I -m <module>` child process."""
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", module],
            input=json.dumps(request).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=sys.prefix,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "kind": "timeout",
            "error": (
                f"timed out after {int(timeout)}s waiting for the keyboard — "
                "check that ZMK Studio is closed and the keyboard is awake"
            ),
        }

    stdout_text = proc.stdout.decode("utf-8", errors="replace")
    if STDOUT_MARKER in stdout_text:
        _, json_part = stdout_text.rsplit(STDOUT_MARKER, 1)
        try:
            parsed = json.loads(json_part.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
    detail = stderr_text or stdout_text.strip() or f"worker exited with code {proc.returncode}"
    return {
        "ok": False,
        "kind": "internal",
        "error": f"hardware worker failed: {detail}",
    }


def invoke_subprocess(
    request: dict[str, typing.Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, typing.Any]:
    """Run one read-only hardware request in `python -m zmk_keymap.ui._hw_read`."""
    return invoke_worker("zmk_keymap.ui._hw_read", request, timeout=timeout)


def _dispatch(req: dict[str, typing.Any]) -> dict[str, typing.Any]:
    action = req.get("action")
    if action == "self_check":
        return execute_self_check(
            register_objc_delegate=bool(req.get("register_objc_delegate", False))
        )
    if action == "read":
        return execute_read(
            include_serial=bool(req.get("include_serial", False)),
            require_saved=bool(req.get("require_saved", False)),
        )
    return {
        "ok": False,
        "kind": "bad_request",
        "error": f"unknown read-only hardware worker action: {action!r}",
    }


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8")
    req = json.loads(raw)
    result = _dispatch(req)
    sys.stdout.write(f"\n{STDOUT_MARKER}\n{json.dumps(result)}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
