"""Localhost web workspace (`keymap-ui`): HTTP security guards and backup/restore flow."""
from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import http.client
import json
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import typing

import pytest

from zmk_keymap import (
    keymap_snapshot as pb,
    live_keyboard,
    snapshot_store as store_mod,
)
from zmk_keymap.cli import ui as cli_ui
from zmk_keymap.ui import _hw as _ui_hw, _hw_read as _ui_hw_read, server as ui
from zmk_studio_client import Keyboard, KeyboardLocked
from zmk_studio_client.writable import WritableKeyboard

from test_restore_plan import (
    CORPUS,
    _FakeSession,
    _diverge,
    _snapshot,
    _with_device,
    _with_flipped_param1,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "src" / "zmk_keymap" / "ui" / "assets"


@contextlib.contextmanager
def _running_ui(
    workspace: pathlib.Path,
    *,
    connect_fn: Callable[[], Keyboard] | None = None,
    connect_writable_fn: Callable[[], WritableKeyboard] | None = None,
) -> Iterator[ui.UiServer]:
    server = ui.UiServer(
        workspace,
        host="127.0.0.1",
        port=0,
        quiet=True,
        connect_fn=connect_fn,
        connect_writable_fn=connect_writable_fn,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)


def _request(
    server: ui.UiServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5.0)
    try:
        req_headers = dict(headers or {})
        put_host = "Host" in req_headers
        conn.putrequest(method, path, skip_host=put_host)
        for key, val in req_headers.items():
            conn.putheader(key, val)
        if body is not None and "Content-Length" not in req_headers:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body is not None:
            conn.send(body)
        resp = conn.getresponse()
        raw = resp.read()
        out_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, out_headers, raw
    finally:
        conn.close()


def _post_json(
    server: ui.UiServer,
    path: str,
    payload: dict[str, typing.Any],
    *,
    token: str | None = "AUTO",
) -> tuple[int, dict[str, typing.Any]]:
    headers = {"Content-Type": "application/json"}
    if token == "AUTO":
        headers["X-Keymap-Token"] = server.csrf_token
    elif token is not None:
        headers["X-Keymap-Token"] = token
    status, _, raw = _request(
        server,
        "POST",
        path,
        body=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    return status, json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- #
# loopback & HTTP security boundaries
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_host", ["0.0.0.0", "192.168.1.10", "*", "example.com"])
def test_non_loopback_bind_host_is_refused(
    tmp_path: pathlib.Path, bad_host: str
) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        ui.UiServer(tmp_path, host=bad_host, port=0)


def test_ipv6_loopback_bind_and_request(tmp_path: pathlib.Path) -> None:
    """`--host ::1` is advertised in docs and must bind `AF_INET6` cleanly."""
    import socket

    try:
        server = ui.UiServer(tmp_path, host="::1", port=0, quiet=True)
    except OSError as exc:
        pytest.skip(f"IPv6 loopback unavailable in this test environment: {exc}")

    assert server.address_family == socket.AF_INET6
    assert server.url.startswith("http://[::1]:")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("::1", server.server_port, timeout=5.0)
        try:
            conn.request(
                "GET",
                "/api/workspace",
                headers={
                    "Host": f"[::1]:{server.server_port}",
                    "X-Keymap-Token": server.csrf_token,
                },
            )
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert body["files"] == {"snapshots": []}
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)


def test_cli_ui_returns_1_on_non_loopback_host_or_missing_dir(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_ui.main(["--dir", str(tmp_path), "--host", "0.0.0.0", "--no-browser"]) == 1
    assert "non-loopback" in capsys.readouterr().err

    missing = tmp_path / "does-not-exist"
    assert cli_ui.main(["--dir", str(missing), "--no-browser"]) == 1
    assert "does not exist" in capsys.readouterr().err

    file_arg = tmp_path / "some-file.txt"
    file_arg.write_text("hi", encoding="utf-8")
    assert cli_ui.main(["--dir", str(file_arg), "--no-browser"]) == 1
    assert "not a directory" in capsys.readouterr().err

    assert cli_ui.main(["--dir", str(tmp_path), "-p", "99999", "--no-browser"]) == 1
    assert "invalid TCP port" in capsys.readouterr().err


def test_html_shell_contains_csrf_meta_csp_nonce_frame_guards_and_no_cors(
    tmp_path: pathlib.Path,
) -> None:
    with _running_ui(tmp_path) as server:
        status1, headers1, raw1 = _request(server, "GET", "/")
        assert status1 == 200
        assert headers1.get("x-frame-options") == "DENY"
        csp1 = headers1["content-security-policy"]
        assert not any(h.startswith("access-control-") for h in headers1)

        csp_nonce_match1 = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp1)
        assert csp_nonce_match1 is not None
        nonce1 = csp_nonce_match1.group(1)
        assert csp1 == ui._build_csp(nonce1)

        html1 = raw1.decode("utf-8")
        csrf_match = re.search(r'<meta name="keymap-csrf" content="([^"]+)">', html1)
        assert csrf_match is not None
        assert csrf_match.group(1) == server.csrf_token
        assert f'<script nonce="{nonce1}">' in html1
        assert 'id="btn-export-html"' in html1
        assert "btn-export-json" not in html1

        # Every GET / rotates the script nonce while keeping the process CSRF token.
        status2, headers2, raw2 = _request(server, "GET", "/")
        assert status2 == 200
        csp_nonce_match2 = re.search(
            r"script-src 'nonce-([A-Za-z0-9_-]+)'",
            headers2["content-security-policy"],
        )
        assert csp_nonce_match2 is not None
        nonce2 = csp_nonce_match2.group(1)
        assert nonce2 != nonce1
        assert f'<script nonce="{nonce2}">' in raw2.decode("utf-8")


def test_embedded_javascript_and_package_assets_are_valid(
    tmp_path: pathlib.Path,
) -> None:
    """Verify `_ui_assets` package-data files exist, are packaged, and parse cleanly."""
    for name in ("shell.html", "app.css", "app.js"):
        assert (ASSETS_DIR / name).is_file(), f"missing UI asset {name}"

    pyproject_raw = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for pattern in ("assets/*.html", "assets/*.css", "assets/*.js"):
        assert pattern in pyproject_raw, f"{pattern} missing from pyproject.toml package-data"

    with _running_ui(tmp_path) as server:
        status, _, raw = _request(server, "GET", "/")
        assert status == 200
        html = raw.decode("utf-8")
        script_match = re.search(
            r'<script nonce="[A-Za-z0-9_-]+">(.*)</script>', html, flags=re.DOTALL
        )
        assert script_match is not None
        js_source = script_match.group(1)
        assert r'lines.join("\n")' in js_source

        node = shutil.which("node")
        if node is not None:
            for label, target in (
                ("asset app.js", ASSETS_DIR / "app.js"),
                ("rendered inline script", tmp_path / "rendered.js"),
            ):
                if label == "rendered inline script":
                    target.write_text(js_source, encoding="utf-8")
                proc = subprocess.run(
                    [node, "--check", str(target)],
                    capture_output=True,
                    text=True,
                )
                assert proc.returncode == 0, f"{label} JS SyntaxError: {proc.stderr}"


def test_p0_quote_bearing_snapshot_filenames_and_attribute_escaping(
    tmp_path: pathlib.Path,
) -> None:
    """Regression test for P0 HTML attribute breakout via snapshot filenames or metadata."""
    ws_dir = tmp_path / "repo"
    snaps_dir = ws_dir / "snapshots"
    snaps_dir.mkdir(parents=True)
    corpus_text = CORPUS.read_text(encoding="utf-8")
    (snaps_dir / "clean.json").write_text(corpus_text, encoding="utf-8")

    # Attempt to plant a quote-bearing filename directly inside snapshots/ (e.g. via git clone).
    evil_name = "evil\" onmouseover=alert(1) x='.json"
    planted_evil = False
    try:
        (snaps_dir / evil_name).write_text(corpus_text, encoding="utf-8")
        planted_evil = True
    except OSError:
        # Windows NTFS forbids '"' in filenames natively.
        planted_evil = False

    assert not store_mod.SnapshotStore.is_safe_name(evil_name)
    assert not store_mod.SnapshotStore.is_safe_name("clean.json\n")
    assert store_mod.SnapshotStore.is_safe_name("sofle-20261115-175503.json")

    with _running_ui(ws_dir) as server:
        st_ws, _, raw_ws = _request(
            server,
            "GET",
            "/api/workspace",
            headers={"X-Keymap-Token": server.csrf_token},
        )
        assert st_ws == 200
        listed = json.loads(raw_ws.decode("utf-8"))["files"]["snapshots"]
        # Even when planted on POSIX, _scan_snapshots omits unsafe filenames.
        assert [item["name"] for item in listed] == ["clean.json"]

        if planted_evil:
            st_load, _ = _post_json(
                server, "/api/snapshots/load", {"path": f"snapshots/{evil_name}"}
            )
            assert st_load == 403

        # Hostile device.name / layer.name / imported label round-trips safely as JSON
        # data and is escaped cleanly in both inline SVG and standalone HTML exports.
        base_snap = _snapshot()
        hostile_layers = list(base_snap.layers)
        hostile_layers[0] = pb.Layer(
            id=hostile_layers[0].id,
            display_order=hostile_layers[0].display_order,
            name="BASE\"><img src=x onerror=alert(1)>'",
            bindings=hostile_layers[0].bindings,
        )
        hostile = pb.KeymapSnapshot(
            device=pb.Device(
                name='Sofle" onmouseover="alert(1)" data-x=\'<script>',
                id=base_snap.device.id,
            ),
            layout=base_snap.layout,
            behaviors=base_snap.behaviors,
            layers=hostile_layers,
        )
        st_imp, imp_body = _post_json(
            server,
            "/api/snapshots/load",
            {"raw_json": hostile.to_json(), "label": 'evil" onclick="alert(1).json'},
        )
        assert st_imp == 200
        assert imp_body["snapshot"]["device"]["name"] == hostile.device.name
        svg0 = imp_body["snapshot"]["layers"][0]["svg"]
        assert "<script>" not in svg0
        assert 'onerror=alert(1)>' not in svg0
        st_export, export_body = _post_json(
            server, "/api/render/html", {"raw_json": hostile.to_json()}
        )
        assert st_export == 200
        assert "<script" not in export_body["html"].lower()
        assert 'onerror=alert(1)>' not in export_body["html"]

    # Also verify app.js's esc() encodes &, <, >, ", and ' for attribute contexts.
    app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
    assert '"&quot;"' in app_js and '"&#39;"' in app_js
    node = shutil.which("node")
    if node is not None:
        check_script = (
            "const fs = require('fs');\n"
            f"const src = fs.readFileSync({json.dumps(str(ASSETS_DIR / 'app.js'))}, 'utf8');\n"
            "const m = src.match(/const _ESC_MAP = \\{[\\s\\S]*?function esc\\(str\\) \\{[\\s\\S]*?\\}/);\n"
            "if (!m) process.exit(2);\n"
            "const fn = new Function(m[0] + '; return esc;')();\n"
            "const out = fn(`a\"b'c<d>e&f`);\n"
            "if (out !== 'a&quot;b&#39;c&lt;d&gt;e&amp;f') {\n"
            "  console.error('unexpected esc output:', out);\n"
            "  process.exit(1);\n"
            "}\n"
        )
        proc = subprocess.run([node, "-e", check_script], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "forged_host",
    ["evil.example", "evil.example:80", "127.0.0.1:1"],
)
def test_dns_rebinding_host_headers_are_rejected(
    tmp_path: pathlib.Path, forged_host: str
) -> None:
    with _running_ui(tmp_path) as server:
        status_get, _, _ = _request(
            server, "GET", "/api/workspace", headers={"Host": forged_host}
        )
        assert status_get == 403

        status_post, _, _ = _request(
            server,
            "POST",
            "/api/keyboard/read",
            body=b"{}",
            headers={
                "Host": forged_host,
                "Content-Type": "application/json",
                "X-Keymap-Token": server.csrf_token,
            },
        )
        assert status_post == 403


def test_post_requires_valid_csrf_token_and_json_content_type(
    tmp_path: pathlib.Path,
) -> None:
    with _running_ui(tmp_path, connect_fn=lambda: _FakeSession(_snapshot())) as server:
        status_missing, _ = _post_json(server, "/api/keyboard/read", {}, token=None)
        assert status_missing == 403

        status_wrong, _ = _post_json(
            server, "/api/keyboard/read", {}, token="wrong-token"
        )
        assert status_wrong == 403

        # Non-ASCII X-Keymap-Token returns clean 403 instead of dropping connection.
        status_non_ascii, _, _ = _request(
            server,
            "POST",
            "/api/keyboard/read",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Keymap-Token": "tok-\u00e9",
            },
        )
        assert status_non_ascii == 403

        # GET /api/workspace also requires X-Keymap-Token.
        status_ws_no_tok, _, _ = _request(server, "GET", "/api/workspace")
        assert status_ws_no_tok == 403

        # Negative and oversized Content-Length headers return clean 400 / 413.
        status_neg_len, _, _ = _request(
            server,
            "POST",
            "/api/keyboard/read",
            body=b"",
            headers={
                "Content-Type": "application/json",
                "X-Keymap-Token": server.csrf_token,
                "Content-Length": "-1",
            },
        )
        assert status_neg_len == 400

        status_big_len, _, _ = _request(
            server,
            "POST",
            "/api/keyboard/read",
            body=b"",
            headers={
                "Content-Type": "application/json",
                "X-Keymap-Token": server.csrf_token,
                "Content-Length": str(ui._MAX_BODY_BYTES + 1),
            },
        )
        assert status_big_len == 413

        # Deeply nested JSON returns clean 400 instead of RecursionError socket drop.
        st_deep, _ = _post_json(
            server,
            "/api/snapshots/load",
            {"raw_json": "{" * 2000 + "}" * 2000},
        )
        assert st_deep == 400

        status_form, _, _ = _request(
            server,
            "POST",
            "/api/keyboard/read",
            body=b"{}",
            headers={
                "Content-Type": "text/plain",
                "X-Keymap-Token": server.csrf_token,
            },
        )
        assert status_form == 415


