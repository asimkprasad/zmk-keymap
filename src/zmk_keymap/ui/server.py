"""Localhost web workspace for inspecting, backing up, diffing, and restoring keymaps.

`keymap-render` produces a static, zero-JavaScript HTML file for publishing and
archiving. `keymap-ui` is its live counterpart: a local-only Python `http.server`
bound strictly to loopback (`127.0.0.1` / `localhost` / `::1`) that drives the
same read-only `read_keyboard()` and 10-step `RestorePlan` safety sequence from a
browser tab.

Security posture (because an unlocked ZMK Studio keyboard accepts RPC writes):

- Loopback-only binding: non-loopback `--host` values are refused at startup.
- DNS-rebinding defense: every `GET` and `POST` validates the HTTP `Host` header
  against loopback and the bound port before running any handler.
- Zero CORS headers, `Cache-Control: no-store`, `X-Frame-Options: DENY`, and a
  per-response nonce `Content-Security-Policy` (`default-src 'self'`,
  `script-src 'nonce-<random>'`, `style-src 'unsafe-inline'`,
  `img-src 'self' data: blob:`, `connect-src 'self'`, `object-src 'none'`,
  `base-uri 'none'`, `form-action 'self'`, `frame-ancestors 'none'`) on the HTML
  shell so the browser UI makes zero external requests, cannot be framed, and
  blocks inline event-attribute or un-nonced script execution.
- Per-process random CSRF token (`X-Keymap-Token`, checked with `secrets.compare_digest`
  over UTF-8 bytes) required on `/api/workspace` and on every `POST` route (alongside
  `Content-Type: application/json` on `POST`).
- Snapshot store confinement (`zmk_keymap.snapshot_store.SnapshotStore`): directory
  scans, file loads (bounded at 4 MiB), and atomic writes resolve symlinks and enforce
  a strict `.json` basename allowlist inside `snapshots/` and `undo-snapshots/`.
- Read/write subprocess split and commitment digest: live reads, backups, and
  dry-run restore previews spawn the read-only `ui._hw_read` worker (which never
  imports `zmk_studio_client.writable`), while `restore/commit` spawns `ui._hw`
  and verifies `RestorePlan.digest()` in constant time before writing.
- Local-process boundary (posture limit): there is no user login. Any local process
  able to connect to loopback TCP (`127.0.0.1` / `::1`) can fetch `GET /` to read
  `X-Keymap-Token` and invoke the API. Run `keymap-ui` only on a trusted single-user
  workstation.
"""
from __future__ import annotations

from collections.abc import Callable
import functools
import html as _html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import ipaddress
import json
import pathlib
import re
import secrets
import socket
import sys
import threading
import typing
from urllib.parse import urlsplit

from . import _hw, _hw_read
from .. import keymap_diff, keymap_snapshot as pb
from ..keymap_renderer import KeymapRenderer
from ..restore_plan import RestorePlan, RestoreRefused
from ..snapshot_store import (
    InvalidSnapshotName,
    PathNotAllowed,
    SnapshotNotFound,
    SnapshotStore,
    SnapshotStoreError,
)
from zmk_studio_client import Keyboard
from zmk_studio_client.writable import WritableKeyboard

__all__ = [
    "UiServer",
]

_MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB cap on JSON request bodies
_MAX_CONCURRENT_CONNECTIONS = 16
_REQUEST_TIMEOUT_SECONDS = 15.0
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
_LOG_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

_WORKER_STATUS_BY_KIND: dict[str, HTTPStatus] = {
    "bad_request": HTTPStatus.BAD_REQUEST,
    "conflict": HTTPStatus.CONFLICT,
    "timeout": HTTPStatus.GATEWAY_TIMEOUT,
    "internal": HTTPStatus.INTERNAL_SERVER_ERROR,
}


@functools.lru_cache(maxsize=1)
def _load_assets() -> tuple[str, str, str]:
    asset_dir = resources.files("zmk_keymap.ui").joinpath("assets")
    shell_html = asset_dir.joinpath("shell.html").read_text(encoding="utf-8")
    app_css = asset_dir.joinpath("app.css").read_text(encoding="utf-8")
    app_js = asset_dir.joinpath("app.js").read_text(encoding="utf-8")
    return shell_html, app_css, app_js


