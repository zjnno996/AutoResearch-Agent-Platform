"""Select bounded repairs from specific evidence gaps; preserve all attempts."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import Artifacts, read_json, write_json


def propose_repair(run_dir: Path, stage: int, config: Any) -> dict[str, Any] | None:
    if not config.research_engine.enabled or stage not in (9, 17, 23, 26):
        return None
    history = read_json(run_dir / "research_repair_history.json").get("attempts", [])
    if len(history) >= config.research_engine.max_repair_rounds:
        return None
    verification = read_json(run_dir / "research_verification.json")
    # Only consume a verification generated for this checkpoint.
    if verification.get("stage") != stage:
        return None
    actions = []
    if verification.get("induction_status") != "completed":
        actions.append({"criterion_id": "task_contract", "owner_stage": 2,
                        "requirement": "Establish task-specific acceptance criteria for every explicit research goal",
                        "action": "Rebuild the task rubric; generic artifact checks do not establish task coverage"})
    for criterion in verification.get("criteria", []):
        if criterion.get("status") != "unmet" or not criterion.get("critical"):
            continue
        owner = criterion.get("owner_stage", 11)
        if type(owner) is not int or not 1 <= owner <= stage:
            continue
        actions.append({"criterion_id": criterion["criterion_id"], "owner_stage": owner,
                        "requirement": criterion["requirement"], "action": criterion["evidence_gap"],
                        "evidence_fingerprint": sorted(ref.get("sha256", "") for ref in criterion.get("evidence_found", [])
                                                       if isinstance(ref, dict))})
    if stage == 9:
        council = read_json(run_dir / "stage-09" / "research_council.json")
        for review in council.get("reviews", []):
            for issue in review.get("issues", []):
                if issue.get("severity") == "critical" and issue.get("supported"):
                    actions.append({"criterion_id": f"design_{review['role']}", "owner_stage": 9,
                                    "requirement": issue["requirement"], "action": issue["action"],
                                    "evidence_fingerprint": [Artifacts(run_dir, stage).reference(p).get("sha256", "")
                                                             for p in issue.get("evidence_refs", [])]})
    if not actions:
        return None
    signature = hashlib.sha256(json.dumps(sorted(actions, key=lambda a: a["criterion_id"]), sort_keys=True).encode()).hexdigest()
    # Stop on unchanged evidence and gaps. An improving score or newly
    # generated evidence still deserves another bounded quality iteration.
    if any(item.get("signature") == signature for item in history):
        return None
    target = min(action["owner_stage"] for action in actions)
    return {"schema_version": "research-revision-v1", "round": len(history) + 1,
            "checkpoint_stage": stage, "from_stage": target, "signature": signature,
            "actions": actions, "verification_before": verification.get("summary", {}),
            "policy": "Fix these criteria; preserve satisfied requirements and actual measurements. Reverify afterward."}


def begin_repair(run_dir: Path, plan: dict[str, Any], run_id: str) -> None:
    """Archive the invalidated suffix outside stage-* discovery paths.

    This prevents old reports or outputs from a prior attempt being consumed
    as fresh evidence when the run returns to planning or execution.
    """
    archive = run_dir / "research_attempts" / f"round-{plan['round']:03d}"
    archive.mkdir(parents=True, exist_ok=False)
    for source in sorted(run_dir.glob("stage-*")):
        match = re.fullmatch(r"stage-(\d{2})(?:_v\d+)?", source.name)
        if match and int(match[1]) >= plan["from_stage"]:
            shutil.move(str(source), str(archive / source.name))
    for name in ("research_verification.json", "research_state.json", "pipeline_summary.json",
                 "research_engine_summary.json", "degradation_signal.json", "quality_warning.txt"):
        source = run_dir / name
        if source.exists():
            shutil.move(str(source), str(archive / name))
    # A deliverable from the previous attempt is never a current export.
    source = run_dir / "deliverables"
    if source.exists():
        shutil.move(str(source), str(archive / "deliverables"))
    if plan["from_stage"] <= 2:
        source = run_dir / "research_rubric.json"
        if source.exists():
            shutil.move(str(source), str(archive / source.name))
    plan["archive"] = str(archive.relative_to(run_dir))
    write_json(run_dir / "research_revision_plan.json", plan)
    history_path = run_dir / "research_repair_history.json"
    history = read_json(history_path)
    history.setdefault("attempts", []).append(plan)
    write_json(history_path, history)
    completed = plan["from_stage"] - 1
    write_json(run_dir / "checkpoint.json", {"last_completed_stage": completed,
               "run_id": run_id, "reason": "evidence_driven_repair"})


def complete_repair(run_dir: Path, stage: int) -> None:
    path = run_dir / "research_repair_history.json"
    history = read_json(path)
    current = read_json(run_dir / "research_verification.json")
    changed = False
    for attempt in history.get("attempts", []):
        if attempt.get("checkpoint_stage") == stage and "verification_after" not in attempt:
            attempt["verification_after"] = current.get("summary", {})
            valid_after = bool(current.get("criteria")) and bool(current.get("summary")) and current.get("stage", stage) == stage
            degraded_design = read_json(run_dir / "stage-09/exp_plan_diagnostics.json").get("degraded", False)
            attempt["completed"] = valid_after and not degraded_design
            before = attempt.get("verification_before", {}).get("coverage", 0)
            after = current.get("summary", {}).get("coverage", 0)
            attempt["coverage_change"] = round(after - before, 4)
            attempt["retained"] = "current"
            archive = run_dir / attempt["archive"]
            previous = read_json(archive / "research_verification.json")
            def contract(report: dict[str, Any]) -> list[str]:
                keys = ("criterion_id", "requirement", "satisfaction_condition", "check", "owner_stage", "due_stage", "target_score")
                return sorted(json.dumps({key: row.get(key) for key in keys}, sort_keys=True)
                              for row in report.get("criteria", []))
            comparable = (previous.get("induction_status") == "completed" and bool(contract(previous))
                          and contract(previous) == contract(current))
            attempt["comparable_contract"] = comparable
            prior_score = read_json(archive / "stage-23/quality_report.json").get("score_1_to_10")
            current_score = read_json(run_dir / "stage-23/quality_report.json").get("score_1_to_10")
            quality_regressed = (after == before and type(prior_score) in (int, float)
                                 and type(current_score) in (int, float) and current_score < prior_score)
            current_rows = {row["criterion_id"]: row for row in current.get("criteria", [])}
            lost_checks = [row["criterion_id"] for row in previous.get("criteria", [])
                           if row.get("satisfied") and not current_rows.get(row["criterion_id"], {}).get("satisfied")]
            attempt["regressed_criteria"] = lost_checks
            candidate_failed = not valid_after or degraded_design
            attempt["status"] = "failed" if candidate_failed else "completed"
            if candidate_failed or (comparable and (after < before or quality_regressed or lost_checks)):
                rejected = archive / "regressed_attempt"
                rejected.mkdir(exist_ok=True)
                for source in sorted(run_dir.glob("stage-*")):
                    match = re.fullmatch(r"stage-(\d{2})(?:_v\d+)?", source.name)
                    if match and int(match[1]) >= attempt["from_stage"]:
                        shutil.move(str(source), str(rejected / source.name))
                for source in archive.glob("stage-*"):
                    if source.is_dir():
                        shutil.copytree(source, run_dir / source.name)
                for name in ("research_rubric.json", "research_verification.json", "research_state.json", "research_engine_summary.json",
                             "degradation_signal.json", "quality_warning.txt"):
                    source = archive / name
                    if source.is_file():
                        shutil.copy2(source, run_dir / name)
                    elif name != "research_rubric.json":
                        (run_dir / name).unlink(missing_ok=True)
                attempt["retained"] = "previous"
            changed = True
    if changed:
        write_json(path, history)
        plan_path = run_dir / "research_revision_plan.json"
        plan = read_json(plan_path)
        if plan.get("checkpoint_stage") == stage:
            latest = next((row for row in reversed(history.get("attempts", [])) if row.get("checkpoint_stage") == stage), {})
            plan["status"] = latest.get("status", "failed")
            if latest.get("completed"):
                plan["actions"] = []
            write_json(plan_path, plan)
