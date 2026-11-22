"""Confined local snapshot store (`snapshots/` and `undo-snapshots/`).

Nothing here knows about HTTP status codes, browser routes, or `zmk_studio_client`
runtime sessions. `SnapshotStore` jails all reads, directory listings, and atomic
saves inside one root directory:

1. **Confinement:** Only `snapshots/` and `undo-snapshots/` under `root` may be
   read, and only `snapshots/` may be written via `save()`. Path traversal (`..`),
   absolute paths, NUL bytes, and symlinks resolving outside their allowed directory
   are rejected (`PathNotAllowed`).
2. **Filename allowlist:** Every snapshot basename must match
   `SnapshotStore.is_safe_name` (`^[A-Za-z0-9][A-Za-z0-9._-]*\\.json$`).
3. **Atomic replacement:** Writes go to a sibling `0600` temp file (`mkstemp`) and
   swap into place via `os.replace`, so a failed write never truncates an existing
   snapshot and captures stay owner-private on disk.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import re
import tempfile

from . import keymap_snapshot as pb

__all__ = [
    "InvalidSnapshotName",
    "PathNotAllowed",
    "SnapshotNotFound",
    "SnapshotStore",
    "SnapshotStoreError",
]

_SAFE_FILENAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\.json\Z")
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024  # 4 MiB cap on snapshot files read from disk


class SnapshotStoreError(Exception):
    """Base error for confined `SnapshotStore` operations."""


class PathNotAllowed(SnapshotStoreError):
    """Path escapes `SnapshotStore` confinement, traverses '..', or targets a symlink."""


class InvalidSnapshotName(SnapshotStoreError):
    """Explicit backup filename is not a safe plain `.json` basename."""


class SnapshotNotFound(SnapshotStoreError):
    """Requested snapshot file does not exist."""


def _atomic_write_text(target: pathlib.Path, content: str) -> None:
    """Write `content` to `target` via sibling `0600` temp file + `os.replace`, refusing symlinks."""
    if target.parent.is_symlink():
        raise PathNotAllowed(
            f"refusing to write into symlinked directory: {target.parent}"
        )
    if target.is_symlink():
        raise PathNotAllowed(f"refusing to overwrite symlink: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise PathNotAllowed(f"refusing to overwrite symlink: {target}")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class SnapshotStore:
    """Confined file store rooted at a single local directory."""

    def __init__(self, root: pathlib.Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> pathlib.Path:
        return self._root

    @staticmethod
    def is_safe_name(filename: str) -> bool:
        """True only for plain `.json` basenames using `[A-Za-z0-9._-]` (no leading dot)."""
        return bool(_SAFE_FILENAME_RE.match(filename))

    @staticmethod
    def enclosing_git_root(start: pathlib.Path | None = None) -> pathlib.Path | None:
        """Return the enclosing git repository or worktree root, or `None` if outside git."""
        directory = (start or pathlib.Path.cwd()).resolve()
        while True:
            # `.git` is a directory in a standard clone and a regular file in a git worktree.
            if (directory / ".git").exists():
                return directory
            if directory.parent == directory:
                return None
            directory = directory.parent

    @classmethod
    def default_root(cls, start: pathlib.Path | None = None) -> pathlib.Path:
        """Enclosing git repository/worktree root if present, else the current directory."""
        current = (start or pathlib.Path.cwd()).resolve()
        return cls.enclosing_git_root(current) or current

    @property
    def snapshots_dir(self) -> pathlib.Path:
        return self.root / "snapshots"

    @property
    def undo_snapshots_dir(self) -> pathlib.Path:
        return self.root / "undo-snapshots"

    def ensure_undo_snapshots_dir(self) -> pathlib.Path:
        """Create and return `undo-snapshots/`, refusing symlinks escaping `root`."""
        if self.undo_snapshots_dir.is_symlink():
            raise PathNotAllowed("undo-snapshots/ may not be a symlink")
        self.undo_snapshots_dir.mkdir(parents=True, exist_ok=True)
        resolved = self.undo_snapshots_dir.resolve()
        if not resolved.is_relative_to(self.root):
            raise PathNotAllowed("undo-snapshots/ escapes snapshot store")
        return resolved

    def list_snapshots(self) -> list[str]:
        """Return confined relative paths (`snapshots/<name>`) in `snapshots/`, sorted by name."""
        directory = self.snapshots_dir
        if not directory.is_dir():
            return []
        allowed_root = directory.resolve()
        if not allowed_root.is_relative_to(self.root):
            return []

        paths: list[str] = []
        for candidate in sorted(directory.glob("*.json"), key=lambda p: p.name):
            if not self.is_safe_name(candidate.name):
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_file() or not resolved.is_relative_to(allowed_root):
                continue
            paths.append(f"snapshots/{candidate.name}")
        return paths

    def _resolve_readable(self, rel_path: str) -> pathlib.Path:
        """Validate `rel_path` stays inside `snapshots/` or `undo-snapshots/`."""
        cleaned = rel_path.strip().replace("\\", "/")
        if not cleaned or cleaned.startswith("/") or "\x00" in cleaned:
            raise PathNotAllowed("invalid snapshot path")

        parts = pathlib.PurePosixPath(cleaned).parts
        if ".." in parts or "." in parts:
            raise PathNotAllowed("path traversal is not allowed")

        allowed_dir: pathlib.Path
        if len(parts) == 2 and parts[0] == "snapshots":
            allowed_dir = self.snapshots_dir
        elif len(parts) == 2 and parts[0] == "undo-snapshots":
            allowed_dir = self.undo_snapshots_dir
        else:
            raise PathNotAllowed(
                "snapshot path must be in snapshots/ or undo-snapshots/"
            )

        filename = parts[-1]
        if not self.is_safe_name(filename):
            raise PathNotAllowed("snapshot filename must be a safe .json name")

        allowed_resolved = allowed_dir.resolve()
        if not allowed_resolved.is_relative_to(self.root):
            raise PathNotAllowed("snapshot directory escapes snapshot store")

        candidate = (allowed_dir / filename).resolve()
        if not candidate.is_relative_to(allowed_resolved):
            raise PathNotAllowed("symlink escapes snapshot directory")
        if not candidate.is_file():
            raise SnapshotNotFound(f"snapshot not found: {cleaned}")

        return candidate

    def load(self, rel_path: str) -> pb.KeymapSnapshot:
        """Read and parse a `KeymapSnapshot` from `snapshots/` or `undo-snapshots/`."""
        file_path = self._resolve_readable(rel_path)
        size = file_path.stat().st_size
        if size > _MAX_SNAPSHOT_BYTES:
            raise ValueError(
                f"snapshot exceeds {_MAX_SNAPSHOT_BYTES} bytes ({size} bytes)"
            )
        return pb.KeymapSnapshot.from_json(file_path.read_text(encoding="utf-8"))

    def validate_snapshot_name(self, filename: str) -> str:
        """Validate an explicit destination filename inside `snapshots/` before hardware I/O."""
        cleaned = filename.strip()
        if not self.is_safe_name(cleaned):
            raise InvalidSnapshotName(
                "snapshot filename must be a plain .json filename "
                "(letters, digits, '.', '_', '-')"
            )
        return cleaned

    def _resolve_snapshot_target(self, filename: str) -> tuple[pathlib.Path, str]:
        cleaned = self.validate_snapshot_name(filename)
        if self.snapshots_dir.is_symlink():
            raise PathNotAllowed("snapshots/ directory may not be a symlink")
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshots_resolved = self.snapshots_dir.resolve()
        if not snapshots_resolved.is_relative_to(self.root):
            raise PathNotAllowed("snapshots/ directory escapes snapshot store")

        target = self.snapshots_dir / cleaned
        if target.is_symlink():
            raise PathNotAllowed("refusing to overwrite a symlink in snapshots/")
        if not target.resolve().is_relative_to(snapshots_resolved):
            raise PathNotAllowed("snapshot target escapes snapshots/")
        return target, f"snapshots/{cleaned}"

    def _allocate_snapshot_target(self, device_name: str) -> tuple[pathlib.Path, str]:
        slug = re.sub(r"[^a-z0-9]+", "-", device_name.lower()).strip("-") or "keymap"
        stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        base = f"{slug}-{stamp}"
        candidate_name = f"{base}.json"
        suffix = 2
        while (
            (self.snapshots_dir / candidate_name).exists()
            and not (self.snapshots_dir / candidate_name).is_symlink()
        ):
            candidate_name = f"{base}-{suffix}.json"
            suffix += 1
        return self._resolve_snapshot_target(candidate_name)

    def _write_atomic(self, target: pathlib.Path, content: str) -> None:
        """Write `content` to `target` inside this store atomically, refusing symlinks."""
        if target.parent.is_symlink() or target.is_symlink():
            raise PathNotAllowed("refusing to write through a symlink in snapshot store")
        target.parent.mkdir(parents=True, exist_ok=True)
        parent_resolved = target.parent.resolve()
        if not parent_resolved.is_relative_to(self.root):
            raise PathNotAllowed("write target escapes snapshot store")
        if not target.resolve().is_relative_to(parent_resolved):
            raise PathNotAllowed("write target escapes parent directory")
        _atomic_write_text(target, content)

    def save(
        self,
        snapshot: pb.KeymapSnapshot,
        *,
        filename: str | None = None,
    ) -> str:
        """Atomically save `snapshot` into `snapshots/` and return canonical `rel_path`."""
        target, rel_posix = (
            self._resolve_snapshot_target(filename)
            if filename
            else self._allocate_snapshot_target(snapshot.device.name)
        )
        self._write_atomic(target, snapshot.to_json())
        return rel_posix

    @staticmethod
    def write_snapshot(
        target: pathlib.Path,
        snapshot: pb.KeymapSnapshot,
    ) -> None:
        """Atomically write `snapshot` JSON to `target`, refusing symlinks."""
        _atomic_write_text(target, snapshot.to_json())

    @staticmethod
    def write_undo_snapshot(
        undo_dir: pathlib.Path,
        snapshot: pb.KeymapSnapshot,
    ) -> pathlib.Path:
        """Atomically write `snapshot` to `undo_dir/before-restore-<UTC>.json`, refusing symlinks."""
        if undo_dir.is_symlink():
            raise PathNotAllowed(
                f"refusing to write undo snapshot into symlinked directory "
                f"{undo_dir}"
            )
        undo_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"before-restore-{stamp}"
        path = undo_dir / f"{base}.json"
        suffix = 2
        while path.exists() and not path.is_symlink():
            path = undo_dir / f"{base}-{suffix}.json"
            suffix += 1
        _atomic_write_text(path, snapshot.to_json())
        return path
