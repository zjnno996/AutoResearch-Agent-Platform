"""Recoverable publication of a caller-selected bundle of binary files.

Only explicit destinations and the journal are written. Sources must be reviewed
snapshots below ``root``; policy about which scientific files belong together is
the caller's responsibility. A bundle is atomic per file, with rollback across
files after an interrupted publication. Concurrent publishers are unsupported;
unexpected file contents are treated as conflicts, never overwritten on recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_PREFIX = "bundle-"
_VERSION = "binary-bundle-v1"
_STATUSES = {"prepared", "applying", "committed", "rolled_back"}


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError(f"Invalid bundle-relative path: {value!r}")
    return value


@dataclass(frozen=True)
class _Root:
    path: Path
    device: int
    inode: int

    @classmethod
    def inspect(cls, path: Path) -> _Root:
        path = Path(path).absolute()
        if ".." in path.parts:
            raise ValueError("Bundle root must not contain traversal")
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError(f"Symlink in bundle root: {parent}")
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Bundle root must be an existing directory")
        return cls(path, info.st_dev, info.st_ino)

    def identity(self) -> dict:
        return {"path": str(self.path), "device": self.device, "inode": self.inode}

    def relative(self, path: Path) -> str:
        path = Path(path).absolute()
        try:
            relative = path.relative_to(self.path).as_posix()
        except ValueError as exc:
            raise ValueError(f"Path outside bundle root: {path}") from exc
        return _relative(relative)


@contextmanager
def _directory(root: _Root, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int | None]:
    """Traverse with directory descriptors so symlinks cannot redirect writes."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.path, flags)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (root.device, root.inode):
            raise ValueError("Bundle root identity changed")
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                os.mkdir(part, mode=0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(part, flags, dir_fd=fd)
            except (NotADirectoryError, OSError) as exc:
                raise ValueError(f"Invalid or symlinked bundle directory: {part}") from exc
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read_at(fd: int, name: str) -> bytes | None:
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"Invalid or symlinked bundle file: {name}") from exc
    with os.fdopen(source, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Bundle path is not a regular file: {name}")
        data = handle.read()
        after = os.fstat(handle.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError(f"Bundle file changed while reading: {name}")
    return data


def _read(root: _Root, relative: str) -> bytes | None:
    parts = Path(_relative(relative)).parts
    with _directory(root, parts[:-1]) as fd:
        return None if fd is None else _read_at(fd, parts[-1])


def _install(root: _Root, relative: str, data: bytes | None, expected: str | None) -> None:
    """Compare current bytes, then atomically replace or remove one destination."""
    parts = Path(_relative(relative)).parts
    with _directory(root, parts[:-1], create=data is not None) as fd:
        if fd is None:
            if expected is not None:
                raise ValueError(f"Bundle file changed before publication: {relative}")
            return
        if _digest(_read_at(fd, parts[-1])) != expected:
            raise ValueError(f"Bundle file changed before publication: {relative}")
        if data is None:
            if expected is not None:
                os.unlink(parts[-1], dir_fd=fd)
                os.fsync(fd)
            return
        temporary = f".{parts[-1]}.bundle-{uuid.uuid4().hex}.tmp"
        try:
            output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=fd)
            with os.fdopen(output, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if _digest(_read_at(fd, parts[-1])) != expected:
                raise ValueError(f"Bundle file changed before publication: {relative}")
            os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def _save(root: _Root, journal: str, manifest: dict) -> None:
    relative = f"{journal}/manifest.json"
    data = (json.dumps(manifest, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _install(root, relative, data, _digest(_read(root, relative)))


def _snapshot(journal: str, side: str, index: int) -> str:
    return f"{journal}/{side}/{index:06d}.bin"


def _validate_destinations(paths: list[str], journal_parent: str) -> None:
    for index, relative in enumerate(paths):
        _relative(relative)
        if relative == journal_parent or relative.startswith(journal_parent + "/"):
            raise ValueError("Bundle destination overlaps its journal")
        if journal_parent.startswith(relative + "/"):
            raise ValueError("Bundle destination contains its journal")
        for other in paths[:index]:
            if relative == other or relative.startswith(other + "/") or other.startswith(relative + "/"):
                raise ValueError("Bundle destinations overlap")


def _load(root: _Root, journal: str, journal_parent: str) -> dict:
    raw = _read(root, f"{journal}/manifest.json")
    if raw is None:
        raise ValueError(f"Incomplete bundle journal: {journal}")
    try:
        manifest = json.loads(raw)
        if (manifest["version"] != _VERSION or manifest["root"] != root.identity()
                or manifest["status"] not in _STATUSES or not isinstance(manifest["files"], list)
                or not manifest["files"] or ("metadata" in manifest and not isinstance(manifest["metadata"], dict))):
            raise ValueError("Invalid bundle journal identity or status")
        paths = [row["path"] for row in manifest["files"]]
        _validate_destinations(paths, journal_parent)
        for index, row in enumerate(manifest["files"]):
            for side in ("before", "after"):
                digest = row[f"{side}_sha256"]
                if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                    raise ValueError("Invalid bundle snapshot hash")
                data = _read(root, _snapshot(journal, side, index))
                if _digest(data) != digest:
                    raise ValueError(f"Missing or tampered bundle {side} snapshot: {row['path']}")
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid bundle manifest: {journal}") from exc
    return manifest


def _rollback(root: _Root, journal: str, journal_parent: str) -> None:
    manifest = _load(root, journal, journal_parent)
    if manifest["status"] in {"committed", "rolled_back"}:
        return
    current = [_digest(_read(root, row["path"])) for row in manifest["files"]]
    for row, digest in zip(manifest["files"], current):
        if digest not in (row["before_sha256"], row["after_sha256"]):
            raise ValueError(f"External edit prevents bundle rollback: {row['path']}")
    for index in reversed(range(len(manifest["files"]))):
        row = manifest["files"][index]
        if current[index] != row["before_sha256"]:
            _install(root, row["path"], _read(root, _snapshot(journal, "before", index)), current[index])
    manifest["status"] = "rolled_back"
    _save(root, journal, manifest)


def publish_bundle(root: Path, files: dict[str, Path | None], journal_parent: Path,
                   metadata: dict | None = None) -> Path:
    """Publish explicit relative destinations; return the retained journal path.

    Sources and journals must be below the existing root, without symlinks.
    ``None`` deletes a destination. Ordinary application errors attempt rollback;
    ``BaseException`` interruptions leave the journal for ``recover_bundles``.
    Missing destination directories may be created and remain empty after rollback.
    """
    checked_root = _Root.inspect(root)
    parent = checked_root.relative(journal_parent)
    if not isinstance(files, dict) or not files:
        raise ValueError("A bundle must contain an explicit nonempty file mapping")
    paths = list(files)
    _validate_destinations(paths, parent)
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Bundle metadata must be an object")
    copied_metadata = json.loads(json.dumps(metadata, allow_nan=False)) if metadata is not None else None
    # Preflight all destinations, sources, and existing journal ancestors before
    # creating even the journal. Captured bytes are the only bytes later installed.
    with _directory(checked_root, Path(parent).parts):
        pass
    before = [_read(checked_root, relative) for relative in paths]
    sources = [checked_root.relative(source) if source is not None else None for source in files.values()]
    if any(source in paths for source in sources if source is not None):
        raise ValueError("Bundle destinations must not overwrite reviewed source snapshots")
    after = []
    for source in sources:
        data = _read(checked_root, source) if source is not None else None
        if source is not None and data is None:
            raise ValueError(f"Missing reviewed bundle snapshot: {source}")
        after.append(data)
    journal = f"{parent}/{_PREFIX}{uuid.uuid4().hex}"
    with _directory(checked_root, Path(journal).parts, create=True):
        pass
    for index, (old, new) in enumerate(zip(before, after)):
        for side, data in (("before", old), ("after", new)):
            if data is not None:
                _install(checked_root, _snapshot(journal, side, index), data, None)
    for relative, data in zip(paths, before):
        if _digest(_read(checked_root, relative)) != _digest(data):
            raise ValueError(f"Bundle file changed during preparation: {relative}")
    for source, data in zip(sources, after):
        if source is not None and _digest(_read(checked_root, source)) != _digest(data):
            raise ValueError(f"Reviewed bundle snapshot changed during preparation: {source}")
    manifest = {"version": _VERSION, "root": checked_root.identity(), "status": "prepared",
                "files": [{"path": path, "before_sha256": _digest(old), "after_sha256": _digest(new)}
                          for path, old, new in zip(paths, before, after)]}
    if copied_metadata is not None:
        manifest["metadata"] = copied_metadata
    _save(checked_root, journal, manifest)
    try:
        for relative, data in zip(paths, before):
            if _digest(_read(checked_root, relative)) != _digest(data):
                raise ValueError(f"Bundle file changed before publication: {relative}")
        manifest["status"] = "applying"
        _save(checked_root, journal, manifest)
        for relative, old, new in zip(paths, before, after):
            _install(checked_root, relative, new, _digest(old))
        for relative, data in zip(paths, after):
            if _digest(_read(checked_root, relative)) != _digest(data):
                raise ValueError(f"Bundle file changed during publication: {relative}")
        manifest["status"] = "committed"
        _save(checked_root, journal, manifest)
    except Exception:
        try:
            _rollback(checked_root, journal, parent)
        except Exception as rollback_error:
            raise RuntimeError(f"Bundle rollback requires recovery: {checked_root.path / journal}") from rollback_error
        raise
    return checked_root.path / journal


def recover_bundles(root: Path, journal_parent: Path) -> None:
    """Validate journals, then roll back interrupted prepared/applying bundles.

    Committed and already rolled back bundles never modify destinations. A missing
    manifest means preparation never completed, so that journal has no live writes.
    Hash or identity failures and external edits raise before rollback starts.
    """
    checked_root = _Root.inspect(root)
    parent = checked_root.relative(journal_parent)
    with _directory(checked_root, Path(parent).parts) as fd:
        if fd is None:
            return
        names = sorted(name for name in os.listdir(fd) if name.startswith(_PREFIX))
    journals = []
    for name in names:
        journal = f"{parent}/{_relative(name)}"
        if _read(checked_root, f"{journal}/manifest.json") is not None:
            _load(checked_root, journal, parent)
            journals.append(journal)
    for journal in journals:
        _rollback(checked_root, journal, parent)
