# Floor 7: `keymap_renderer` Design Decisions

This subpackage (`keymap_renderer/`) is the sole occupant of **Floor 7 (Presentation)** — turning a `KeymapSnapshot` (and optional `KeymapDiff` overlay from Floor 6) into deterministic plain text, single-layer SVG, inline UI `<svg>` fragments, or a self-contained static HTML page.

## Why It Is Structured This Way

1. **Single public noun class `KeymapRenderer` (in `keymap_renderer/`):**
   - Takes the Floor 6 Python `KeymapSnapshot` wrapper in `__init__(snapshot: KeymapSnapshot)` and pre-indexes `layer_names`, `{layer_id: {position: KeyBinding}}`, and SVG `viewBox` once on initialization.
   - Exposes `render()`, `to_text()`, `to_svg()`, `to_html()`, `layer_svg()`, `binding_summary()`, and `key_diff_summary()`.
2. **Private `_key_labels.py` (`KeyLabel`) hidden behind `KeymapRenderer`:**
   - Deriving compact human-friendly glyphs (`⌃⇧⌘4`, `SYM / ⏎`, `BT 0`) alongside raw ZMK tokens (`LC(LS(LG(N4)))`, `L4 / RET`) is internal to drawing keycaps and UI summaries.
   - Because `KeymapSnapshot` re-derives every `KeyBinding` from raw `(behavior_id, param1, param2)` at construction and `KeymapDiff.KeyDiff` carries non-optional `before_binding` / `after_binding` `KeyBinding`s, `_key_labels` formats friendly labels from `KeyBinding` (`arguments`, `behavior_name`, and canonical `decoded`) without accepting raw untrusted string expressions from callers; all SVG/HTML interpolation passes through a single C0-stripping `_esc()` chokepoint.
   - `_key_labels.friendly_keycode()` caps modifier-wrapper unwrapping (`_MAX_MOD_DEPTH = 8`) so adversarial input stays $O(1)$.
3. **Optional `diff: KeymapDiff | None = None` overlay on `layer_svg()` and `to_svg()` (no `KeymapDiffRenderer`):**
   - ~90% of board rendering (physical key geometry, thumb-cluster rotations, monospace shrink-to-fit `_fit`/`_wrap`, `_SVG_STYLE`, and unchanged key groups) is shared between normal and diff views; only the changed-cap face (`− before` / `+ after`) differs.
   - `layer_svg(layer, diff=diff)` and `to_svg(layer_id, diff=diff)` take `diff` alone — never a separate `baseline: KeymapSnapshot` parameter.

## Invariants to Preserve

- **Determinism:** Never read wall-clock time during rendering; the only timestamp shown is `snapshot.captured_iso`.
- **Inert standalone HTML (`to_html()`):** Must contain zero `<script>`, inline event attributes, or external network URLs. Layer switching and Friendly/Raw + All-Layers toggles rely strictly on `<input>` + CSS `:checked` sibling selectors.
- **Offline isolation (`tests/test_isolation.py`):** Zero I/O, zero `zmk_studio_client` runtime imports, and never imported by Floor 8 (`live_keyboard`, `restore_plan`) or `cli.restore`.
