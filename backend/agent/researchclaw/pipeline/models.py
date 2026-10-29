"""Shared stage results, independent of stage implementations and scheduling."""

from __future__ import annotations

from dataclasses import dataclass

from researchclaw.pipeline.stages import Stage, StageStatus


@dataclass(frozen=True)
class StageResult:
    stage: Stage
    status: StageStatus
    artifacts: tuple[str, ...]
    error: str | None = None
    decision: str = "proceed"
    evidence_refs: tuple[str, ...] = ()
