"""Attach research skills, evidence contracts and handoffs to every stage."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.council import review_council
from researchclaw.orchestration.context import stage_guidance
from researchclaw.orchestration.memory import memory_context, record_handoff
from researchclaw.orchestration.methods import method_plan, strategy_guidance
from researchclaw.orchestration.rubric import induce_rubric, verify_research
from researchclaw.skills import SkillRegistry


def prepare_stage(run_dir: Path, stage: int, config: Any) -> str:
    settings = config.research_engine
    if not settings.enabled:
        return ""
    from researchclaw.orchestration.assessment import validate_efficiency_approval
    validate_efficiency_approval(config)
    from researchclaw.skills.registry import BUILTIN_ROOT
    import hashlib
    lock_file = BUILTIN_ROOT.parent / "sources.lock.json"
    write_json(run_dir / "research_identity.json", {
        "schema_version": "research-identity-v1",
        "primary_model": config.llm.primary_model, "coding_model": config.llm.coding_model,
        "experiment_mode": config.experiment.mode,
        "skill_sources_sha256": hashlib.sha256(lock_file.read_bytes()).hexdigest() if lock_file.is_file() else "unavailable",
        "max_skills_per_stage": settings.max_skills_per_stage, "skill_context_chars": settings.skill_context_chars,
    })
    write_json(run_dir / "research_method_plan.json", method_plan(config))
    stage_dir = run_dir / f"stage-{stage:02d}"
    context = [
        "Research policy: establish task fidelity and scientific evidence before optimizing time or tokens. "
        "Keep negative and inconclusive results. Do not replace the requested experiment with an unrelated smoke test. "
        "Save raw outputs, commands, dataset scope, conditions and independent seeds. "
        "Memory and reviews point to evidence; they are not themselves experimental evidence."
    ]
    context.append(strategy_guidance(config, stage))
    if stage == 2:
        from researchclaw.orchestration.workspace import profile_workspace
        context.append("Observed research inputs:\n" + json.dumps(profile_workspace(run_dir, config), ensure_ascii=False))
    memory = memory_context(run_dir, config)
    if memory:
        context.append(memory)
    rubric = read_json(run_dir / "research_rubric.json")
    criteria = rubric.get("criteria", [])
    if criteria:
        context.append("Research acceptance criteria:\n" + json.dumps([
            {k: c.get(k) for k in ("criterion_id", "requirement", "satisfaction_condition", "grounded_procedure", "blockers", "owner_stage", "due_stage")}
            for c in criteria
        ], ensure_ascii=False))
    if stage > 9:
        council = read_json(run_dir / "stage-09" / "research_council.json")
        issues = [issue for review in council.get("reviews", []) for issue in review.get("issues", [])
                  if issue.get("supported")]
        if issues:
            context.append("Independent design review corrections:\n" + json.dumps(issues, ensure_ascii=False)[:8000])
    skill_records = []
    if settings.skills_enabled:
        registry = SkillRegistry(settings.skills_dirs)
        max_skills = settings.max_skills_per_stage
        max_chars = settings.skill_context_chars
        if settings.profile == "balanced":
            max_skills = min(max_skills, 2)
            max_chars = min(max_chars, 12000)
        skill_text, skill_records = registry.prompt(stage, config.research.topic,
                                                   limit=max_skills, max_chars=max_chars)
        if skill_text:
            context.append(skill_text)
    text = "\n\n".join(context)
    write_json(stage_dir / "research_skill_usage.json", {
        "schema_version": "research-skills-v1", "stage": stage,
        "profile": settings.profile, "skills": skill_records, "context_chars": len(text),
    })
    return text


def evaluate_stage(run_dir: Path, stage: int, result: Any, config: Any, llm: Any) -> Any:
    if not config.research_engine.enabled:
        return result
    extra = ["research_skill_usage.json"]
    if stage == 2:
        extra.append("task_data_profile.json")
    verification = None
    if result.status.value == "done":
        if config.research_engine.rubric_enabled and stage == 2:
            induce_rubric(run_dir, config, llm)
        if config.research_engine.rubric_enabled and stage == 7:
            from researchclaw.orchestration.rubric import ground_rubric
            ground_rubric(run_dir, config, llm)
        if config.research_engine.council_enabled and stage in (9, 21):
            with stage_guidance(""):
                review_council(run_dir, stage, config, llm)
            extra.append("research_council.json")
    can_verify = result.status.value == "done" or (stage == 23 and (run_dir / "stage-23/quality_report.json").is_file())
    if config.research_engine.rubric_enabled and can_verify and stage in (9, 17, 23, 26):
        with stage_guidance(""):
            verification = verify_research(run_dir, stage, config, llm)
        extra.append("research_verification.json")
        if not verification["due_satisfied"]:
            result = replace(result, decision="degraded" if result.decision == "proceed" else result.decision)
    return replace(result, artifacts=tuple(dict.fromkeys((*result.artifacts, *extra))))


def record_stage(run_dir: Path, stage: int, result: Any, config: Any) -> Any:
    """Record the final gate outcome, after all scientific and human gates."""
    if not config.research_engine.enabled:
        return result
    verification = (read_json(run_dir / f"stage-{stage:02d}" / "research_verification.json") or None
                    if "research_verification.json" in result.artifacts else None)
    extra = []
    record_handoff(run_dir, config, stage, result, verification)
    summary = read_json(run_dir / "research_engine_summary.json")
    usage = read_json(run_dir / f"stage-{stage:02d}" / "research_skill_usage.json")
    stages = summary.setdefault("stages", {})
    stages[str(stage)] = {"skills": [item["name"] for item in usage.get("skills", [])],
                          'skill_records': usage.get('skills', []),
                          "context_chars": usage.get("context_chars", 0), "status": result.status.value}
    summary.update(schema_version="research-engine-v1", profile=config.research_engine.profile,
                   last_stage=stage, quality_validated=False,
                   quality_validation_status="requires_external_evaluation",
                   skill_count=len(SkillRegistry(config.research_engine.skills_dirs).skills),
                   policy="quality_before_efficiency", method=method_plan(config)["strategy"])
    if verification:
        summary.update(verification=verification["summary"], all_satisfied=verification["all_satisfied"],
                       induction_status=verification["induction_status"])
    if result.status.value != "done" or (not verification and stage < 26):
        summary["all_satisfied"] = False
    write_json(run_dir / "research_engine_summary.json", summary)
    for name in ("research_state.json", "research_engine_summary.json", "research_rubric.json",
                 "research_method_plan.json", "research_execution_plan.json"):
        payload = read_json(run_dir / name)
        if payload:
            write_json(run_dir / f"stage-{stage:02d}" / name, payload)
            extra.append(name)
    return replace(result, artifacts=tuple(dict.fromkeys((*result.artifacts, *extra))))


def finish_stage(run_dir: Path, stage: int, result: Any, config: Any, llm: Any) -> Any:
    """Compatibility entry point for evaluation followed by final recording."""
    result = evaluate_stage(run_dir, stage, result, config, llm)
    return record_stage(run_dir, stage, result, config)
