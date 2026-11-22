"""Write-capable main-thread restore-commit worker for `keymap-ui`.

Live reads, snapshot backups, and dry-run restore previews all run in `ui._hw_read`,
which never imports `zmk_studio_client.writable`. This module exists solely for
`execute_restore_commit`:

- Opens `connect_writable()`, reads `live = read_keyboard(session, require_saved=True)`,
  builds `RestorePlan(live, want)`, verifies `plan.digest()` in constant time
  (`secrets.compare_digest`), and runs `plan.apply()`.

Nothing here imports `http.HTTPStatus` or formats UI display legends; HTTP status
codes and view-model presentation belong to `ui/server.py`.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import pathlib
import secrets
import sys
import typing

from . import _hw_read
from .. import keymap_snapshot as pb, live_keyboard
from ..restore_plan import RestoreFailed, RestorePlan, RestoreRefused
from ..snapshot_store import SnapshotStore
from zmk_studio_client import KeyboardLocked, NotReachable, RpcError
from zmk_studio_client.writable import EditAbandoned, WritableKeyboard, connect_writable


def execute_restore_commit(
    *,
    want_json: str,
    plan_digest: str,
    force_different_device: bool,
    undo_dir: str,
    connect_fn: Callable[[], WritableKeyboard] = connect_writable,
) -> dict[str, typing.Any]:
    """Re-verify `plan_digest` against the live keyboard and run `plan.apply()`."""
    want = pb.KeymapSnapshot.from_json(want_json)
    undo_root = pathlib.Path(undo_dir)

    try:
        session = connect_fn()
    except NotReachable as exc:
        return {"ok": False, "kind": "conflict", "error": str(exc)}

    in_flight = False
    try:
        try:
            live = live_keyboard.read_keyboard(session, require_saved=True)
            plan = RestorePlan(live=live, target=want)
        except (
            RestoreRefused,
            live_keyboard.UnsavedChanges,
            live_keyboard.InvalidLayout,
            KeyboardLocked,
            RpcError,
        ) as exc:
            in_flight = True
            return {"ok": False, "kind": "conflict", "error": str(exc)}

        if plan.identity_warning and not force_different_device:
            in_flight = True
            return {
                "ok": False,
                "kind": "conflict",
                "error": (
                    f"different keyboard ({plan.identity_warning}); "
                    "check 'Restore onto this different keyboard' to confirm"
                ),
            }

        if plan.is_empty:
            in_flight = True
            return {
                "ok": False,
                "kind": "conflict",
                "error": "nothing to do: the keyboard already matches the snapshot",
            }

        actual_digest = plan.digest()
        if not secrets.compare_digest(actual_digest, plan_digest):
            in_flight = True
            return {
                "ok": False,
                "kind": "conflict",
                "error": (
                    "the keyboard state changed since the preview was computed; "
                    "re-run Plan Restore first. Nothing was written."
                ),
            }

        try:
            undo_path = plan.apply(
                session,
                lambda current: SnapshotStore.write_undo_snapshot(
                    undo_root, current
                ),
            )
        except RestoreRefused as exc:
            in_flight = True
            return {"ok": False, "kind": "conflict", "error": str(exc)}
        except RestoreFailed as exc:
            in_flight = True
            return {
                "ok": False,
                "kind": "internal",
                "error": str(exc),
                "refresh_files": True,
            }

        live_snap = live_keyboard.read_keyboard(session, include_serial=False)
    except BaseException:
        in_flight = True
        raise
    finally:
        try:
            session.close()
        except EditAbandoned as exc:
            if not in_flight:
                return {
                    "ok": False,
                    "kind": "internal",
                    "error": str(exc),
                    "refresh_files": True,
                }

    return {
        "ok": True,
        "undo_path": str(undo_path),
        "live_json": live_snap.to_json(),
    }


def invoke_subprocess(
    request: dict[str, typing.Any],
    *,
    timeout: float = _hw_read.DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, typing.Any]:
    """Run one restore-commit request in a clean main-thread `python -m zmk_keymap.ui._hw`."""
    return _hw_read.invoke_worker("zmk_keymap.ui._hw", request, timeout=timeout)


def _dispatch(req: dict[str, typing.Any]) -> dict[str, typing.Any]:
    action = req.get("action")
    if action == "self_check":
        return _hw_read.execute_self_check(
            register_objc_delegate=bool(req.get("register_objc_delegate", False))
        )
    if action == "restore_commit":
        return execute_restore_commit(
            want_json=str(req["want_json"]),
            plan_digest=str(req["plan_digest"]),
            force_different_device=bool(req.get("force_different_device", False)),
            undo_dir=str(req["undo_dir"]),
        )
    return {
        "ok": False,
        "kind": "bad_request",
        "error": f"unknown restore worker action: {action!r}",
    }


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8")
    req = json.loads(raw)
    result = _dispatch(req)
    sys.stdout.write(f"\n{_hw_read.STDOUT_MARKER}\n{json.dumps(result)}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
