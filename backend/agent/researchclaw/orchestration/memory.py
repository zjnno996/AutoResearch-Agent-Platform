"""Inspectable research handoffs and scoped cross-session experience.

Public inspiration: Qiushi Meta-Trace and InnoClaw Memory Fabric. Memory is
an index of decisions and source pointers, never a replacement for evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import Artifacts, read_json, write_json


def memory_path(config: Any) -> Path:
    # Project + topic isolation prevents unrelated projects inheriting results.
    namespace = f"{config.project.name}\n{config.research.topic}"
    key = hashlib.sha256(namespace.encode()).hexdigest()[:24]
    return Path(config.knowledge_base.root).expanduser() / "research_memory" / f"{key}.jsonl"


def retained_lessons(run_dir: Path) -> list[dict[str, Any]]:
    """Derive bounded advice only from a compared, retained repair attempt.

    These are local observations, not causal evidence of a method's benefit.
    They never rewrite skills or relax the next task's acceptance contract.
    """
    history_file = run_dir / "research_repair_history.json"
    history = read_json(history_file)
    lessons = []
    for attempt in history.get("attempts", [])[-3:]:
        if not attempt.get("completed") or not attempt.get("comparable_contract"):
            continue
        retained = attempt.get("retained")
        if retained == "current" and (attempt.get("coverage_change", 0) <= 0 or attempt.get("regressed_criteria")):
            continue
        if retained not in ("current", "previous"):
            continue
        for action in attempt.get("actions", [])[:4]:
            content = {"criterion": action["criterion_id"], "requirement": action.get("requirement", ""),
                       "correction": action.get("action", ""), "retained": retained,
                       "coverage_change": attempt.get("coverage_change", 0)}
            lessons.append({**content,
                "lesson_id": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20],
                "observation": ("The retained repair increased internal coverage under the same contract."
                                if retained == "current" else "This attempted correction regressed quality; the prior attempt was retained."),
                "source": str(history_file.resolve()), "attempt_archive": attempt.get("archive", ""),
                "source_sha256": hashlib.sha256(history_file.read_bytes()).hexdigest(),
                "scope": "Advice for this project/topic; revalidate with new code, data and model. Not causal or experimental evidence."})
    return lessons


def record_handoff(run_dir: Path, config: Any, stage: int, result: Any,
                   verification: dict[str, Any] | None = None) -> dict[str, Any]:
    artifacts = Artifacts(run_dir, stage)
    refs = [artifacts.reference(f"stage-{stage:02d}/{name}") for name in result.artifacts]
    status = result.status.value
    gaps = (verification or {}).get("summary", {}).get("highest_priority_gaps", [])
    phase = "explore" if stage <= 9 else "execute" if stage <= 17 else "express"
    state = "repair" if status == "failed" or gaps else "review" if stage in (9, 17, 21, 23, 26) else "process"
    if status == "blocked_approval":
        state = "awaiting_approval"
    record = {
        "schema_version": "research-handoff-v1", "stage": stage, "phase": phase,
        "state": state, "status": status, "decision": result.decision,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir.resolve()),
        "judgment_summary": result.error or ("Evidence gaps require targeted work" if gaps else "Stage artifacts recorded; verify before making claims"),
        "evidence_refs": [ref for ref in refs if ref], "unresolved_gaps": gaps[:12],
        "next_task": (f"Wait for the configured approval for stage {stage}" if status == "blocked_approval" else
                      result.error if status == "failed" else gaps[0] if gaps else
                      f"Continue after stage {stage}, checking source artifacts"),
        "evidence_boundary": artifacts.json(artifacts.latest("research_readiness.json")).get("writing_policy", "unverified"),
        "lessons": retained_lessons(run_dir) if stage in (17, 23, 26) else [],
    }
    write_json(run_dir / "research_state.json", record)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with (run_dir / "research_trace.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line)
    if config.research_engine.shared_memory and stage in (17, 23, 26):
        path = memory_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(line)
            fcntl.flock(handle, fcntl.LOCK_UN)
    return record


def memory_context(run_dir: Path, config: Any) -> str:
    parts: list[str] = []
    current = read_json(run_dir / "research_state.json")
    if current:
        parts.append("Current research handoff:\n" + json.dumps(current, ensure_ascii=False))
    if config.research_engine.shared_memory:
        path = memory_path(config)
        if path.is_file():
            from collections import deque
            with path.open(encoding="utf-8", errors="replace") as handle:
                recent = deque(handle, maxlen=4)
            entries = []
            for line in recent:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("run_dir") != str(run_dir.resolve()):
                    entries.append({k: item.get(k) for k in (
                        "stage", "state", "judgment_summary", "unresolved_gaps", "evidence_boundary", "timestamp", "lessons")})
            if entries:
                parts.append("Prior experience in this project/topic (advice only, not current evidence):\n"
                             + json.dumps(entries, ensure_ascii=False))
    plan = read_json(run_dir / "research_revision_plan.json")
    if plan.get("actions"):
        # Put source pointers before long actions so truncation cannot erase
        # the location of the candidate being repaired.
        compact = {key: plan.get(key) for key in ("archive", "from_stage", "checkpoint_stage", "round", "policy")}
        compact["actions"] = plan["actions"]
        parts.insert(0, "Active targeted repair contract:\n" + json.dumps(compact, ensure_ascii=False))
    limit = config.research_engine.memory_context_chars
    return ("\n\n".join(parts))[:limit]
