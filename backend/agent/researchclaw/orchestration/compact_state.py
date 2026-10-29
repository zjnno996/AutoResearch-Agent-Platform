"""Atomic, hash-validated checkpoints for six actual phases."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import read_json, write_json

PHASES = (
    ("ground", 1, 8), ("plan", 9, 9), ("implement", 10, 13),
    ("execute", 14, 18), ("report", 19, 25), ("verify", 26, 26),
)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class PhaseStore:
    def __init__(self, root: Path, run_id: str, identity: str) -> None:
        self.root, self.run_id = root, run_id
        self.path = root / "compact_state.json"
        self.state = read_json(self.path)
        if self.state and self.state.get("identity") != identity:
            raise ValueError("Source, task or settings changed; use a fresh run directory")
        if not self.state and (root / "checkpoint.json").exists():
            raise ValueError("Legacy run detected. Resume it with workflow=legacy or use a fresh run directory")
        self.state = self.state or {"schema_version": "compact-state-v1", "identity": identity,
                                    "workflow": "compact", "phases": {}, "attempts": []}

    def manifest(self, paths: list[str]) -> dict[str, str]:
        result = {}
        for relative in paths:
            path = (self.root / relative).resolve()
            if not path.is_relative_to(self.root.resolve()) or not path.is_file():
                raise ValueError(f"Missing or invalid phase output: {relative}")
            result[relative] = file_hash(path)
        return result

    def cached(self, phase: str, inputs: str) -> bool:
        row = self.state["phases"].get(phase, {})
        if row.get("status") != "done" or row.get("inputs") != inputs:
            return False
        try:
            return bool(row.get("outputs")) and self.manifest(list(row["outputs"])) == row["outputs"]
        except (OSError, ValueError):
            return False

    def invalidate(self, phase: str) -> None:
        names = [row[0] for row in PHASES]
        for name in names[names.index(phase):]:
            if name in self.state["phases"]:
                self.state["phases"][name]["status"] = "stale"
        self.save()

    def start(self, phase: str, inputs: str) -> None:
        self.invalidate(phase)
        self.state["phases"][phase] = {"status": "running", "inputs": inputs, "started": time.time()}
        self.state["attempts"].append({"phase": phase, "started": time.time(), "status": "running"})
        self.save()

    def finish(self, phase: str, paths: list[str], *, error: str = "") -> None:
        row = self.state["phases"][phase]
        row.update(status="failed" if error else "done", elapsed_sec=round(time.time() - row["started"], 3))
        if error:
            row["error"] = error
        else:
            row["outputs"] = self.manifest(paths)
        self.state["attempts"][-1].update(row)
        self.save()

    def save(self) -> None:
        write_json(self.path, self.state)
        completed = [(name, end) for name, _, end in PHASES if self.state["phases"].get(name, {}).get("status") == "done"]
        write_json(self.root / "checkpoint.json", {"run_id": self.run_id, "workflow": "compact",
                   "last_completed_stage": completed[-1][1] if completed else 0,
                   "last_completed_name": completed[-1][0] if completed else "",
                   "timestamp": datetime.now(timezone.utc).isoformat(),
                   "completed_phases": [name for name, _ in completed],
                   "completed_stages": [end for _, end in completed]})
