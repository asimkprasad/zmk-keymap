"""Which modules can call or load which, checked statically and at runtime.

    keymap-backup    must not reach a write      - it captures, never restores
    keymap-render    must not call a keyboard    - it draws a file
    keymap-restore   may read and write          - CLI dry-run + --commit
    keymap-ui        may read and write          - localhost UI for backup + restore

**How the boundary sits across two repositories.** The protocol lives in
`zmk-studio-client`, a separate distribution, and that library ships three areas:

    zmk_studio_client             reading a keyboard (connect, Keyboard, Rpc, READS)
    zmk_studio_client.writable    the only route to changing one (plus .writes)
    zmk_studio_client.proto       the Behavior protobuf descriptor (keyboard_pb2)

**Source-level call path vs. runtime `sys.modules`.** Both matter, and they are
not the same thing:

1. *Module namespace / call path (checked statically over the AST below):*
   `keymap_snapshot.proto` imports `keyboard.proto` so `KeymapSnapshot` reuses
   `zmk_studio_client.v1.Behavior` rather than duplicating it, which means
   generated `proto/keymap_snapshot_pb2.py` imports `zmk_studio_client.proto.keyboard_pb2`.
   No module on the `renderer`, `cli_render`, `decoder` or `keymap_snapshot` path
   imports `zmk_studio_client` or any callable/session from it, so no function on the
   offline path holds a reference that can call `connect()` or open a port.

2. *Process `sys.modules` (checked in fresh subprocesses below):*
   `zmk_keymap/__init__.py` is intentionally empty, while importing
   `zmk_studio_client.proto.keyboard_pb2` (via `keymap_snapshot_pb2.py`) executes
   `zmk_studio_client/__init__.py`. Consequently, `zmk_studio_client`'s top-level
   read modules and `_pb` descriptors ARE loaded into `sys.modules` at import time. What is NOT loaded into
   `sys.modules` is the write surface (`zmk_studio_client.writable` /
   `zmk_studio_client.writes`, loaded only by `keymap-restore`) or the third-party
   OS transport drivers (`serial`, `objc`, `Foundation`, `CoreBluetooth`, which
   `zmk_studio_client.connection` lazy-imports inside `SerialConnection.__init__`
   and `find_bluetooth()`). Both halves of that runtime fact - plus positive
   non-vacuity controls - are pinned by subprocess tests below so documentation
   and reality cannot drift apart.

**No source module of this package imports `zmk_studio_client._pb`.** The client
hands back its own model, so naming a ZMK wire message in `src/` would be reaching
around the library's API into a private directory. Only the two below-`Keyboard`
seam tests (`test_dump_assembly.py` and `test_restore_plan.py`) import `zmk_studio_client._pb`,
and that list is enforced below as well.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = HERE / "src" / "zmk_keymap"
TESTS = HERE / "tests"

#: The client runtime namespace (connect, Keyboard, Rpc, READS, etc.).
CLIENT = "zmk_studio_client"

#: The client's protobuf descriptor subpackage, imported by keymap_snapshot_pb2.py.
#: Note: importing this at runtime still executes zmk_studio_client/__init__.py.
CLIENT_SCHEMA = f"{CLIENT}.proto"

#: Its generated ZMK wire bindings. Private to the client; the model is the API.
PRIVATE = f"{CLIENT}._pb"

#: The two modules in it that can change a keyboard.
#:
#:     zmk_studio_client.writes      the requests that mutate
#:     zmk_studio_client.writable    the object that sends them
#:
#: Both are named rather than just the door, so that a future rewrite of `writable`
#: that built its requests inline could not slip past a check that still reported a
#: clean closure.
WRITE_SURFACE = {f"{CLIENT}.writes", f"{CLIENT}.writable"}

#: Third-party hardware/OS driver packages lazy-imported inside
#: `zmk_studio_client.connection` (`SerialConnection.__init__`, `find_bluetooth`).
TRANSPORT_DRIVERS = {"serial", "objc", "Foundation", "CoreBluetooth"}


def _resolve_relative(enclosing_pkg: tuple[str, ...], level: int, module: str | None) -> str:
    """Resolve a relative import's base inside `zmk_keymap` to a dotted local name."""
    up = level - 1
    anchor = enclosing_pkg[: len(enclosing_pkg) - up] if up else enclosing_pkg
    parts = (*anchor, *(module.split(".") if module else ()))
    # Strip the leading "zmk_keymap" package name so local modules are spelled
    # relative to PACKAGE (e.g. "render", "proto.keymap_snapshot_pb2").
    local = parts[1:] if parts and parts[0] == "zmk_keymap" else parts
    return ".".join(local)