# --------------------------------------------------------------------------- #
# workspace path confinement
# --------------------------------------------------------------------------- #

def test_workspace_rejects_path_traversal_and_symlinks(tmp_path: pathlib.Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(CORPUS.read_text(encoding="utf-8"), encoding="utf-8")

    ws_dir = tmp_path / "repo"
    (ws_dir / "snapshots").mkdir(parents=True)
    (ws_dir / "snapshots" / "good.json").write_text(
        CORPUS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (ws_dir / "tests" / "data").mkdir(parents=True)
    (ws_dir / "tests" / "data" / "sofle-2026-11-10.json").write_text(
        CORPUS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (ws_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (ws_dir / "snapshots" / "escape.json").symlink_to(outside)

    with _running_ui(ws_dir) as server:
        status_ws, _, raw_ws = _request(
            server,
            "GET",
            "/api/workspace",
            headers={"X-Keymap-Token": server.csrf_token},
        )
        assert status_ws == 200
        ws_payload = json.loads(raw_ws.decode("utf-8"))["files"]
        # Only snapshots/ is exposed in the sidebar; tests/data and undo-snapshots are hidden.
        assert set(ws_payload.keys()) == {"snapshots"}
        listed = ws_payload["snapshots"]
        # The escaping symlink must not even appear in the directory listing.
        assert [item["name"] for item in listed] == ["good.json"]

        for bad_path in (
            "../outside.json",
            "snapshots/../pyproject.toml",
            "pyproject.toml",
            "tests/data/sofle-2026-11-10.json",
            "/etc/passwd",
            "snapshots/escape.json",
        ):
            st, body = _post_json(server, "/api/snapshots/load", {"path": bad_path})
            assert st == 403, f"{bad_path} returned {st}: {body}"

        st_missing, _ = _post_json(
            server, "/api/snapshots/load", {"path": "snapshots/missing.json"}
        )
        assert st_missing == 404

        ok_st, ok_body = _post_json(
            server, "/api/snapshots/load", {"path": "snapshots/good.json"}
        )
        assert ok_st == 200
        assert ok_body["snapshot"]["device"]["name"] == "Sofle"
        assert len(ok_body["snapshot"]["layers"]) == 6

        # Direct `_write_atomic` also enforces store confinement and refuses symlinks.
        with pytest.raises(store_mod.PathNotAllowed):
            server.store._write_atomic(outside, "{}")
        with pytest.raises(store_mod.PathNotAllowed):
            server.store._write_atomic(ws_dir / "snapshots" / "escape.json", "{}")


# --------------------------------------------------------------------------- #
# live read, backup, standalone HTML export, and offline diff
# --------------------------------------------------------------------------- #

def test_live_read_backup_and_locked_error_handling(tmp_path: pathlib.Path) -> None:
    snap = _snapshot()
    with _running_ui(tmp_path, connect_fn=lambda: _FakeSession(snap)) as server:
        # Read-only live read writes no files.
        st_read, read_body = _post_json(server, "/api/keyboard/read", {})
        assert st_read == 200
        assert read_body["snapshot"]["source"]["kind"] == "live"
        assert not (tmp_path / "snapshots").exists()

        # Unsafe backup filenames are rejected before touching hardware.
        for bad_name in ("../escape.json", "nested/keymap.json", ".hidden.json", "plain.txt"):
            st_bad, _ = _post_json(server, "/api/backup", {"filename": bad_name})
            assert st_bad == 400

        # Valid backup writes snapshots/keymap.json atomically.
        st_bak, bak_body = _post_json(
            server, "/api/backup", {"filename": "keymap.json", "include_serial": False}
        )
        assert st_bak == 200
        assert bak_body["saved_path"] == "snapshots/keymap.json"
        written = (tmp_path / "snapshots" / "keymap.json").read_text(encoding="utf-8")
        assert pb.KeymapSnapshot.from_json(written).device.name == "Sofle"

    # Now verify a locked keyboard leaves the existing backup untouched.
    def locked() -> Keyboard:
        raise KeyboardLocked("unlock ZMK Studio first")

    with _running_ui(tmp_path, connect_fn=locked) as server2:
        st_err, err_body = _post_json(
            server2, "/api/backup", {"filename": "keymap.json"}
        )
        assert st_err == 409
        assert "unlock ZMK Studio" in err_body["error"]
        assert (tmp_path / "snapshots" / "keymap.json").read_text(encoding="utf-8") == written


def test_timestamped_snapshot_backups_accumulate_newest_first_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    import datetime as dt

    calls = 0

    class _SteppingDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> _SteppingDateTime:
            nonlocal calls
            calls += 1
            return cls(2026, 11, 18, 12, calls, 0, tzinfo=tz)

    monkeypatch.setattr(live_keyboard.datetime, "datetime", _SteppingDateTime)

    with _running_ui(tmp_path, connect_fn=lambda: _FakeSession(_snapshot())) as server:
        st1, b1 = _post_json(server, "/api/backup", {})
        st2, b2 = _post_json(server, "/api/backup", {})
        assert st1 == 200
        assert st2 == 200
        assert b1["saved_path"] != b2["saved_path"]
        assert re.match(r"^snapshots/sofle-\d{8}-\d{6}\.json$", b1["saved_path"])
        assert re.match(
            r"^snapshots/sofle-\d{8}-\d{6}(-\d+)?\.json$", b2["saved_path"]
        )
        # Newest snapshot is sorted first in the Backup section listing.
        listed_paths = [item["rel_path"] for item in b2["files"]["snapshots"]]
        assert listed_paths == [b2["saved_path"], b1["saved_path"]]
        if sys.platform != "win32":
            mode = (tmp_path / b2["saved_path"]).stat().st_mode & 0o777
            assert mode == 0o600


def test_offline_diff_and_standalone_html_export(tmp_path: pathlib.Path) -> None:
    before = _snapshot()
    after = _with_flipped_param1(before, layer_idx=0, binding_idx=0)

    with _running_ui(tmp_path) as server:
        st_diff, diff_body = _post_json(
            server,
            "/api/diff/snapshots",
            {"before_json": before.to_json(), "after_json": after.to_json()},
        )
        assert st_diff == 200
        assert len(diff_body["proposal"]["changes"]) == 1
        assert 'class="key changed"' in diff_body["diff_layers"][0]["svg"]

        st_html, html_body = _post_json(
            server, "/api/render/html", {"raw_json": before.to_json()}
        )
        assert st_html == 200
        assert "<script" not in html_body["html"].lower()

        # Huge uint32 position in `/api/diff/snapshots` returns 409 immediately without OOM.
        raw_huge = json.loads(_snapshot().to_json())
        raw_huge["layers"][0]["bindings"][0]["position"] = 4_000_000_000
        huge_json = json.dumps(raw_huge)
        st_huge, huge_body = _post_json(
            server,
            "/api/diff/snapshots",
            {"before_json": huge_json, "after_json": after.to_json()},
        )
        assert st_huge == 409
        assert "position 4000000000" in huge_body["error"]


# --------------------------------------------------------------------------- #
# two-step restore: dry-run plan -> digest-checked commit -> pre-flight undo
# --------------------------------------------------------------------------- #

def test_restore_plan_works_with_strictly_read_only_keyboard_session(
    tmp_path: pathlib.Path,
) -> None:
    """`restore/plan` needs only read-only `Keyboard`, not `WritableKeyboard`."""
    want = _snapshot()
    backing = _FakeSession(_snapshot())
    _diverge(backing, layer_index=0, position=0)

    def open_read_only() -> Keyboard:
        session = Keyboard(backing._rpc)
        assert not isinstance(session, WritableKeyboard)
        return session

    with _running_ui(tmp_path, connect_fn=open_read_only) as server:
        st_plan, plan_body = _post_json(
            server, "/api/restore/plan", {"raw_json": want.to_json()}
        )
        assert st_plan == 200
        assert backing.writes == []
        assert len(plan_body["proposal"]["changes"]) == 1
        assert len(plan_body["plan_digest"]) == 64


def test_restore_plan_sends_zero_writes_and_commit_enforces_digest_and_device_guard(
    tmp_path: pathlib.Path,
) -> None:
    want = _snapshot()
    live_state = _snapshot()
    sessions_opened: list[_FakeSession] = []

    def open_session() -> _FakeSession:
        s = _FakeSession(live_state)
        _diverge(s, layer_index=0, position=0)
        sessions_opened.append(s)
        return s

    with _running_ui(tmp_path, connect_writable_fn=open_session) as server:
        # Step 1: dry-run preview sends zero writes.
        st_plan, plan_body = _post_json(
            server, "/api/restore/plan", {"raw_json": want.to_json()}
        )
        assert st_plan == 200
        assert sessions_opened[-1].writes == []
        assert len(plan_body["proposal"]["changes"]) == 1
        digest = plan_body["plan_digest"]
        assert 'class="key changed"' in plan_body["diff_layers"][0]["svg"]

        # Step 2a: stale/wrong plan_digest is refused before writing anything.
        st_stale, stale_body = _post_json(
            server,
            "/api/restore/commit",
            {
                "raw_json": want.to_json(),
                "plan_digest": "0" * 64,
                "force_different_device": False,
            },
        )
        assert st_stale == 409
        assert "changed since the preview" in stale_body["error"]
        assert sessions_opened[-1].writes == []

        # Step 2b: valid digest commits, writes pre-flight backup, and saves to flash.
        st_commit, commit_body = _post_json(
            server,
            "/api/restore/commit",
            {
                "raw_json": want.to_json(),
                "plan_digest": digest,
                "force_different_device": False,
            },
        )
        assert st_commit == 200
        assert sessions_opened[-1].writes == [
            "write:set_layer_binding",
            "write:save_changes",
        ]
        undo_rel = commit_body["undo_path"]
        assert undo_rel.startswith("undo-snapshots/before-restore-")
        assert (tmp_path / undo_rel).is_file()
        if sys.platform != "win32":
            assert ((tmp_path / undo_rel).stat().st_mode & 0o777) == 0o600
        assert set(commit_body["files"].keys()) == {"snapshots"}

        # The post-restore Undo button can still load the silent pre-restore undo snapshot.
        st_undo, undo_body = _post_json(
            server, "/api/snapshots/load", {"path": undo_rel}
        )
        assert st_undo == 200
        assert undo_body["snapshot"]["source"]["kind"] == "undo"
        assert undo_body["snapshot"]["source"]["path"] == undo_rel


def test_restore_commit_refuses_different_device_unless_forced(
    tmp_path: pathlib.Path,
) -> None:
    want = _with_device(_snapshot(), id="ffffffffffff")
    sessions: list[_FakeSession] = []

    def open_session() -> _FakeSession:
        s = _FakeSession(_snapshot())
        _diverge(s, layer_index=0, position=0)
        sessions.append(s)
        return s

    with _running_ui(tmp_path, connect_writable_fn=open_session) as server:
        st_plan, plan_body = _post_json(
            server, "/api/restore/plan", {"raw_json": want.to_json()}
        )
        assert st_plan == 200
        assert plan_body["proposal"]["identity_warning"]
        digest = plan_body["plan_digest"]

        # Without force_different_device, commit refuses with 0 writes.
        st_refused, _ = _post_json(
            server,
            "/api/restore/commit",
            {
                "raw_json": want.to_json(),
                "plan_digest": digest,
                "force_different_device": False,
            },
        )
        assert st_refused == 409
        assert sessions[-1].writes == []

        # With force_different_device=True, commit proceeds cleanly.
        st_forced, _ = _post_json(
            server,
            "/api/restore/commit",
            {
                "raw_json": want.to_json(),
                "plan_digest": digest,
                "force_different_device": True,
            },
        )
        assert st_forced == 200
        assert sessions[-1].writes[-1] == "write:save_changes"


def test_restore_commit_refuses_symlinked_undo_snapshots_dir_and_guards_malformed_worker_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Symlinked `undo-snapshots/` is refused before touching hardware, and malformed
    worker payloads return JSON 500 instead of dropping the HTTP connection.
    """
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws_dir / "undo-snapshots").symlink_to(outside, target_is_directory=True)

    want = _snapshot()
    sessions: list[_FakeSession] = []

    def open_session() -> _FakeSession:
        s = _FakeSession(_snapshot())
        _diverge(s, layer_index=0, position=0)
        sessions.append(s)
        return s

    with _running_ui(ws_dir, connect_writable_fn=open_session) as server:
        st_plan, plan_body = _post_json(
            server, "/api/restore/plan", {"raw_json": want.to_json()}
        )
        assert st_plan == 200
        assert len(sessions) == 1

        # Commit refuses symlinked undo-snapshots/ with 403 BEFORE opening WritableKeyboard.
        st_sym, sym_body = _post_json(
            server,
            "/api/restore/commit",
            {
                "raw_json": want.to_json(),
                "plan_digest": plan_body["plan_digest"],
                "force_different_device": False,
            },
        )
        assert st_sym == 403
        assert "symlink" in sym_body["error"]
        assert len(sessions) == 1  # no second session opened!

        # Malformed worker dict without expected keys returns clean HTTP 500 JSON, not a dropped socket.
        monkeypatch.setattr(_ui_hw_read, "execute_read", lambda **_: {"ok": True})
        st_bad_worker, bad_body = _post_json(
            server, "/api/restore/plan", {"raw_json": want.to_json()}
        )
        assert st_bad_worker == 500
        assert "malformed" in bad_body["error"]


# --------------------------------------------------------------------------- #
# isolated main-thread hardware subprocesses (CoreBluetooth + PyObjC regression)
# --------------------------------------------------------------------------- #

def test_hardware_workers_run_on_main_thread_in_fresh_processes_without_objc_collision() -> None:
    """Regression test: repeated BLE reads/plans from worker threads must run on main thread
    and never collide on PyObjC's process-global `class Delegate(NSObject)` table.
    """
    read_results: list[dict[str, typing.Any]] = []
    write_results: list[dict[str, typing.Any]] = []

    def run_checks() -> None:
        assert threading.current_thread() is not threading.main_thread()
        read_results.append(
            _ui_hw_read.invoke_subprocess(
                {"action": "self_check", "register_objc_delegate": True}
            )
        )
        write_results.append(
            _ui_hw.invoke_subprocess(
                {"action": "self_check", "register_objc_delegate": True}
            )
        )

    for _ in range(2):
        t = threading.Thread(target=run_checks)
        t.start()
        t.join(timeout=10.0)

    assert len(read_results) == 2
    assert len(write_results) == 2
    for group in (read_results, write_results):
        assert group[0]["ok"] is True
        assert group[1]["ok"] is True
        assert group[0]["is_main_thread"] is True
        assert group[1]["is_main_thread"] is True
        assert group[0]["pid"] != group[1]["pid"]


def test_p0_hardware_workers_ignore_hostile_shadow_modules_in_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Regression test: shadow `.py` files in a cloned repo CWD must never execute in workers."""
    evil_repo = tmp_path / "evilrepo"
    evil_repo.mkdir()
    marker = tmp_path / "PWNED.txt"
    payload = (
        f"import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('pwned', encoding='utf-8')\n"
        f"raise RuntimeError('PWNED')\n"
    )
    for mod_name in ("json.py", "secrets.py", "typing.py"):
        (evil_repo / mod_name).write_text(payload, encoding="utf-8")

    monkeypatch.chdir(evil_repo)
    res_read = _ui_hw_read.invoke_subprocess({"action": "self_check"})
    res_write = _ui_hw.invoke_subprocess({"action": "self_check"})
    assert res_read["ok"] is True
    assert res_write["ok"] is True
    assert not marker.exists()
