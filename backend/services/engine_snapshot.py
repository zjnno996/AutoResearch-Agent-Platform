"""Pin project workers to an immutable research engine across stage handoffs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

EXCLUDED = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}


def freeze_engine(source: Path, cache: Path) -> tuple[Path, str]:
    package = source.resolve() / "researchclaw"
    if not package.is_dir():
        raise ValueError("Research engine package is missing")
    files = sorted(p for p in package.rglob("*") if p.is_file()
        and not EXCLUDED.intersection(p.relative_to(package).parts)
        and p.suffix not in {".pyc", ".pyo"})
    digest = hashlib.sha256()
    manifest = {}
    for path in files:
        relative = path.relative_to(package).as_posix()
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest[relative] = content_hash
        executable_bits = path.stat().st_mode & 0o111
        digest.update((relative + "\0" + content_hash + "\0" + str(executable_bits) + "\n").encode())
    revision = digest.hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / revision
    if (destination / "engine_snapshot.json").is_file():
        return destination, revision
    staging = Path(tempfile.mkdtemp(prefix=".engine-", dir=cache))
    try:
        for relative, expected in manifest.items():
            target = staging / "researchclaw" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(package / relative, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError("Engine changed while snapshotting; retry after the edit finishes")
        (staging / "engine_snapshot.json").write_text(json.dumps({
            "revision": revision, "source": str(source.resolve()), "files": manifest,
        }, indent=2), encoding="utf-8")
        try:
            os.rename(staging, destination)
        except OSError:
            if not (destination / "engine_snapshot.json").is_file():
                raise
        return destination, revision
    finally:
        if staging.exists():
            shutil.rmtree(staging)
