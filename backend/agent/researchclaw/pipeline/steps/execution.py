"""Steps execution components."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import _read_prior_artifact, _utcnow_iso
from researchclaw.pipeline.support.context import _chat_with_prompt, _get_evolution_overlay
from researchclaw.pipeline.support.parsing import _safe_json_loads
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _execute_code_generation(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """S11 CODE_GENERATION — delegates to the refactored codegen package.

    The full implementation lives in ``researchclaw.pipeline.codegen``,
    structured around claw-code's harness engineering patterns:
    StrategyRegistry, CodegenRouter, CodegenRuntime, and PromptBuilder.
    """
    from researchclaw.pipeline.codegen import execute_code_generation
    return execute_code_generation(
                    stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
            llm=llm,
        prompts=prompts,
    )


def _execute_sanity_check(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    run_id: str = "",
    prompts: "PromptManager | None" = None,
    llm: "LLMClient | None" = None,
    **kwargs: object,
) -> StageResult:
    """S12 SANITY_CHECK — delegates to the refactored sanity_check package.

    The full implementation uses a claw-code agentic turn loop to run
    smoke tests, diagnose errors, and fix code iteratively.
    """
    from researchclaw.pipeline.sanity_check import execute_sanity_check
    return execute_sanity_check(
                stage_dir=stage_dir,
                run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
        prompts=prompts,
    )


def _execute_resource_planning(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    exp_plan = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
    schedule: dict[str, Any] | None = None
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "resource_planning")
        sp = _pm.for_stage("resource_planning", evolution_overlay=_overlay, exp_plan=exp_plan)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        parsed = _safe_json_loads(resp.content, {})
        if isinstance(parsed, dict):
            schedule = parsed
    if schedule is None:
        schedule = {
            "tasks": [
                {
                    "id": "baseline",
                    "name": "Run baseline",
                    "depends_on": [],
                    "gpu_count": 1,
                    "estimated_minutes": 20,
                    "priority": "high",
                },
                {
                    "id": "proposed",
                    "name": "Run proposed method",
                    "depends_on": ["baseline"],
                    "gpu_count": 1,
                    "estimated_minutes": 30,
                    "priority": "high",
                },
            ],
            "total_gpu_budget": 1,
            "generated": _utcnow_iso(),
        }
    schedule.setdefault("generated", _utcnow_iso())
    (stage_dir / "schedule.json").write_text(
        json.dumps(schedule, indent=2), encoding="utf-8"
    )
    return StageResult(
        stage=Stage.RESOURCE_PLANNING,
        status=StageStatus.DONE,
        artifacts=("schedule.json",),
        evidence_refs=("stage-13/schedule.json",),
    )


def _execute_experiment_run(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """S14 EXPERIMENT_RUN — delegates to the refactored experiment_run package.

    The full implementation uses a claw-code agentic turn loop to execute
    the experiment, monitor for runtime errors, and apply fixes.
    """
    from researchclaw.pipeline.experiment_run import execute_experiment_run
    return execute_experiment_run(
        stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
        prompts=prompts,
    )


def _execute_iterative_refine(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """S15 ITERATIVE_REFINE — delegates to the refactored iterative_refine package.

    The full implementation uses a claw-code agentic turn loop to analyze
    results, modify code, and re-run experiments iteratively.
    """
    from researchclaw.pipeline.iterative_refine import execute_iterative_refine
    return execute_iterative_refine(
        stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
        prompts=prompts,
    )
