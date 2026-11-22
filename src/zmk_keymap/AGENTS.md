# `zmk_keymap` Architecture, Floor 6 `KeymapDiff` & Floor 8 Live Bridge Decisions

`zmk_keymap` is organized into strict capability floors above `zmk-studio-client` (Floors 0–5), using **noun-centered modules and classes** aligned with the domain objects they represent rather than verb/action modules or shallow pass-through wrappers.

## Package Floor Map

- **`__init__.py`:** Import-free (docstring + `from __future__ import annotations` only) so importing any submodule never pulls in unrelated floors (`KeymapRenderer` vs. `live_keyboard` / `RestorePlan`).
- **Schema (`proto/`):** `keymap_snapshot.proto` + generated `_pb2.py{,i}` (`zmk_keymap.v1`).
- **Floor 6 — Domain Model (`keymap_snapshot/` & `keymap_diff.py`):** Pure in-memory domain objects (`KeymapSnapshot` & `KeymapDiff`) with zero device or filesystem I/O — see [keymap_snapshot/AGENTS.md](./keymap_snapshot/AGENTS.md) and `KeymapDiff` below.
- **Floor 7 — Presentation (`keymap_renderer/`):** Pure offline visual formatter (`KeymapRenderer`: text, SVG, standalone HTML) — see [keymap_renderer/AGENTS.md](./keymap_renderer/AGENTS.md).
- **Floor 8 — Live Keyboard Bridge (`live_keyboard.py` & `restore_plan.py`):** Pure hardware RPC adapter (`KeymapSnapshot` ↔ `Keyboard` / `WritableKeyboard`, zero filesystem I/O, zero UI/renderer dependency).
- **Floor 9 — Local Storage & Web Workspace (`snapshot_store.py` & `ui/`):** Confined local snapshot/undo file storage (`SnapshotStore`) and loopback web workspace (`UiServer` + isolated read/write hardware workers — see [ui/AGENTS.md](./ui/AGENTS.md)).
- **Floor 10 — CLI (`cli/`):** CLI entry points (`backup.py`, `render.py`, `restore.py`, `ui.py` — see [cli/AGENTS.md](./cli/AGENTS.md)).

---

## Floor 6: `keymap_diff.py` (`KeymapDiff`) Design Decisions

1. **Part of Floor 6 (Domain Model), not Floor 7 (Presentation):**
   - `KeymapDiff` models the domain difference and behavior-ID translation between two keymaps and is required by headless hardware restore (`RestorePlan`) with zero UI/rendering involvement.
2. **Passive, immutable value object computed via `before.diff(target)`:**
   - Callers already hold `KeymapSnapshot` instances and invoke `before.diff(target) -> KeymapDiff` (`KeymapSnapshot -> KeymapDiff -> _pb`, strictly one-way with zero circular imports).
   - Because Floor 8 converts live `Keyboard` reads into `KeymapSnapshot` at the boundary and `ui/server.py` reconstructs `RestorePlan(live, target)` directly from `live_json` + `want`, `against_live()` (and its 3 `_Live...` protocols) and `to_dict()` / `from_dict()` were removed completely.
3. **Nested `KeymapDiff.BehaviorTranslator` and `KeymapDiff.KeyDiff`:**
   - ZMK firmware `behavior_id` integers are local to one firmware build; `BehaviorTranslator` rejects duplicate `behavior_id` entries unconditionally and matches bound behaviors by `Behavior.display_name` (raising `BehaviorMissing` for bound missing behaviors and `BehaviorAmbiguous` for bound duplicate display names, while tolerating unbound missing or duplicate behaviors).
   - Every `KeyDiff` carries the translated target `behavior_id`, raw parameters, `before` / `after` ZMK strings, and **non-optional** `before_binding` / `after_binding` (`KeyBinding`, `hash=False` so `KeymapDiff` stays immutable and hashable).
4. **Bounded sparse position validation:**
   - Snapshot position validation uses sparse `{position: KeyBinding}` indexing bounded by `len(layout.keys)` (or `_MAX_KEY_POSITIONS = 1024` when headless) so adversarial `uint32` positions fail immediately with `LayoutMismatch` without allocating, and sparse baseline gaps raise `LayoutMismatch` rather than fabricating zero bindings.

---

## Floor 8: `live_keyboard.py` & `restore_plan.py` Design Decisions

1. **Pure hardware RPC bridge (zero filesystem I/O, zero presentation coupling):**
   - Moving live-device capture out of `keymap_snapshot` (`from_device` + 8 fake `_...Like` protocols) and out of `zmk_keymap/__init__.py` into `live_keyboard.py` keeps Floor 6 100% device-free and `zmk_keymap/__init__.py` 100% side-effect-free.
   - Read/write capability split mirrors `zmk_studio_client` (`Keyboard` vs. `writable.WritableKeyboard`): `live_keyboard.py` exposes `read_keyboard(keyboard=None, *, include_serial=False, require_saved=False) -> KeymapSnapshot`, `InvalidLayout`, and `UnsavedChanges` (dropping `dump()` and keeping `_device_id` and `_snapshot_from_reads` private), and never imports `restore_plan` or `zmk_studio_client.writable`.
2. **`@dataclass(frozen=True) class RestorePlan(live, target)`:**
   - Single constructor `RestorePlan(live, target)` (no `from_keyboard` factory, no `Keyboard` / `client_model` import on `RestorePlan`): callers read `live = read_keyboard(session, require_saved=True)` and construct `RestorePlan(live, target)`.
   - Holds immutable `live: KeymapSnapshot`, `target: KeymapSnapshot`, and `diff: KeymapDiff = field(init=False)` directly (validated in `__post_init__` via `_check_layout` + `self.live.diff(self.target)`, with `device_name`, `identity_warning`, `is_empty`, and `digest()` derived from `(live, target)`).
   - `plan.apply(session, save_undo)` re-reads `current = read_keyboard(session, require_saved=True)` and verifies `RestorePlan(current, self.target).digest() == self.digest()` (`0` extra RPCs), persists the mandatory undo snapshot via `save_undo(current)` before `session.edit()`, stages `set_binding` in RAM, verifies `read_keyboard(session).diff(self.target).is_empty` before `edit.save()`, and calls `edit.discard()` on any doubt.

---

## Floor 9a: `snapshot_store.py` (`SnapshotStore`) Design Decisions

1. **Single regular service class `SnapshotStore` (folded `_fs.py` in, no wrapper dataclasses):**
   - `SnapshotStore` is a plain Python class (not a `@dataclass`), since it represents a confined filesystem repository rooted at `self.root` (`snapshots/` for user-saved snapshots, `undo-snapshots/` for automatic pre-restore undo captures) rather than a value object.
   - Once Floor 8 (`RestorePlan`) stopped doing direct disk I/O, `SnapshotStore` became the sole owner of atomic file writes and git-root discovery (`SnapshotStore.enclosing_git_root()`, `SnapshotStore.default_root()`, `SnapshotStore.is_safe_name()`, `SnapshotStore.write_undo_snapshot()`), allowing `_fs.py` to be deleted.
   - `store.list_snapshots() -> list[str]` returns confined relative paths (`"snapshots/<name>"`) without any UI JSON view-model coupling, `store.load(rel_path) -> KeymapSnapshot` returns `KeymapSnapshot` directly, `store.save(snapshot, filename=...) -> str` returns the canonical relative path (`"snapshots/<name>"`), and internal path/atomic helpers (`_resolve_readable`, `_write_atomic`) stay private.
   - Depends **only** on Floor 6 (`keymap_snapshot`) — zero hardware and zero HTTP/UI imports.
