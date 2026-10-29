"""Compare quality first, then efficiency, on matched completed research runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import read_json, write_json


def _pipeline_duration(run_dir: Path) -> float:
    import json
    path = run_dir / "traces" / "workflow_trace.jsonl"
    total = 0.0
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if row.get("event") == "stage_finished":
                    total += max(0, float(row.get("duration_sec", 0)))
            except (ValueError, TypeError, AttributeError):
                continue
    return round(total, 3)


def compare_runs(baseline: Path, candidate: Path, output: Path | None = None) -> dict[str, Any]:
    before = read_json(baseline / "research_verification.json")
    after = read_json(candidate / "research_verification.json")
    baseline_rubric = read_json(baseline / "research_rubric.json")
    candidate_rubric = read_json(candidate / "research_rubric.json")
    baseline_eval = read_json(baseline / "external_evaluation.json")
    candidate_eval = read_json(candidate / "external_evaluation.json")
    reasons = []
    same_topic = bool(baseline_rubric.get("topic_hash")) and baseline_rubric.get("topic_hash") == candidate_rubric.get("topic_hash")
    if not same_topic:
        reasons.append("Research topics do not match or are unrecorded")
    before_ids = {row["criterion_id"] for row in before.get("criteria", [])}
    after_ids = {row["criterion_id"] for row in after.get("criteria", [])}
    same_rubric = bool(before_ids) and before_ids == after_ids
    import json
    contract_keys = ("criterion_id", "requirement", "satisfaction_condition", "check", "owner_stage", "due_stage")
    def contract(rubric: dict[str, Any]) -> str:
        return json.dumps(sorted([{key: row.get(key) for key in contract_keys}
                                  for row in rubric.get("criteria", [])], key=lambda row: row["criterion_id"]), sort_keys=True)
    same_rubric = same_rubric and contract(baseline_rubric) == contract(candidate_rubric)
    if not same_rubric:
        reasons.append("Acceptance criteria do not match")
    no_lost_checks = all(not row.get("satisfied") or any(
        other.get("criterion_id") == row["criterion_id"] and other.get("satisfied")
        for other in after.get("criteria", [])) for row in before.get("criteria", []))
    if not after.get("all_satisfied") or after.get("stage") != 26:
        reasons.append("Candidate has unresolved criteria or final verification is incomplete")
    if not no_lost_checks:
        reasons.append("Candidate regressed on previously satisfied criteria")
    identity_keys = ("benchmark", "task_id", "evaluator_version", "judge_model", "data_revision")
    matched_external = all(baseline_eval.get(key) and baseline_eval.get(key) == candidate_eval.get(key) for key in identity_keys)
    if not matched_external:
        reasons.append("Matched independent external evaluations are required")
    before_score, after_score = baseline_eval.get("score"), candidate_eval.get("score")
    from math import isfinite
    scores_valid = all(type(score) in (int, float) and isfinite(score) for score in (before_score, after_score))
    external_non_regression = matched_external and scores_valid and after_score >= before_score
    if not external_non_regression:
        reasons.append("External research quality is unverified or regressed")
    usage_before = read_json(baseline / "traces" / "llm_trace_summary.json")
    usage_after = read_json(candidate / "traces" / "llm_trace_summary.json")
    tokens_before, tokens_after = usage_before.get("total_tokens", 0), usage_after.get("total_tokens", 0)
    time_before, time_after = _pipeline_duration(baseline), _pipeline_duration(candidate)
    measured = (usage_before.get("usage_complete") is True and usage_after.get("usage_complete") is True
                and all(type(v) in (int, float) and isfinite(v) and v > 0 for v in (tokens_before, tokens_after, time_before, time_after)))
    efficiency_improved = measured and tokens_after <= tokens_before and time_after <= time_before and (
        tokens_after < tokens_before or time_after < time_before)
    if not measured:
        reasons.append("Complete nonzero token and latency measurements are required")
    quality_passed = same_topic and same_rubric and no_lost_checks and after.get("all_satisfied") is True and after.get("stage") == 26 and external_non_regression
    report = {
        "schema_version": "research-comparison-v1", "baseline": str(baseline.resolve()),
        "candidate": str(candidate.resolve()), "topic_hash": candidate_rubric.get("topic_hash"),
        "quality_non_regression": bool(quality_passed), "external_evaluation_matched": bool(matched_external),
        "baseline_external_score": before_score, "candidate_external_score": after_score,
        "baseline_coverage": before.get("summary", {}).get("coverage"),
        "candidate_coverage": after.get("summary", {}).get("coverage"),
        "efficiency_improved": bool(efficiency_improved),
        "approve_efficiency_profile": bool(quality_passed and efficiency_improved),
        "baseline_tokens": tokens_before, "candidate_tokens": tokens_after,
        "baseline_pipeline_sec": time_before, "candidate_pipeline_sec": time_after,
        "baseline_llm_latency_sec": usage_before.get("total_latency_sec"),
        "candidate_llm_latency_sec": usage_after.get("total_latency_sec"),
        "token_reduction": round(1 - tokens_after / tokens_before, 4) if measured else None,
        "latency_reduction": round(1 - time_after / time_before, 4) if measured else None,
        "reasons": reasons,
        "scope": "This matched task only; evaluate representative tasks before changing a global default.",
    }
    if output:
        write_json(output, report)
    return report


def validate_efficiency_approval(config: Any) -> None:
    if config.research_engine.profile != "balanced":
        return
    import hashlib
    report = read_json(Path(config.research_engine.efficiency_approval).expanduser())
    if (report.get("schema_version") != "research-comparison-v1"
            or report.get("approve_efficiency_profile") is not True
            or report.get("topic_hash") != hashlib.sha256(config.research.topic.encode()).hexdigest()):
        raise ValueError("Balanced profile requires a passing measured comparison for this research topic")