def _render_app_shell(*, csrf_token: str, script_nonce: str, svg_style: str) -> str:
    """Assemble the complete HTML document for `GET /` with CSP nonce and CSRF token."""
    if not _NONCE_RE.match(script_nonce):
        raise ValueError("script_nonce must be a URL-safe base64/hex token")
    shell_html, app_css, app_js = _load_assets()
    escaped_token = _html.escape(csrf_token, quote=True)
    css = app_css.replace("/*__SVG_STYLE__*/", svg_style)
    return (
        shell_html.replace("__CSRF_TOKEN__", escaped_token)
        .replace("__SCRIPT_NONCE__", script_nonce)
        .replace("__APP_CSS__", css)
        .replace("__APP_JS__", app_js)
    )


def _build_csp(script_nonce: str) -> str:
    """Build the `Content-Security-Policy` header bound to one `GET /` script nonce."""
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{script_nonce}'; "
        "style-src 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


class _HttpError(Exception):
    """Structured HTTP error turned into a JSON response."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        extra: dict[str, typing.Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def _raise_store_http_error(exc: SnapshotStoreError) -> typing.NoReturn:
    if isinstance(exc, InvalidSnapshotName):
        raise _HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    if isinstance(exc, SnapshotNotFound):
        raise _HttpError(HTTPStatus.NOT_FOUND, str(exc)) from exc
    if isinstance(exc, PathNotAllowed):
        raise _HttpError(HTTPStatus.FORBIDDEN, str(exc)) from exc
    raise _HttpError(HTTPStatus.FORBIDDEN, str(exc)) from exc


def _worker_http_status(res: dict[str, typing.Any]) -> int:
    kind = str(res.get("kind") or "conflict")
    return int(_WORKER_STATUS_BY_KIND.get(kind, HTTPStatus.CONFLICT))


def _is_loopback_host(host: str) -> bool:
    """True only for `localhost` or an IPv4/IPv6 loopback address."""
    cleaned = host.strip().lower().strip("[]")
    if not cleaned:
        return False
    if cleaned == "localhost":
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _is_allowed_host_header(header: str | None, server_port: int) -> bool:
    """Reject DNS-rebinding requests whose `Host` header is not local loopback."""
    if not header:
        return False
    raw = header.strip()
    if not raw or any(ch.isspace() for ch in raw):
        return False

    host_part: str
    port_part: str | None = None
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return False
        host_part = raw[1:end]
        rest = raw[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return False
            port_part = rest[1:]
    elif ":" in raw:
        host_part, port_part = raw.rsplit(":", 1)
    else:
        host_part = raw

    if not _is_loopback_host(host_part):
        return False
    if port_part is not None:
        if (
            not port_part.isascii()
            or not port_part.isdigit()
            or int(port_part) != server_port
        ):
            return False
    return True


def _format_diff_payload(
    *,
    device_name: str,
    identity_warning: str | None,
    diff: keymap_diff.KeymapDiff,
    target_renderer: KeymapRenderer,
) -> dict[str, typing.Any]:
    changes_out: list[dict[str, typing.Any]] = []
    for key_diff in diff:
        before_friendly, after_friendly = target_renderer.key_diff_summary(
            key_diff, diff
        )
        changes_out.append(
            {
                "layer_id": key_diff.layer_id,
                "layer_name": key_diff.layer_name,
                "position": key_diff.position,
                "before": key_diff.before,
                "after": key_diff.after,
                "before_friendly": before_friendly,
                "after_friendly": after_friendly,
            }
        )

    return {
        "device_name": device_name,
        "identity_warning": identity_warning,
        "compared": diff.compared,
        "remapping": {
            "is_identity": diff.translator.is_identity,
            "matched_count": diff.translator.matched_count,
            "unused_and_missing": list(diff.translator.unused_missing),
        },
        "changes": changes_out,
    }


def _serialize_snapshot(
    snap: pb.KeymapSnapshot,
    *,
    kind: str,
    label: str,
    path: str | None = None,
) -> dict[str, typing.Any]:
    """Turn a `KeymapSnapshot` into the JSON payload expected by the UI workspace."""
    pres = KeymapRenderer(snap)
    return {
        "raw_json": snap.to_json(),
        "source": {
            "kind": kind,
            "label": label,
            "path": path,
        },
        "device": {
            "name": snap.device.name,
            "id": snap.device.id,
            "has_serial": bool(snap.device.serial_number),
        },
        "capture": {
            "captured_at": snap.captured_iso,
            "tool_version": snap.metadata.tool_version,
            "studio_messages_revision": snap.metadata.studio_messages_revision,
        },
        "layout": {
            "name": snap.layout.name,
            "active_index": snap.layout.active_index,
            "available": list(snap.layout.available),
            "keys": [
                {
                    "position": key.position,
                    "x": key.x,
                    "y": key.y,
                    "width": key.width,
                    "height": key.height,
                    "rotation": key.rotation,
                    "rotation_x": key.rotation_x,
                    "rotation_y": key.rotation_y,
                }
                for key in snap.layout.keys
            ],
        },
        "behaviors": [
            {
                "id": behavior.id,
                "display_name": behavior.display_name,
                "parameter_sets": len(behavior.parameter_sets),
            }
            for behavior in snap.behaviors
        ],
        "layers": [
            {
                "id": layer.id,
                "display_order": layer.display_order,
                "name": layer.name,
                "svg": pres.layer_svg(layer),
                "bindings": {
                    str(binding.position): {
                        "position": binding.position,
                        "behavior_id": binding.behavior_id,
                        "behavior_name": binding.behavior_name,
                        "param1": binding.param1,
                        "param2": binding.param2,
                        "rendered": binding.decoded,
                        "friendly": pres.binding_summary(binding),
                        "params": [
                            {
                                "name": arg.name,
                                "raw": arg.raw,
                                "raw_hex": f"0x{arg.raw:08x}",
                                "decoded": arg.decoded,
                                "kind": pb.argument_kind_name(arg.kind),
                            }
                            for arg in binding.arguments
                        ],
                    }
                    for binding in layer.bindings
                },
            }
            for layer in snap.layers
        ],
    }


def _serialize_snapshot_list(
    store: SnapshotStore,
) -> dict[str, list[dict[str, typing.Any]]]:
    """Build the UI sidebar summary cards from confined `store.list_snapshots()`."""
    valid_items: list[dict[str, typing.Any]] = []
    error_items: list[dict[str, typing.Any]] = []
    for rel_posix in store.list_snapshots():
        name = pathlib.PurePosixPath(rel_posix).name
        try:
            snap = store.load(rel_posix)
            valid_items.append(
                {
                    "name": name,
                    "rel_path": rel_posix,
                    "device_name": snap.device.name,
                    "device_id": snap.device.id,
                    "layers": len(snap.layers),
                    "captured_at": snap.captured_iso,
                    "error": None,
                }
            )
        except (SnapshotStoreError, OSError, ValueError) as exc:
            error_items.append(
                {
                    "name": name,
                    "rel_path": rel_posix,
                    "device_name": "",
                    "device_id": "",
                    "layers": 0,
                    "captured_at": "",
                    "error": str(exc),
                }
            )

    valid_items.sort(
        key=lambda item: (item["captured_at"], item["name"]),
        reverse=True,
    )
    return {"snapshots": valid_items + error_items}


def _parse_snapshot_json(raw_json: str) -> pb.KeymapSnapshot:
    try:
        return pb.KeymapSnapshot.from_json(raw_json)
    except (ValueError, RecursionError) as exc:
        raise _HttpError(HTTPStatus.BAD_REQUEST, f"invalid keymap snapshot: {exc}") from exc


def _require_str(body: dict[str, typing.Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _HttpError(HTTPStatus.BAD_REQUEST, f"missing or empty '{key}'")
    return value


class UiServer(ThreadingHTTPServer):
    """Loopback-only HTTP server holding `SnapshotStore`, CSRF secret, and hardware lock."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        store_dir: pathlib.Path,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        quiet: bool = False,
        connect_fn: Callable[[], Keyboard] | None = None,
        connect_writable_fn: Callable[[], WritableKeyboard] | None = None,
    ) -> None:
        if not _is_loopback_host(host):
            raise ValueError(
                f"refusing to bind keymap-ui to non-loopback host {host!r}; "
                "use '127.0.0.1', 'localhost', or '::1'"
            )
        bind_host = host.strip().strip("[]")
        try:
            if ipaddress.ip_address(bind_host).version == 6:
                self.address_family = socket.AF_INET6
        except ValueError:
            pass
        self.store = SnapshotStore(store_dir)
        self.csrf_token = secrets.token_urlsafe(32)
        self.hardware_lock = threading.RLock()
        self._conn_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_CONNECTIONS)
        self.quiet = quiet
        self._connect_fn = connect_fn
        self._connect_writable_fn = connect_writable_fn
        super().__init__((bind_host, port), _UiHandler)

    def process_request(self, request: typing.Any, client_address: typing.Any) -> None:
        """Bound concurrent handler threads without blocking `serve_forever`'s accept loop."""
        if not self._conn_semaphore.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._conn_semaphore.release()
            raise

    def process_request_thread(
        self, request: typing.Any, client_address: typing.Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_semaphore.release()

    def run_read(
        self,
        include_serial: bool = False,
        *,
        require_saved: bool = False,
    ) -> pb.KeymapSnapshot:
        """Read the live keyboard via the strictly read-only `_hw_read` worker."""
        with self.hardware_lock:
            read_session_fn = self._connect_fn or self._connect_writable_fn
            if read_session_fn is not None:
                res = _hw_read.execute_read(
                    include_serial=include_serial,
                    require_saved=require_saved,
                    connect_fn=read_session_fn,
                )
            else:
                res = _hw_read.invoke_subprocess(
                    {
                        "action": "read",
                        "include_serial": include_serial,
                        "require_saved": require_saved,
                    }
                )
        if not res.get("ok"):
            raise _HttpError(
                _worker_http_status(res),
                str(res.get("error", "failed to read keyboard")),
            )
        try:
            return pb.KeymapSnapshot.from_json(str(res["snapshot_json"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise _HttpError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"malformed hardware worker response: {exc}",
            ) from exc

    def run_restore_commit(
        self,
        *,
        want_json: str,
        plan_digest: str,
        force_different_device: bool,
        undo_dir: pathlib.Path | None = None,
    ) -> dict[str, typing.Any]:
        """Verify `plan_digest` and commit restore (via isolated main-thread child process unless mocked)."""
        target_undo = str(undo_dir or self.store.undo_snapshots_dir)
        with self.hardware_lock:
            if self._connect_writable_fn is not None:
                res = _hw.execute_restore_commit(
                    want_json=want_json,
                    plan_digest=plan_digest,
                    force_different_device=force_different_device,
                    undo_dir=target_undo,
                    connect_fn=self._connect_writable_fn,
                )
            else:
                res = _hw.invoke_subprocess(
                    {
                        "action": "restore_commit",
                        "want_json": want_json,
                        "plan_digest": plan_digest,
                        "force_different_device": force_different_device,
                        "undo_dir": target_undo,
                    }
                )
        if not res.get("ok"):
            extra: dict[str, typing.Any] = {}
            if res.get("refresh_files"):
                extra["files"] = _serialize_snapshot_list(self.store)
            raise _HttpError(
                _worker_http_status(res),
                str(res.get("error", "restore commit failed")),
                extra=extra,
            )
        return res

    @property
    def url(self) -> str:
        raw_host = str(self.server_address[0])
        host = f"[{raw_host}]" if ":" in raw_host else raw_host
        return f"http://{host}:{self.server_port}"


class _UiHandler(BaseHTTPRequestHandler):
    """HTTP handler for the `keymap-ui` HTML shell and JSON API."""

    server: UiServer
    timeout = _REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: object) -> None:
        if self.server.quiet:
            return
        safe_line = _LOG_CTRL_RE.sub("?", format % args)
        print(f"[keymap-ui] {safe_line}", file=sys.stderr, flush=True)

    def _check_host(self) -> bool:
        if not _is_allowed_host_header(self.headers.get("Host"), self.server.server_port):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden Host header (loopback required)"},
            )
            return False
        return True

    def _has_valid_csrf(self) -> bool:
        provided = self.headers.get("X-Keymap-Token") or ""
        if not provided:
            return False
        return secrets.compare_digest(
            provided.encode("utf-8", errors="surrogatepass"),
            self.server.csrf_token.encode("ascii"),
        )

    def _send_json(self, status: int, payload: dict[str, typing.Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html_doc: str, *, csp: str) -> None:
        raw = html_doc.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(raw)

    def _read_post_json(self) -> dict[str, typing.Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _HttpError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
            )

        if not self._has_valid_csrf():
            raise _HttpError(
                HTTPStatus.FORBIDDEN,
                "invalid or missing X-Keymap-Token CSRF header",
            )

        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "missing Content-Length header")
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length header") from exc
        if length < 0:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "negative Content-Length header")
        if length > _MAX_BODY_BYTES:
            raise _HttpError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"request body exceeds {_MAX_BODY_BYTES} bytes",
            )

        raw_bytes = self.rfile.read(length) if length > 0 else b"{}"
        try:
            decoded = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise _HttpError(HTTPStatus.BAD_REQUEST, f"malformed JSON body: {exc}") from exc
        if not isinstance(decoded, dict):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "JSON request body must be an object")
        return decoded

    # -- GET -----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        route = urlsplit(self.path).path
        if route == "/":
            script_nonce = secrets.token_urlsafe(24)
            shell = _render_app_shell(
                csrf_token=self.server.csrf_token,
                script_nonce=script_nonce,
                svg_style=KeymapRenderer.SVG_STYLE,
            )
            self._send_html(shell, csp=_build_csp(script_nonce))
            return
        if route == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if route == "/api/workspace":
            if not self._has_valid_csrf():
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "invalid or missing X-Keymap-Token CSRF header"},
                )
                return
            store = self.server.store
            self._send_json(
                HTTPStatus.OK,
                {"root": str(store.root), "files": _serialize_snapshot_list(store)},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown route: {route}"})

    # -- POST ----------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        route = urlsplit(self.path).path
        try:
            body = self._read_post_json()
            result = self._dispatch_post(route, body)
        except TimeoutError:
            self.close_connection = True
            return
        except _HttpError as err:
            payload: dict[str, typing.Any] = {"error": err.message, **err.extra}
            self._send_json(err.status, payload)
            return
        self._send_json(HTTPStatus.OK, result)

    def _dispatch_post(
        self, route: str, body: dict[str, typing.Any]
    ) -> dict[str, typing.Any]:
        if route == "/api/snapshots/load":
            return self._handle_snapshots_load(body)
        if route == "/api/keyboard/read":
            return self._handle_keyboard_read()
        if route == "/api/backup":
            return self._handle_backup(body)
        if route == "/api/render/html":
            return self._handle_render_html(body)
        if route == "/api/diff/snapshots":
            return self._handle_diff_snapshots(body)
        if route == "/api/restore/plan":
            return self._handle_restore_plan(body)
        if route == "/api/restore/commit":
            return self._handle_restore_commit(body)
        raise _HttpError(HTTPStatus.NOT_FOUND, f"unknown route: {route}")

    # -- route handlers ------------------------------------------------------

    def _handle_snapshots_load(self, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
        store = self.server.store
        rel_arg = body.get("path")
        if isinstance(rel_arg, str) and rel_arg.strip():
            try:
                snap = store.load(rel_arg)
            except SnapshotStoreError as exc:
                _raise_store_http_error(exc)
            except ValueError as exc:
                raise _HttpError(
                    HTTPStatus.BAD_REQUEST, f"invalid keymap snapshot: {exc}"
                ) from exc
            except OSError as exc:
                raise _HttpError(
                    HTTPStatus.BAD_REQUEST,
                    f"cannot read snapshot: {exc.strerror or exc}",
                ) from exc
            parts = pathlib.PurePosixPath(rel_arg.strip().replace("\\", "/")).parts
            canonical_rel = f"{parts[0]}/{parts[1]}"
            kind = "snapshot" if parts[0] == "snapshots" else "undo"
            return {
                "snapshot": _serialize_snapshot(
                    snap,
                    kind=kind,
                    label=parts[1],
                    path=canonical_rel,
                )
            }

        raw_json = body.get("raw_json")
        if isinstance(raw_json, str):
            label = str(body.get("label") or "imported.json").strip()[:120] or "imported.json"
            snap = _parse_snapshot_json(raw_json)
            return {
                "snapshot": _serialize_snapshot(
                    snap, kind="imported", label=label, path=None
                )
            }

        raise _HttpError(HTTPStatus.BAD_REQUEST, "expected 'path' or 'raw_json'")

    def _handle_keyboard_read(self) -> dict[str, typing.Any]:
        snap = self.server.run_read(False)
        return {
            "snapshot": _serialize_snapshot(
                snap, kind="live", label="Live keyboard", path=None
            )
        }

    def _handle_backup(self, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
        store = self.server.store
        raw_filename = body.get("filename")
        explicit_name = raw_filename.strip() if isinstance(raw_filename, str) else ""
        include_serial = bool(body.get("include_serial", False))

        if explicit_name:
            try:
                store.validate_snapshot_name(explicit_name)
            except SnapshotStoreError as exc:
                _raise_store_http_error(exc)

        with self.server.hardware_lock:
            snap = self.server.run_read(include_serial)
            try:
                rel_posix = store.save(snap, filename=explicit_name or None)
            except SnapshotStoreError as exc:
                _raise_store_http_error(exc)

        saved_name = pathlib.PurePosixPath(rel_posix).name
        return {
            "saved_path": rel_posix,
            "snapshot": _serialize_snapshot(
                snap, kind="snapshot", label=saved_name, path=rel_posix
            ),
            "files": _serialize_snapshot_list(store),
        }

    def _handle_render_html(self, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
        snap = _parse_snapshot_json(_require_str(body, "raw_json"))
        return {"html": KeymapRenderer(snap).to_html()}

    def _handle_diff_snapshots(self, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
        before = _parse_snapshot_json(_require_str(body, "before_json"))
        after = _parse_snapshot_json(_require_str(body, "after_json"))
        try:
            result = before.diff(after)
        except (
            keymap_diff.BehaviorMissing,
            keymap_diff.BehaviorAmbiguous,
            keymap_diff.LayoutMismatch,
        ) as exc:
            raise _HttpError(HTTPStatus.CONFLICT, str(exc)) from exc

        after_renderer = KeymapRenderer(after)
        return {
            "proposal": _format_diff_payload(
                device_name=before.device.name or "Baseline",
                identity_warning=None,
                diff=result,
                target_renderer=after_renderer,
            ),
            "diff_layers": [
                {
                    "id": layer.id,
                    "name": layer.name,
                    "svg": after_renderer.layer_svg(layer, diff=result),
                }
                for layer in after.layers
            ],
            "baseline_snapshot": _serialize_snapshot(
                before,
                kind="baseline",
                label=before.device.name or "Baseline",
                path=None,
            ),
        }

    def _handle_restore_plan(self, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
        want = _parse_snapshot_json(_require_str(body, "raw_json"))
        if not want.layers:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "snapshot has no layers to restore")

        live_snap = self.server.run_read(include_serial=False, require_saved=True)
        try:
            plan = RestorePlan(live=live_snap, target=want)
        except RestoreRefused as exc:
            raise _HttpError(HTTPStatus.CONFLICT, str(exc)) from exc

        want_renderer = KeymapRenderer(want)
        return {
            "proposal": _format_diff_payload(
                device_name=plan.device_name,
                identity_warning=plan.identity_warning,
                diff=plan.diff,
                target_renderer=want_renderer,
            ),
            "plan_digest": plan.digest(),
            "diff_layers": [
                {
                    "id": layer.id,
                    "name": layer.name,
                    "svg": want_renderer.layer_svg(layer, diff=plan.diff),
                }
                for layer in want.layers
            ],
            "live_snapshot": _serialize_snapshot(
                live_snap, kind="live", label="Live keyboard", path=None
            ),
        }

    def _handle_restore_commit(self, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
        store = self.server.store
        want = _parse_snapshot_json(_require_str(body, "raw_json"))
        expected_digest = _require_str(body, "plan_digest")
        force_different_device = bool(body.get("force_different_device", False))

        if not want.layers:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "snapshot has no layers to restore")

        try:
            confined_undo = store.ensure_undo_snapshots_dir()
        except SnapshotStoreError as exc:
            _raise_store_http_error(exc)

        canonical_want = want.to_json()
        res = self.server.run_restore_commit(
            want_json=canonical_want,
            plan_digest=expected_digest,
            force_different_device=force_different_device,
            undo_dir=confined_undo,
        )

        try:
            undo_file = pathlib.Path(str(res["undo_path"]))
            live_snap = pb.KeymapSnapshot.from_json(str(res["live_json"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise _HttpError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"malformed restore commit worker response: {exc}",
            ) from exc
        try:
            undo_rel = undo_file.resolve().relative_to(store.root).as_posix()
        except ValueError:
            undo_rel = str(undo_file)
        return {
            "undo_path": undo_rel,
            "live_snapshot": _serialize_snapshot(
                live_snap, kind="live", label="Live keyboard", path=None
            ),
            "files": _serialize_snapshot_list(store),
        }
