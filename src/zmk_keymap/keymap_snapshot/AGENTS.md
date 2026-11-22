# Floor 6: `keymap_snapshot` Design Decisions

This subpackage (`keymap_snapshot/` and `../proto/keymap_snapshot.proto`) owns `KeymapSnapshot` on **Floor 6** — the pure in-memory archive domain model, parameter decoding, and canonical JSON serialization (alongside `../keymap_diff.py` for `KeymapDiff`).

## Why It Is Structured This Way

1. **Immutable Python `KeymapSnapshot` wrapper over private `_pb.KeymapSnapshot`:**
   - `protoc` (`upb`) generates sealed C-extension message classes that forbid Python subclassing and expose raw mutable submessages. Wrapping `_pb.KeymapSnapshot` in an immutable Python `KeymapSnapshot` class keeps `_pb2` private behind construction, serialization, identity, and diff methods (`KeymapSnapshot(...)`, `KeymapSnapshot.from_json(raw)`, `snap.to_json()`, `snap.sha256()`, `snap.diff(target)`), with zero mutator methods and deterministic `hash(snap)`.
   - Leaf submessages (`Layer`, `KeyBinding`, `BehaviorArgument`, `PhysicalLayout`, `Device`, `SnapshotMetadata`) are returned as defensive `CopyFrom` copies so caller mutations can never alter `self._pb` or invalidate cached `sha256()`.
   - Every construction path (including `from_json`) rejects duplicate `Behavior.id` entries and re-derives every `KeyBinding` (`behavior_name`, `decoded`, `arguments`) from `(table[behavior_id], param1, param2)` via `_decoder.decode_binding`, so untrusted JSON cannot display one binding string while restoring another `behavior_id`.
   - Live device capture (`read_keyboard`, `_device_id`, `InvalidLayout`, `UnsavedChanges`) and wall-clock `captured_at` stamping live in Floor 8 (`live_keyboard.py`), keeping Floor 6 completely deterministic and free of `zmk_studio_client` session shapes and duck-typed `_...Like` protocols.
2. **Domain nouns in `keymap_snapshot.proto`:**
   - **`SnapshotMetadata metadata`** *(replaced `Capture capture`)*: Provenance (`captured_at`, `tool_version`, `studio_messages_revision`).
   - **`KeyBinding`** *(replaced `Binding`)*: Avoids colliding with positional `zmk_studio_client.v1.Binding`.
   - **`KeyBinding.decoded` and `KeyBinding.arguments` of `BehaviorArgument` + `ArgumentKind`:** Floor 6 **decodes** raw 32-bit wire parameters against `Behavior.parameter_sets`; Floor 7 (`KeymapRenderer`) **renders**.
   - **Direct import of `zmk_studio_client.v1.Behavior`:** Reuses the client's behavior descriptor directly rather than maintaining a second copy of parameter-set metadata.
3. **Private `_decoder.py` returning protobuf `KeyBinding` directly:**
   - Decoding has no state or lifecycle of its own — only static ZMK HID usage and devicetree `&sigil` tables called during `KeymapSnapshot(...)` construction.

## Invariants to Preserve

- **Immutability:** `KeymapSnapshot` is a point-in-time record with zero mutator methods; callers in `src/` and `tests/` construct snapshots via `KeymapSnapshot(...)` or `KeymapSnapshot.from_json(...)` and never mutate returned submessages in place.
- **Schema-only isolation (`tests/test_isolation.py`):** Modules here may import `zmk_studio_client.proto.keyboard_pb2` (descriptors only), and must **never** import `zmk_studio_client` runtime sessions/transports or `zmk_studio_client.writable`.
- **Protobuf regeneration (`tests/test_packaging.py`):** Keep `proto/keymap_snapshot.proto` at `src/zmk_keymap/proto/` (preserving the `"zmk_keymap/proto/keymap_snapshot.proto"` descriptor name) and regenerate via `uv run --extra regen bash src/scripts/generate_snapshot_model/generate.sh`.
- **Package version lookup:** `_tool_version()` must query `__package__.split(".", 1)[0]` (`"zmk_keymap"`), never the subpackage name.