def _enclosing_package(path: pathlib.Path) -> tuple[str, ...]:
    try:
        rel = path.resolve().relative_to(PACKAGE.resolve())
    except ValueError:
        return ("zmk_keymap",)
    if rel.name == "__init__.py":
        return ("zmk_keymap", *rel.parent.parts)
    return ("zmk_keymap", *rel.parent.parts)


def _imports_at(path: pathlib.Path) -> set[str]:
    """Names imported by one file: local modules (dotted) and client modules in full."""
    found: set[str] = set()
    pkg = _enclosing_package(path)
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(pkg, node.level, node.module)
                if base:
                    found.add(base)
                    top = base.split(".")[0]
                    found.add(top)
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    found.add(candidate)
            elif node.module == CLIENT:
                for alias in node.names:
                    qualified = f"{CLIENT}.{alias.name}"
                    if qualified == CLIENT_SCHEMA:
                        found.add(CLIENT_SCHEMA)
                    else:
                        found.add(CLIENT)
                        found.add(qualified)
            elif node.module == CLIENT_SCHEMA or (
                node.module and node.module.startswith(f"{CLIENT_SCHEMA}.")
            ):
                found.add(CLIENT_SCHEMA)
            elif node.module and node.module.startswith(f"{CLIENT}."):
                found.add(CLIENT)
                found.add(node.module)
            elif node.module == "zmk_keymap":
                found.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith("zmk_keymap."):
                sub = node.module.removeprefix("zmk_keymap.")
                found.add(sub)
                found.add(sub.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                prefixes = [
                    ".".join(parts[:i]) for i in range(1, len(parts) + 1)
                ]
                if alias.name == CLIENT_SCHEMA or alias.name.startswith(
                    f"{CLIENT_SCHEMA}."
                ):
                    found.add(CLIENT_SCHEMA)
                elif alias.name == CLIENT or alias.name.startswith(f"{CLIENT}."):
                    found.update(prefixes)
                elif alias.name.startswith("zmk_keymap."):
                    for pref in prefixes[1:]:
                        sub = pref.removeprefix("zmk_keymap.")
                        found.add(sub)
    return found


def _module_path(module: str) -> pathlib.Path | None:
    """Where a module of this package lives (nested submodules included), or None."""
    rel = pathlib.Path(*module.split("."))
    for candidate in (PACKAGE / f"{rel}.py", PACKAGE / rel / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _imports(module: str) -> set[str]:
    path = _module_path(module)
    return _imports_at(path) if path else set()


def closure(start: str) -> set[str]:
    """Every name reachable from `start` by imports (`start` must exist on disk)."""
    assert _module_path(start) is not None, f"unknown module {start!r} in closure()"
    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        for name in _imports(current) - seen:
            seen.add(name)
            pending.append(name)
    return seen


# --------------------------------------------------------------------------- #
# the capture path cannot write
# --------------------------------------------------------------------------- #

def test_the_backup_command_cannot_reach_a_write() -> None:
    """keymap-backup reads a keyboard. It must have no route to changing one."""
    reached = closure("cli.backup")
    assert not (reached & WRITE_SURFACE), (
        f"zmk_keymap.cli.backup reaches {sorted(reached & WRITE_SURFACE)}; the capture "
        f"command must not be able to modify a keyboard. Reached: {sorted(reached)}")


def test_the_package_does_not_re_export_a_write() -> None:
    """`zmk_keymap/__init__.py` stays import-free so submodule imports have zero side effects."""
    imported = _imports("__init__")
    assert imported == set(), (
        f"zmk_keymap/__init__.py must stay free of submodule/client imports, "
        f"found: {sorted(imported)}"
    )


def test_floor_8_live_keyboard_and_restore_plan_are_pure_hardware_bridges() -> None:
    """`live_keyboard` is read-only; neither Floor 8 module touches `snapshot_store` or `keymap_renderer`."""
    live_reached = closure("live_keyboard")
    assert CLIENT in live_reached
    assert not (live_reached & WRITE_SURFACE)
    assert "keymap_renderer" not in live_reached
    assert "snapshot_store" not in live_reached

    plan_reached = closure("restore_plan")
    assert plan_reached & WRITE_SURFACE
    assert "keymap_renderer" not in plan_reached
    assert "snapshot_store" not in plan_reached


# --------------------------------------------------------------------------- #
# the offline presentation & keymap_snapshot paths bind no client runtime
# --------------------------------------------------------------------------- #

def test_render_binds_no_client_runtime() -> None:
    """`keymap_renderer` only touches `keymap_snapshot` (and `keyboard_pb2` descriptors)."""
    reached = closure("keymap_renderer")
    assert CLIENT not in reached, (
        f"zmk_keymap.keymap_renderer imports {CLIENT} runtime names; the presentation path "
        f"must have no call path to a connection. Reached: {sorted(reached)}")
    assert not (reached & WRITE_SURFACE)


def test_the_render_command_binds_no_client_runtime() -> None:
    """`keymap-render` takes a path, not a port: no client runtime in its AST closure."""
    reached = closure("cli.render")
    assert "keymap_renderer" in reached
    assert CLIENT not in reached, (
        f"zmk_keymap.cli.render imports {CLIENT} runtime names. Reached: {sorted(reached)}")
    assert not (reached & WRITE_SURFACE)


def test_decoding_cannot_reach_the_client_runtime() -> None:
    """`keymap_snapshot._decoder` constructs protobuf `KeyBinding`s; never imports client runtime."""
    reached = closure("keymap_snapshot._decoder")
    assert CLIENT not in reached
    assert CLIENT_SCHEMA in reached
    assert not (reached & WRITE_SURFACE)


def test_key_labels_keymap_diff_and_snapshot_store_bind_no_client_runtime() -> None:
    """`keymap_renderer._key_labels`, `keymap_diff`, `snapshot_store`, and `cli._common` bind no client runtime."""
    labels_reached = closure("keymap_renderer._key_labels")
    assert CLIENT not in labels_reached
    assert CLIENT_SCHEMA in labels_reached
    assert not (labels_reached & WRITE_SURFACE)

    diff_reached = closure("keymap_diff")
    assert "proto.keymap_snapshot_pb2" in diff_reached
    assert CLIENT not in diff_reached
    assert not (diff_reached & WRITE_SURFACE)

    store_reached = closure("snapshot_store")
    assert "keymap_snapshot" in store_reached
    assert CLIENT not in store_reached
    assert not (store_reached & WRITE_SURFACE)

    cli_common_reached = closure("cli._common")
    assert "keymap_snapshot" in cli_common_reached
    assert CLIENT not in cli_common_reached
    assert "keymap_renderer" not in cli_common_reached
    assert not (cli_common_reached & WRITE_SURFACE)


def test_ui_read_worker_cannot_reach_or_load_write_surface() -> None:
    """`keymap-ui`'s read/backup worker (`ui._hw_read`) has zero route to writes."""
    reached = closure("ui._hw_read")
    assert CLIENT in reached, "ui._hw_read must reach the read client (non-vacuity)"
    assert not (reached & WRITE_SURFACE), (
        f"ui._hw_read reaches write surface: {sorted(reached & WRITE_SURFACE)}"
    )

    loaded = _loaded_modules("import zmk_keymap.ui._hw_read")
    assert "zmk_studio_client" in loaded
    assert not (loaded & WRITE_SURFACE), (
        f"ui._hw_read loaded write surface at runtime: {sorted(loaded & WRITE_SURFACE)}"
    )
    assert not (loaded & TRANSPORT_DRIVERS), (
        f"ui._hw_read eagerly loaded transport drivers: {sorted(loaded & TRANSPORT_DRIVERS)}"
    )


def test_keymap_snapshot_binds_only_the_client_behavior_descriptor() -> None:
    """`zmk_keymap.keymap_snapshot` references `zmk_studio_client.proto`, not client callables."""
    reached = closure("keymap_snapshot")
    assert CLIENT not in reached
    assert CLIENT_SCHEMA in reached
    assert not (reached & WRITE_SURFACE)


# --------------------------------------------------------------------------- #
# the restore command is the only one that may write
# --------------------------------------------------------------------------- #

def test_the_restore_command_reaches_the_write_path() -> None:
    """The positive half. Without it, 'only restore can write' is unproven."""
    reached = closure("cli.restore")
    assert reached & WRITE_SURFACE, (
        "zmk_keymap.cli.restore is the restore command and must reach the write "
        f"path; if it does not, the import walker is not seeing real imports. "
        f"Reached: {sorted(reached)}")


def test_the_restore_command_does_not_draw() -> None:
    """Rendering is a separate command; restore has no business importing it."""
    assert "keymap_renderer" not in closure("cli.restore")
    assert "keymap_renderer._key_labels" not in closure("cli.restore")


def _declared_commands() -> dict[str, str]:
    """Parse `[project.scripts]` from `pyproject.toml` -> `{local_module: script_name}`."""
    raw = (HERE / "pyproject.toml").read_text(encoding="utf-8")
    in_scripts = False
    out: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_scripts = stripped == "[project.scripts]"
            continue
        if in_scripts and "=" in stripped and not stripped.startswith("#"):
            match = re.match(
                r'^([A-Za-z0-9_-]+)\s*=\s*"zmk_keymap\.([A-Za-z0-9_.]+):', stripped
            )
            if match:
                script_name, mod_name = match.groups()
                out[mod_name] = script_name
    assert out, "failed to parse [project.scripts] from pyproject.toml"
    return out


def test_only_restore_and_ui_commands_can_write() -> None:
    """Every declared CLI entry point is classified; backup and render cannot write."""
    commands = _declared_commands()
    assert set(commands.values()) == {
        "keymap-backup",
        "keymap-render",
        "keymap-restore",
        "keymap-ui",
    }
    writers = {
        name for module, name in commands.items() if closure(module) & WRITE_SURFACE
    }
    assert writers == {"keymap-restore", "keymap-ui"}, (
        f"commands able to modify a keyboard: {sorted(writers)}"
    )


# --------------------------------------------------------------------------- #
# the walker itself - without these, every test above could pass vacuously
# --------------------------------------------------------------------------- #

def test_the_check_would_notice_the_client_at_all() -> None:
    """A typo in the walker would make the renderer tests pass for nothing."""
    assert CLIENT in closure("cli.backup"), (
        "zmk_keymap.cli.backup is the capture command and must reach the client; if it "
        "does not, the import walker is not seeing real imports")


def test_the_walker_traverses_nested_submodules() -> None:
    """Nested packages like `proto.keymap_snapshot_pb2` must not be blind spots."""
    reached = closure("keymap_renderer")
    assert "proto.keymap_snapshot_pb2" in reached
    assert CLIENT_SCHEMA in reached


@pytest.mark.parametrize("source,expected", [
    ("from zmk_studio_client.writable import connect_writable",
     {"zmk_studio_client", "zmk_studio_client.writable"}),
    ("from zmk_studio_client import writable",
     {"zmk_studio_client", "zmk_studio_client.writable"}),
    ("import zmk_studio_client.writable",
     {"zmk_studio_client", "zmk_studio_client.writable"}),
    ("from zmk_studio_client import connect",
     {"zmk_studio_client", "zmk_studio_client.connect"}),
    ("import zmk_studio_client", {"zmk_studio_client"}),
    ("from zmk_studio_client.proto import keyboard_pb2", {"zmk_studio_client.proto"}),
    ("from . import _decoder", {"_decoder"}),
    ("from .keymap_renderer import KeymapRenderer",
     {"keymap_renderer", "keymap_renderer.KeymapRenderer"}),
    ("from .proto.keymap_snapshot_pb2 import KeyBinding",
     {"proto", "proto.keymap_snapshot_pb2", "proto.keymap_snapshot_pb2.KeyBinding"}),
    ("import json", set()),
    ("from __future__ import annotations", set()),
])
def test_the_walker_catches_every_import_syntax(
    tmp_path: pathlib.Path,
    source: str,
    expected: set[str],
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source + "\n", encoding="utf-8")
    assert _imports_at(probe) == expected


# --------------------------------------------------------------------------- #
# runtime sys.modules: pin what Python package __init__.py actually loads
# --------------------------------------------------------------------------- #

def _loaded_modules(import_stmt: str) -> set[str]:
    """Run `import_stmt` in a clean interpreter and return all `sys.modules` keys."""
    code = (
        f"{import_stmt}\n"
        "import json, sys\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(json.loads(proc.stdout))


def test_runtime_sys_modules_on_cli_render_matches_documented_boundary() -> None:
    """Pin both halves of the runtime truth when `keymap-render` starts up.

    1. `zmk_keymap/__init__.py` is empty, while `keymap_snapshot_pb2.py` imports
       `zmk_studio_client.proto.keyboard_pb2` (which executes `zmk_studio_client/__init__.py`),
       loading `zmk_studio_client`'s read package into `sys.modules`.
    2. Neither the write surface (`zmk_studio_client.writable` / `.writes`) nor the
       lazy third-party transport drivers (`serial`, `objc`, `Foundation`,
       `CoreBluetooth`) are loaded.
    """
    loaded = _loaded_modules("import zmk_keymap.cli.render")
    assert "zmk_studio_client" in loaded
    assert "zmk_studio_client.connection" in loaded
    assert "zmk_studio_client.rpc" in loaded
    assert not (loaded & WRITE_SURFACE), f"render loaded write surface: {sorted(loaded & WRITE_SURFACE)}"
    assert not (loaded & TRANSPORT_DRIVERS), (
        f"render eagerly loaded transport drivers: {sorted(loaded & TRANSPORT_DRIVERS)}"
    )


def test_runtime_sys_modules_on_cli_backup_never_loads_write_surface() -> None:
    loaded = _loaded_modules("import zmk_keymap.cli.backup")
    assert "zmk_studio_client" in loaded
    assert not (loaded & WRITE_SURFACE), f"backup loaded write surface: {sorted(loaded & WRITE_SURFACE)}"
    assert not (loaded & TRANSPORT_DRIVERS), (
        f"backup eagerly loaded transport drivers at import: {sorted(loaded & TRANSPORT_DRIVERS)}"
    )


def test_runtime_sys_modules_on_cli_restore_loads_write_surface() -> None:
    """Positive runtime twin: `keymap-restore` really does load `writable` and `writes`."""
    loaded = _loaded_modules("import zmk_keymap.cli.restore")
    assert WRITE_SURFACE <= loaded, f"restore missed write surface: {sorted(WRITE_SURFACE - loaded)}"
    assert not (loaded & TRANSPORT_DRIVERS), (
        f"restore eagerly loaded transport drivers at import: {sorted(loaded & TRANSPORT_DRIVERS)}"
    )


def test_runtime_sys_modules_on_cli_ui_loads_write_surface_without_drivers() -> None:
    """`keymap-ui` loads the write surface for restore endpoints, but no OS drivers at import."""
    loaded = _loaded_modules("import zmk_keymap.cli.ui")
    assert WRITE_SURFACE <= loaded, f"ui missed write surface: {sorted(WRITE_SURFACE - loaded)}"
    assert not (loaded & TRANSPORT_DRIVERS), (
        f"ui eagerly loaded transport drivers at import: {sorted(loaded & TRANSPORT_DRIVERS)}"
    )


def test_runtime_transport_driver_check_is_non_vacuous() -> None:
    """Positive control: `_loaded_modules` actually observes the real driver names."""
    with_serial = _loaded_modules("import serial")
    assert "serial" in (with_serial & TRANSPORT_DRIVERS)
    if sys.platform == "darwin":
        with_ble = _loaded_modules("import objc, Foundation, CoreBluetooth")
        assert {"objc", "Foundation", "CoreBluetooth"} <= (with_ble & TRANSPORT_DRIVERS)


# --------------------------------------------------------------------------- #
# the tool does not reach around the library's API
# --------------------------------------------------------------------------- #

def _package_modules() -> list[pathlib.Path]:
    """Every source file shipped in this package, generated code included."""
    return sorted(PACKAGE.rglob("*.py"))


def test_no_module_of_this_package_reaches_the_clients_zmk_bindings() -> None:
    """The claim the two-repo split rests on: nothing in src/ names a ZMK wire message."""
    modules = _package_modules()
    assert modules
    offenders = {
        path.relative_to(PACKAGE).as_posix(): sorted(_imports_at(path) & {PRIVATE})
        for path in modules
        if PRIVATE in _imports_at(path)
    }
    assert offenders == {}, (
        f"these modules import the client's private ZMK bindings: {offenders}. "
        f"Use zmk_studio_client.model instead - that is what it is for.")


def test_only_seam_tests_import_the_clients_zmk_bindings() -> None:
    """In tests/, only the two below-Keyboard seam fakes may speak ZMK wire messages."""
    test_files = sorted(TESTS.rglob("*.py"))
    assert test_files
    importers = {
        path.relative_to(TESTS).as_posix()
        for path in test_files
        if PRIVATE in _imports_at(path)
    }
    assert importers == {"test_dump_assembly.py", "test_restore_plan.py"}, (
        f"unexpected test files importing {PRIVATE}: {sorted(importers)}. "
        f"Tests above Keyboard/WritableKeyboard must use zmk_studio_client.model.")


def test_the_generated_snapshot_bindings_import_the_model_not_the_bindings() -> None:
    """Positive half: the schema really does import the library's message."""
    generated = PACKAGE / "proto" / "keymap_snapshot_pb2.py"
    source = generated.read_text(encoding="utf-8")
    assert "zmk_studio_client.proto" in source, (
        "keymap_snapshot_pb2.py no longer imports the client's model; the snapshot "
        "schema has gone back to declaring its own copy of the behavior table")
    assert "zmk_studio_client._pb" not in source


def test_the_check_would_notice_a_private_import(tmp_path: pathlib.Path) -> None:
    """Non-vacuity, in each syntax a violation could be written in (plus unknown module guard)."""
    for source in (
        "from zmk_studio_client._pb import keymap_pb2",
        "from zmk_studio_client import _pb",
        "import zmk_studio_client._pb",
        "import zmk_studio_client._pb.keymap_pb2",
        "import zmk_studio_client._pb.keyboard_pb2 as kb",
    ):
        probe = tmp_path / "probe.py"
        probe.write_text(source + "\n", encoding="utf-8")
        assert PRIVATE in _imports_at(probe), source

    with pytest.raises(AssertionError, match="unknown module"):
        closure("nonexistent_renamed_module")
