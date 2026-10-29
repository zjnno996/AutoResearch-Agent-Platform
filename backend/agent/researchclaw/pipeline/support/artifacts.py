"""Active-stage artifact access and stage metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any

from researchclaw.config import RCConfig
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import NEXT_STAGE, Stage
from researchclaw.pipeline.support.parsing import _safe_json_loads

logger = logging.getLogger(__name__)


def active_stage_glob(run_dir: Path, pattern: str) -> tuple[Path, ...]:
    """Resolve stage patterns against active, visible attempts only.

    A retained archive or a future stage cannot become current evidence merely
    because it matches an old wildcard used by a writing or plotting helper.
    """
    import re
    from researchclaw.observability.tracing import current_stage_number
    root = run_dir.resolve()
    visible_through = current_stage_number() or 26
    matches = []
    for path in root.glob(pattern):
        relative = path.relative_to(root)
        if not relative.parts:
            continue
        stage = re.fullmatch(r"stage-(\d{2})", relative.parts[0])
        if not stage or not 1 <= int(stage[1]) <= visible_through:
            continue
        stage_root = root / relative.parts[0]
        if stage_root.is_symlink() or not path.resolve().is_relative_to(stage_root):
            continue
        matches.append(path)
    return tuple(sorted(matches))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_stage_meta(
    stage_dir: Path, stage: Stage, run_id: str, result: StageResult
) -> None:
    next_stage = NEXT_STAGE[stage]
    meta = {
        "stage_id": f"{int(stage):02d}-{stage.name.lower()}",
        "run_id": run_id,
        "status": result.status.value,
        "decision": result.decision,
        "output_artifacts": list(result.artifacts),
        "evidence_refs": list(result.evidence_refs),
        "error": result.error,
        "ts": _utcnow_iso(),
        "next_stage": int(next_stage) if next_stage is not None else None,
    }
    from researchclaw.orchestration.artifacts import write_json
    write_json(stage_dir / "decision.json", meta)


def _read_prior_artifact(run_dir: Path, filename: str) -> str | None:
    candidate = _find_prior_artifact(run_dir, filename, allow_directory=filename.endswith("/"))
    if candidate is None:
        return None
    return str(candidate) if candidate.is_dir() else candidate.read_text(encoding="utf-8")


def _find_prior_artifact(run_dir: Path, filename: str, *, allow_directory: bool = False) -> Path | None:
    if Path(filename).is_absolute() or ".." in Path(filename).parts:
        return None
    for stage_dir in reversed(active_stage_glob(run_dir, "stage-*")):
        candidate = stage_dir / filename
        if not candidate.resolve().is_relative_to(stage_dir):
            continue
        if candidate.is_file() or (allow_directory and candidate.is_dir()):
            return candidate
    return None


def _find_prior_file(run_dir: Path, filename: str) -> Path | None:
    """Return an active evidence path without consuming archives or future stages."""
    return _find_prior_artifact(run_dir, filename)


def _read_literature_bib(run_dir: Path) -> str:
    """Read a non-empty bibliography without stale export files shadowing S4."""
    candidates = [path for pattern in ("stage-22/references.bib", "stage-20/references_preverified.bib",
                                        "stage-04/references.bib") for path in active_stage_glob(run_dir, pattern)]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"@\w+\{[^,]+,", text):
            return text
    return ""


def _load_hardware_profile(run_dir: Path) -> dict[str, Any] | None:
    """Load hardware_profile.json from a prior stage (usually stage-01)."""
    raw = _read_prior_artifact(run_dir, "hardware_profile.json")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("..", "_")
    name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)
    return name[:100] or "unnamed"


def _find_discussion_dir(run_dir: Path, config: RCConfig) -> Path | None:
    """Locate the discussion artifacts directory.

    Checks run_dir/discussion first, then falls back to
    shared_results_dir/idea_runs/<idea_id>/discussion for cases
    where L1 ran in shared_results but L5 runs in /dev/shm.
    """
    local = run_dir / "discussion"
    if local.is_dir() and (local / "consensus_synthesis.md").is_file():
        return local
    shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
    if shared_dir:
        fallback = Path(shared_dir) / "idea_runs" / run_dir.name / "discussion"
        if fallback.is_dir() and (fallback / "consensus_synthesis.md").is_file():
            return fallback
    return None


def _parse_jsonl_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = _safe_json_loads(line, {})
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _collect_json_context(
    directory: Path,
    *,
    max_files: int = 30,
    max_total_chars: int = 50_000,
) -> str:
    """Collect JSON context from a directory, with size limits.

    Large fields like ``stderr`` and ``stdout`` are stripped to avoid
    exceeding LLM token limits (the raw experiment output can be 5 MB+).
    """
    chunks: list[str] = []
    total = 0
    for file_path in sorted(directory.glob("*.json"))[:max_files]:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # Strip verbose fields that bloat the context
        if isinstance(data, dict):
            for key in ("stderr", "stdout", "raw_output", "traceback"):
                if key in data and isinstance(data[key], str) and len(data[key]) > 500:
                    data[key] = data[key][:500] + f"\n... [truncated, {len(data[key])} chars total]"
        chunk = json.dumps(data, indent=2, ensure_ascii=False)
        if total + len(chunk) > max_total_chars:
            remaining = max_total_chars - total
            if remaining > 200:
                chunks.append(chunk[:remaining] + "\n... [truncated]")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n\n".join(chunks)
