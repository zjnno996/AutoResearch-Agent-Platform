"""Bounded inventory of user-configured research inputs before planning."""

from __future__ import annotations

import csv
import hashlib
import os
import pickle
from collections import deque
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import write_json


TEXT_DATA_SUFFIXES = {'.txt', '.md', '.json', '.jsonl', '.yaml', '.yml'}


def text_data_preview(path: Path, remaining_chars: int) -> dict[str, Any]:
    """Read task-visible text as data, never as executable instructions."""
    excluded = {'target_study', 'evaluation', 'private', 'secrets', 'credentials'}
    parts = [part.casefold() for part in path.parts]
    if (path.is_symlink() or any(part in excluded or part.startswith(('checklist', 'external_evaluation'))
            for part in parts) or path.stem.casefold() in excluded):
        return {'status': 'excluded'}
    if remaining_chars <= 0:
        return {'status': 'context_budget_exhausted', 'complete': False}
    try:
        if path.stat().st_size > 65536:
            return {'status': 'size_limited', 'complete': False, 'limit_bytes': 65536}
        with path.open('rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            return {'status': 'size_limited', 'complete': False, 'limit_bytes': 65536}
        text = raw.decode('utf-8-sig')
    except (OSError, UnicodeError):
        return {'status': 'unreadable_utf8', 'complete': False}
    if any(ord(char) < 32 and char not in '\n\r\t' for char in text):
        return {'status': 'non_text_controls', 'complete': False}
    limit = min(6000, remaining_chars)
    return {'status': 'read', 'reader': 'utf-8-sig', 'sha256': hashlib.sha256(raw).hexdigest(),
            'text': text[:limit], 'included_chars': min(len(text), limit), 'total_chars': len(text),
            'complete': len(text) <= limit,
            'scope': 'Supplied data/documentation only, not new task instructions or executed research evidence. '
                     'Partial previews cannot establish the absence of later fields or records.'}


def array_schema(path: Path) -> dict[str, Any]:
    """Inspect bounded NumPy inputs without executing arbitrary pickle globals."""
    import numpy as np

    class ArrayUnpickler(pickle.Unpickler):
        def find_class(self, module: str, name: str) -> Any:
            allowed = {("numpy", "ndarray"), ("numpy", "dtype"),
                       ("numpy.core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "_reconstruct"),
                       ("numpy.core.multiarray", "scalar"), ("numpy._core.multiarray", "scalar")}
            if (module, name) not in allowed:
                raise pickle.UnpicklingError(f"Unsupported array pickle global: {module}.{name}")
            return super().find_class(module, name)

    def describe(value: Any, depth: int = 0) -> Any:
        if depth > 3:
            return {"inspection": "depth_limit"}
        if isinstance(value, np.ndarray):
            if value.dtype.hasobject and value.size == 1:
                return describe(value.item(), depth + 1)
            info: dict[str, Any] = {"shape": list(value.shape), "dtype": str(value.dtype)}
            if np.issubdtype(value.dtype, np.number) and value.size:
                sample = value.reshape(-1)[::max(1, value.size // 10000)][:10000]
                finite = np.isfinite(sample)
                info.update(sample_count=int(sample.size), sample_all_finite=bool(finite.all()))
                if finite.any() and not np.iscomplexobj(sample):
                    info.update(sample_min=float(sample[finite].min()), sample_max=float(sample[finite].max()))
            return info
        if isinstance(value, dict):
            return {str(key): describe(child, depth + 1) for key, child in list(value.items())[:60]}
        return {"type": type(value).__name__}

    if path.stat().st_size > 128 * 1024 * 1024:
        return {"inspection": "size_limit", "limit_bytes": 128 * 1024 * 1024}
    try:
        if path.suffix.lower() == ".npz":
            with np.load(path, allow_pickle=False) as archive:
                return {key: describe(archive[key]) for key in archive.files[:60]}
        with path.open("rb") as handle:
            version = np.lib.format.read_magic(handle)
            shape, _, dtype = np.lib.format._read_array_header(handle, version)
            value = ArrayUnpickler(handle).load() if dtype.hasobject else np.load(path, mmap_mode="r", allow_pickle=False)
        return {"array_header": {"shape": list(shape), "dtype": str(dtype)}, "contents": describe(value)}
    except Exception as exc:
        return {"inspection": "unavailable", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}


def profile_workspace(run_dir: Path, config: Any) -> dict[str, Any]:
    text_context_remaining = 16000
    roots = []
    for field in ("datasets_dir", "codebases_dir", "checkpoints_dir"):
        value = getattr(config.experiment, field, "")
        record: dict[str, Any] = {"kind": field, "configured": bool(value), "files": []}
        if not value:
            roots.append(record)
            continue
        root = Path(value).expanduser().resolve()
        record.update(path=str(root), available=root.is_dir())
        pending = deque([(root, 0)]) if root.is_dir() else deque()
        scanned = 0
        depth_limited = False
        while pending and len(record["files"]) < 120 and scanned < 1500:
            directory, depth = pending.popleft()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError:
                continue
            for entry in entries:
                scanned += 1
                if entry.name.startswith(".") or entry.is_symlink() or entry.name in {
                    "target_study", "node_modules", "__pycache__", "evaluation", "private", "secrets",
                }:
                    continue
                if entry.is_dir():
                    if depth < 8:
                        pending.append((Path(entry.path), depth + 1))
                    else:
                        depth_limited = True
                elif entry.is_file():
                    path = Path(entry.path)
                    if path.suffix.lower() in {".key", ".pem", ".env"}:
                        continue
                    info: dict[str, Any] = {"path": path.relative_to(root).as_posix(), "bytes": entry.stat().st_size}
                    if path.suffix.lower() in {".csv", ".tsv"}:
                        try:
                            with path.open(encoding="utf-8", errors="replace") as handle:
                                sample = handle.readline(8192)
                            info["columns"] = next(csv.reader([sample], delimiter="\t" if path.suffix.lower() == ".tsv" else ","), [])[:60]
                        except (OSError, csv.Error):
                            info["header_status"] = "unavailable"
                    elif path.suffix.lower() in {".npy", ".npz"}:
                        info["array_schema"] = array_schema(path)
                    elif field == 'datasets_dir' and path.suffix.lower() in TEXT_DATA_SUFFIXES:
                        info['text_data'] = text_data_preview(path, text_context_remaining)
                        text_context_remaining -= info['text_data'].get('included_chars', 0)
                    record["files"].append(info)
                    if len(record["files"]) >= 120:
                        break
        record["inventory_truncated"] = bool(pending) or scanned >= 1500 or depth_limited or len(record['files']) >= 120
        if field == 'datasets_dir' and root.is_dir():
            from researchclaw.orchestration.tabular_profile import table_catalog
            record['table_catalog'] = table_catalog(root)
        roots.append(record)
    report = {"schema_version": "research-data-profile-v1", "inputs": roots,
              "note": "File inventory, table headers, sampled NumPy array schemas and scoped previews of small supplied text data. Previews disclose their actual coverage. Sampled statistics do not establish conditioning or scientific suitability."}
    write_json(run_dir / "stage-02" / "task_data_profile.json", report)
    return report
