"""Steps decision components."""

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
from researchclaw.pipeline.support.claims import (
    _build_research_readiness,
    _scope_locked_reproduction_can_proceed,
)
from researchclaw.pipeline.support.context import _get_evolution_overlay
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _parse_decision(text: str) -> str:
    """Extract PROCEED/PIVOT/REFINE from decision text.

    Looks for the first standalone keyword on its own line after a
    ``## Decision`` heading.  Falls back to a keyword scan of the first
    few lines after the heading, but only matches the keyword itself
    (not mentions inside explanatory prose like "PIVOT is not warranted").
    Returns lowercase ``"proceed"`` / ``"pivot"`` / ``"refine"``.
    Defaults to ``"proceed"`` if nothing matches.
    """
    import re as _re

    text_upper = text.upper()
    # Look in the first occurrence after "## Decision" heading
    decision_section = ""
    for keyword in ("## DECISION", "## Decision", "## decision"):
        if keyword.upper() in text_upper:
            idx = text_upper.index(keyword.upper())
            decision_section = text[idx : idx + 200]
            break
    search_text = decision_section or text[:500]

    # First try: look for a line that is just the keyword (possibly with
    # whitespace / markdown bold / trailing punctuation).
    for line in search_text.splitlines():
        stripped = line.strip().strip("*").strip("#").strip()
        if stripped.upper() in ("PROCEED", "PIVOT", "REFINE"):
            return stripped.lower()

    # Fallback: regex for standalone word boundaries so that
    # "PIVOT is not warranted" does NOT match as a decision.
    for kw in ("PIVOT", "REFINE", "PROCEED"):
        # Only match if the keyword appears as the FIRST keyword-class token
        # on its own (not embedded in a sentence saying "not PIVOT").
        pattern = _re.compile(
            r"(?:^|##\s*Decision\s*\n\s*)" + kw, _re.IGNORECASE | _re.MULTILINE
        )
        if pattern.search(search_text):
            return kw.lower()

    # Last resort: simple containment (original behavior)
    search_upper = search_text.upper()
    if "REFINE" in search_upper:
        return "refine"
    if "PIVOT" in search_upper:
        return "pivot"
    return "proceed"


