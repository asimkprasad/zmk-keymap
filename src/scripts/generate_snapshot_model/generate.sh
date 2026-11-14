#!/usr/bin/env bash
# Regenerate the bindings for this tool's own archive schema.
#
# Only this one .proto is ours. The ZMK definitions live in zmk-studio-client,
# vendored and pinned there, because they are someone else's contract; this file
# describes a snapshot on disk and is ours to change.
set -euo pipefail
# Anchored by searching for the repo root rather than counting ../ hops, so this
# stays correct wherever the script is moved to.
root() {
  local dir; dir="$(cd "$(dirname "$0")" && pwd)"
  while [ "$dir" != "/" ]; do
    [ -f "$dir/pyproject.toml" ] && { echo "$dir"; return; }
    dir="$(dirname "$dir")"
  done
  echo "cannot find the repository root (no pyproject.toml above $0)" >&2
  exit 1
}
cd "$(root)"

# The snapshot schema imports the client's model, so protoc needs to resolve
# "zmk_studio_client/proto/keyboard.proto". Asked of the installed package rather
# than written as a sibling path: the client is a dependency, not a directory that
# happens to sit next to this one, and a checkout laid out differently - or a
# released wheel - would still have to work.
CLIENT_ROOT="$(python3 -c 'import pathlib, zmk_studio_client
print(pathlib.Path(zmk_studio_client.__file__).parent.parent)')"

# -Isrc, not -Isrc/zmk_keymap/proto, so the descriptor records this file as
# "zmk_keymap/proto/keymap_snapshot.proto" rather than a bare "keymap_snapshot.proto"
# that could collide in the process-wide default descriptor pool.
python3 -m grpc_tools.protoc -Isrc -I"$CLIENT_ROOT" \
  --python_out=src --pyi_out=src src/zmk_keymap/proto/keymap_snapshot.proto
echo "regenerated src/zmk_keymap/proto/keymap_snapshot_pb2.py{,i} - review the diff"
