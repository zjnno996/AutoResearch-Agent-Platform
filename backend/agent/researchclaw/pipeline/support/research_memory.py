"""Support research_memory components."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

from researchclaw.config import RCConfig


logger = logging.getLogger(__name__)


def _resolve_shared_results_path(config: RCConfig, run_dir: Path) -> Path:
    if config.research_engine.enabled:
        from researchclaw.orchestration.memory import memory_path
        return memory_path(config).with_suffix("")
    raw = getattr(getattr(config, "experiment", None), "shared_results_dir", "") or "runs/shared_results"
    path = Path(str(raw))
    if path.is_absolute():
        return path
    # The CLI usually runs from repo/backend/agent; this fallback keeps direct
    # tests and web-launched jobs writing under backend/runs/shared_results.
    backend_runs = run_dir.parents[2] / "shared_results" if len(run_dir.parents) >= 3 else run_dir.parent / "shared_results"
    return backend_runs if backend_runs.parent.name == "runs" else Path.cwd() / path


def _load_ideation_memory(config: RCConfig, run_dir: Path, *, max_chars: int = 8000) -> str:
    paths = [
        _resolve_shared_results_path(config, run_dir) / "ideation_memory.md",
    ]
    if not config.research_engine.enabled:
        paths.append(run_dir.parent / "ideation_memory.md")
    chunks: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text:
            chunks.append(f"## {path.name}\n{text[-max_chars:]}")
    return "\n\n".join(chunks)[-max_chars:]


def _load_experiment_memory(config: RCConfig, run_dir: Path, *, max_chars: int = 8000) -> str:
    """Load experiment memory from shared results dir — contains lessons from past experimental runs."""
    paths = [
        _resolve_shared_results_path(config, run_dir) / "experiment_memory.md",
    ]
    if not config.research_engine.enabled:
        paths.append(run_dir.parent / "experiment_memory.md")
    chunks: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text:
            chunks.append(f"## {path.name}\n{text[-max_chars:]}")
    return "\n\n".join(chunks)[-max_chars:]


def _write_experiment_memory_update(
    config: RCConfig,
    run_dir: Path,
    stage_dir: Path,
    *,
    topic: str,
    experiment_summary: str,
) -> tuple[str, ...]:
    """Write an experiment memory update after experimental runs (Stages 14-15).

    Captures what worked, what failed, and key lessons for future hypothesis generation cycles.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    update = [
        f"## {today} — {topic}",
        "",
        "### Experiment Summary",
        experiment_summary[:2000].strip(),
        "",
        "### Lessons",
        "- Record what worked, what failed, and why.",
        "- Note any unexpected findings, regressions, or surprising results.",
        "",
    ]
    update_text = "\n".join(update).strip() + "\n"
    try:
        shared_dir = _resolve_shared_results_path(config, run_dir)
        shared_dir.mkdir(parents=True, exist_ok=True)
        memory_path = shared_dir / "experiment_memory.md"
        append_experience(memory_path, update_text)
        logger.info("S8: Experiment memory updated (run_dir=%s)", run_dir)
    except OSError:
        logger.warning("S8: Failed to update experiment memory", exc_info=True)
    return ("experiment_memory_update.md",)


def append_experience(path: Path, text: str) -> None:
    """Serialize appends without reading and rewriting another run's update."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write("\n\n" + text)
        fcntl.flock(handle, fcntl.LOCK_UN)