def _execute_research_decision(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    pre_decision_readiness = _build_research_readiness(run_dir)
    readiness_context = (
        "\n\n## NON-NEGOTIABLE RESEARCH READINESS AND CLAIM BOUNDARY\n"
        + json.dumps(pre_decision_readiness, ensure_ascii=False, indent=2)
        + "\nThe decision must respect writing_policy. PROCEED may mean proceeding to an "
        "engineering report or limited-evidence paper; it never upgrades the allowed claim scope.\n"
    )

    # P6: Detect degenerate REFINE cycles — inject warning if metrics stagnate
    _degenerate_hint = ""
    _refine_log = _read_prior_artifact(run_dir, "refinement_log.json")
    if _refine_log:
        try:
            _rl = json.loads(_refine_log)
            _iters = _rl.get("iterations", [])
            if not isinstance(_iters, list):
                _iters = []
            _metrics = [it.get("metric") for it in _iters if isinstance(it, dict)]
            _valid = [m for m in _metrics if m is not None]
            _all_saturated = _valid and all(m <= 0.001 or m >= 0.999 for m in _valid)
            _all_identical = len(set(_valid)) <= 1 and len(_valid) >= 2
            if _all_saturated or _all_identical:
                _degenerate_hint = (
                    "\n\nSYSTEM WARNING — DEGENERATE REFINE CYCLE DETECTED:\n"
                    f"Metrics across {len(_valid)} iterations: {_valid}\n"
                    "All iterations produce identical/saturated results. Further REFINE "
                    "cycles CANNOT fix this — the underlying benchmark design is too "
                    "easy/hard. You SHOULD choose PROCEED with a quality caveat rather "
                    "than REFINE again.\n"
                )
                logger.warning("P6: Degenerate refine cycle detected, injecting PROCEED hint")
        except (json.JSONDecodeError, OSError):
            pass

    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "research_decision")
        sp = _pm.for_stage("research_decision", evolution_overlay=_overlay, analysis=analysis)
        _user = sp.user + _degenerate_hint + readiness_context
        resp = llm.chat(
            [{"role": "user", "content": _user}],
            system=sp.system,
        )
        decision_md = resp.content
    else:
        decision_md = f"""# Research Decision

## Decision
PROCEED

## Justification
Current evidence suggests measurable progress with actionable limitations.

## Next Actions
- Build detailed paper outline
- Expand ablation and uncertainty analysis in writing

Generated: {_utcnow_iso()}
"""
    (stage_dir / "decision.md").write_text(decision_md, encoding="utf-8")

    # --- Extract structured decision ---
    decision = _parse_decision(decision_md)

    # T3.1: Validate decision quality — check for minimum experiment rigor
    _quality_warnings: list[str] = []
    _dec_lower = decision_md.lower()
    if "baseline" not in _dec_lower and "control" not in _dec_lower:
        _quality_warnings.append("Decision text does not mention baselines")
    if "seed" not in _dec_lower and "replicat" not in _dec_lower and "run" not in _dec_lower:
        _quality_warnings.append("Decision text does not mention multi-seed/replicate runs")
    if "metric" not in _dec_lower and "accuracy" not in _dec_lower and "loss" not in _dec_lower:
        _quality_warnings.append("Decision text does not mention evaluation metrics")
    if _quality_warnings:
        logger.warning("T3.1: Decision quality warnings: %s", _quality_warnings)

    decision_payload = {
        "decision": decision,
        "raw_text_excerpt": decision_md[:500],
        "quality_warnings": _quality_warnings,
        "generated": _utcnow_iso(),
    }
    readiness = _build_research_readiness(run_dir, decision=decision)
    operational_decision = decision
    if (
        decision in {"refine", "pivot"}
        and _scope_locked_reproduction_can_proceed(run_dir, readiness)
    ):
        operational_decision = "proceed"
        override_reason = "scope_locked_reproduction_and_real_execution_complete"
        decision_payload.update({
            "execution_control_decision": operational_decision,
            "operational_override": True,
            "operational_override_reason": override_reason,
            "claim_scope": "evidence_limited",
        })
        readiness.update({
            "execution_control_decision": operational_decision,
            "operational_override": True,
            "operational_override_reason": override_reason,
            "claim_scope": "evidence_limited",
        })
        boundary_note = (
            "\n\n## Operational Boundary\n"
            "The scientific recommendation above remains advisory. The user locked this "
            "run to the requested baselines, and the real experiment completed with saved "
            "metrics. The workflow therefore proceeds to an evidence-limited engineering "
            "report without adding seeds, models, datasets, ablations, or scientific "
            "performance claims.\n"
        )
        with (stage_dir / "decision.md").open("a", encoding="utf-8") as handle:
            handle.write(boundary_note)
        logger.warning(
            "Research decision %s retained as advisory; proceeding to writing because "
            "the completed reproduction scope is user-locked",
            decision,
        )
    decision_payload.update({
        "readiness_level": readiness["readiness_level"],
        "readiness_score": readiness["readiness_score"],
        "writing_policy": readiness["writing_policy"],
        "scientific_claims_allowed": readiness["scientific_claims_allowed"],
        "limited_claims_allowed": readiness["limited_claims_allowed"],
    })
    (stage_dir / "decision_structured.json").write_text(
        json.dumps(decision_payload, indent=2), encoding="utf-8"
    )
    (stage_dir / "research_readiness.json").write_text(
        json.dumps(readiness, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        "Research decision: advisory=%s, operational=%s",
        decision,
        operational_decision,
    )

    return StageResult(
        stage=Stage.RESEARCH_DECISION,
        status=StageStatus.DONE,
        artifacts=("decision.md", "decision_structured.json", "research_readiness.json"),
        evidence_refs=("stage-17/decision.md", "stage-17/research_readiness.json"),
        decision=operational_decision,
    )


def _execute_knowledge_summary(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
    **kwargs: object,
) -> StageResult:
    """Summarize experiment findings into the shared knowledge base.

    Reads analysis, decision, and experiment plan, then produces a structured
    knowledge entry (JSON) and appends it to the shared knowledge base.
    L1 lobsters read this knowledge base when generating new hypotheses.
    """
    from researchclaw.llm import create_llm_client

    if llm is None:
        llm = create_llm_client(config)

    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    decision = _read_prior_artifact(run_dir, "decision.md") or ""
    exp_plan = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
    hypotheses = _read_prior_artifact(run_dir, "hypotheses.md") or ""

    summary_prompt = (
        "Based on the following research experiment, create a structured knowledge entry.\n\n"
        "## Hypotheses\n" + hypotheses[:2000] + "\n\n"
        "## Experiment Plan\n" + exp_plan[:2000] + "\n\n"
        "## Analysis\n" + analysis[:3000] + "\n\n"
        "## Decision\n" + decision[:1000] + "\n\n"
        "Create a JSON object with these fields:\n"
        '- "topic": one-line research topic\n'
        '- "hypotheses": list of hypotheses tested\n'
        '- "method": experimental methodology summary (2-3 sentences)\n'
        '- "settings": key experimental settings (model, dataset, hyperparams, hardware)\n'
        '- "results": dict of metric_name -> value for main results\n'
        '- "conclusions": list of key conclusions (what worked, what didn\'t)\n'
        '- "insights": list of surprising or useful insights for future research\n'
        '- "limitations": list of limitations\n'
        '- "suggested_directions": list of promising follow-up directions\n'
        "Return valid JSON only."
    )

    try:
        resp = llm.chat(
            [{"role": "user", "content": summary_prompt}],
            system="You are a research scientist. Summarize experiment findings into a structured knowledge entry. Return valid JSON only.",
            json_mode=True,
        )
        content = resp.content if hasattr(resp, 'content') else str(resp)

        import json as _json_ks
        try:
            entry = _json_ks.loads(content)
        except _json_ks.JSONDecodeError:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            entry = _json_ks.loads(content)

        if not isinstance(entry, dict):
            entry = {"raw": str(entry)}

        entry["project_id"] = getattr(config.project, "name", "unknown")
        entry["research_topic"] = config.research.topic
        entry["domains"] = list(config.research.domains)
        entry["timestamp"] = _utcnow_iso()

        (stage_dir / "knowledge_entry.json").write_text(
            _json_ks.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8",
        )

        # Write to shared knowledge base
        _shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
        if _shared_dir:
            import os as _os_ks
            kb_dir = Path(_shared_dir) / "knowledge_base"
            kb_dir.mkdir(parents=True, exist_ok=True)
            kb_file = kb_dir / f"{entry['project_id']}_{_utcnow_iso().replace(':', '-')}.json"
            kb_file.write_text(
                _json_ks.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8",
            )

            # Append summary to knowledge_index.jsonl for quick L1 lookup
            index_file = kb_dir / "knowledge_index.jsonl"
            compact = {
                "project": entry.get("project_id", ""),
                "topic": entry.get("topic", entry.get("research_topic", "")),
                "conclusions": entry.get("conclusions", []),
                "insights": entry.get("insights", []),
                "suggested_directions": entry.get("suggested_directions", []),
                "results": entry.get("results", {}),
            }
            with open(index_file, "a", encoding="utf-8") as f:
                f.write(_json_ks.dumps(compact, ensure_ascii=False) + "\n")

            logger.info("Knowledge entry written to shared KB: %s", kb_file.name)
            print(f"[KNOWLEDGE] Written to shared KB: {len(entry.get('conclusions', []))} conclusions, {len(entry.get('insights', []))} insights", flush=True)

    except Exception as e:
        logger.warning("Knowledge summary generation failed: %s", e)
        (stage_dir / "knowledge_entry.json").write_text(
            json.dumps({"error": str(e), "topic": config.research.topic}, indent=2),
            encoding="utf-8",
        )

    return StageResult(
        stage=Stage.KNOWLEDGE_SUMMARY,
        status=StageStatus.DONE,
        artifacts=("knowledge_entry.json",),
        evidence_refs=("stage-18/knowledge_entry.json",),
    )
