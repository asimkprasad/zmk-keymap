"""End-to-end CLI entry point behavior and documented exit codes."""
from __future__ import annotations

import pathlib

import pytest

from zmk_keymap import cli, cli_render, cli_restore, from_json, model as pb, render, snapshot as builder, to_json
from zmk_studio_client import KeyboardLocked, NotReachable, model as client_model
from zmk_studio_client.writable import EditAbandoned

from test_restore import CORPUS, _FakeSession, _diverge, _snapshot


# --------------------------------------------------------------------------- #
# keymap-render
# --------------------------------------------------------------------------- #

def test_cli_render_suffixes_match_render_formats() -> None:
    """Tested rather than `assert`ed at import so `python -O` cannot strip it."""
    assert set(cli_render._SUFFIX) == set(render.FORMATS)


def test_cli_render_writes_html_svg_and_text(tmp_path: pathlib.Path) -> None:
    out_html = tmp_path / "keymap.html"
    assert cli_render.main([str(CORPUS), "-o", str(out_html)]) == 0
    html = out_html.read_text(encoding="utf-8")
    assert "<!doctype html>" in html.lower()
    assert "▽" in html

    out_svg = tmp_path / "layer.svg"
    assert cli_render.main([str(CORPUS), "-f", "svg", "--layer", "0", "-o", str(out_svg)]) == 0
    assert "<svg" in out_svg.read_text(encoding="utf-8")

    out_txt = tmp_path / "keymap.txt"
    assert cli_render.main([str(CORPUS), "-f", "text", "-o", str(out_txt)]) == 0
    assert "=== layer" in out_txt.read_text(encoding="utf-8")


def test_cli_render_rejects_malformed_snapshot_cleanly(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli_render.main([str(bad)])
    assert "not a keymap snapshot" in str(exc.value)


# --------------------------------------------------------------------------- #
# keymap-backup
# --------------------------------------------------------------------------- #

def test_cli_backup_writes_snapshot_and_leaves_existing_file_alone_on_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    dest = tmp_path / "keymap.json"
    monkeypatch.setattr(cli, "dump", lambda include_serial=False: _snapshot())
    assert cli.main(["-o", str(dest)]) == 0
    original = dest.read_text(encoding="utf-8")
    assert from_json(original).device.name == "Sofle"

    def locked(include_serial: bool = False) -> pb.KeymapSnapshot:
        raise KeyboardLocked("keyboard is locked")

    monkeypatch.setattr(cli, "dump", locked)
    assert cli.main(["-o", str(dest)]) == 1
    assert dest.read_text(encoding="utf-8") == original


def test_cli_backup_returns_1_not_a_traceback_on_invalid_active_layout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
) -> None:
    """Out-of-range active_index from firmware raises ValueError in snapshot.build."""
    dest = tmp_path / "keymap.json"
    dest.write_text("sentinel", encoding="utf-8")

    def bogus_layout(include_serial: bool = False) -> pb.KeymapSnapshot:
        bad_layouts = client_model.PhysicalLayouts(active_index=3)
        bad_layouts.layouts.add(name="Only")
        return builder.build(
            client_model.DeviceInfo(name="Sofle", serial_number=b"\x01"),
            client_model.Keymap(),
            bad_layouts,
            {},
            include_serial=include_serial,
        )

    monkeypatch.setattr(cli, "dump", bogus_layout)
    assert cli.main(["-o", str(dest)]) == 1
    assert "active layout 3 of 1" in capsys.readouterr().err
    assert dest.read_text(encoding="utf-8") == "sentinel"

    def unrelated_bug(include_serial: bool = False) -> pb.KeymapSnapshot:
        raise ValueError("internal bug unrelated to InvalidLayout")

    monkeypatch.setattr(cli, "dump", unrelated_bug)
    with pytest.raises(ValueError, match="internal bug"):
        cli.main(["-o", str(dest)])


# --------------------------------------------------------------------------- #
# keymap-restore: exit codes 0, 1, 2, 3 without uncaught tracebacks
# --------------------------------------------------------------------------- #

def test_cli_restore_returns_0_when_already_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session)
    assert cli_restore.main([str(CORPUS)]) == 0
    assert session._fake.closed


def test_cli_restore_returns_3_on_dry_run_with_changes_and_sends_no_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session)
    assert cli_restore.main([str(CORPUS)]) == 3
    assert session.writes == []
    assert session._fake.closed


def test_cli_restore_returns_0_when_committed_and_writes_preflight_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    want = _snapshot()
    session = _FakeSession(want)
    _diverge(session)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session)
    assert cli_restore.main([str(CORPUS), "--commit", "--backup-dir", str(tmp_path)]) == 0
    assert list(tmp_path.glob("before-restore-*.json"))
    assert session.writes[-1] == "write:save_changes"


def test_cli_restore_returns_1_not_a_traceback_when_a_behavior_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression test for §4.3: missing bound behaviors must exit 1 with a refusal."""
    want = _snapshot()
    bound_id = want.layers[0].bindings[0].behavior_id
    session = _FakeSession(want, drop_behavior_id=bound_id)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session)
    assert cli_restore.main([str(CORPUS), "--commit"]) == 1
    err = capsys.readouterr().err
    assert "refused:" in err
    assert "no behavior matching" in err
    assert session.writes == []


def test_cli_restore_returns_1_on_different_device_unless_forced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    want = _snapshot()
    want.device.id = "ffffffffffff"
    snap_path = tmp_path / "other.json"
    snap_path.write_text(to_json(want), encoding="utf-8")

    session = _FakeSession(want)
    _diverge(session)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session)
    assert cli_restore.main([str(snap_path), "--commit"]) == 1
    assert session.writes == []

    session2 = _FakeSession(want)
    _diverge(session2)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session2)
    assert cli_restore.main([
        str(snap_path), "--commit", "--force-different-device",
        "--backup-dir", str(tmp_path / "backups"),
    ]) == 0


def test_cli_restore_returns_1_when_keyboard_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable() -> _FakeSession:
        raise NotReachable("no keyboard found")

    monkeypatch.setattr(cli_restore, "connect_writable", unreachable)
    assert cli_restore.main([str(CORPUS)]) == 1


def test_cli_restore_returns_2_when_commit_fails_after_writes_begin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    want = _snapshot()
    session = _FakeSession(want, save_ok=False)
    _diverge(session)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session)
    assert cli_restore.main([str(CORPUS), "--commit", "--backup-dir", str(tmp_path)]) == 2
    assert session.writes[-1] == "write:discard_changes"


def test_cli_restore_returns_2_if_close_raises_edit_abandoned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If session.close() ever raises EditAbandoned, main() must not exit 0."""
    want = _snapshot()
    session = _FakeSession(want)

    def abandon_on_close() -> None:
        session._fake.closed = True
        raise EditAbandoned("2 binding(s) were queued and never settled")

    monkeypatch.setattr(session, "close", abandon_on_close)
    monkeypatch.setattr(cli_restore, "connect_writable", lambda: session)
    assert cli_restore.main([str(CORPUS)]) == 2
    assert "failed: 2 binding(s) were queued and never settled" in capsys.readouterr().err
