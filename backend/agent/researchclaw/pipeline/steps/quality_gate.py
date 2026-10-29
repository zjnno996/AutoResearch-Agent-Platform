"""Steps quality_gate components."""

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
from researchclaw.pipeline.support.claims import _build_claim_integrity_report
from researchclaw.pipeline.support.context import _chat_with_prompt, _get_evolution_overlay
from researchclaw.pipeline.support.parsing import _safe_json_loads
from researchclaw.pipeline.support.templates import _default_quality_report
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _execute_quality_gate(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    report: dict[str, Any] | None = None

    # BUG-25: Load experiment summary for cross-checking
    _exp_summary_text = _read_prior_artifact(run_dir, "experiment_summary.json") or ""
    _exp_summary = _safe_json_loads(_exp_summary_text, {}) if _exp_summary_text else {}
    _exp_failed = False
    if isinstance(_exp_summary, dict):
        _best_run = _exp_summary.get("best_run", {})
        if isinstance(_best_run, dict):
            _exp_failed = (
                _best_run.get("status") == "failed"
                and not _best_run.get("metrics")
            )
        # Also check if metrics_summary is empty
        if not _exp_summary.get("metrics_summary"):
            _exp_failed = True

    _claim_integrity = _build_claim_integrity_report(run_dir, revised)
    _protocol_audit: dict[str, Any] = {}
    try:
        _protocol_audit = _safe_json_loads(
            (run_dir / "stage-16" / "evaluation_protocol_audit.json").read_text(encoding="utf-8"),
            {},
        )
    except (OSError, UnicodeDecodeError):
        _protocol_audit = {}

    if llm is not None:
        _pm = prompts or PromptManager()
        # IMP-33: Evaluate the full paper instead of truncating to 12K chars.
        # Split into chunks if very long, but prefer sending the full text.
        paper_for_eval = revised[:40000] if len(revised) > 40000 else revised

        # BUG-25: Inject experiment status into quality gate prompt
        _exp_context = ""
        if _exp_summary and isinstance(_exp_summary, dict):
            _exp_status_keys = {
                k: _exp_summary.get(k) for k in (
                    "total_conditions", "total_metric_keys",
                    "metrics_summary",
                ) if _exp_summary.get(k) is not None
            }
            if _best_run := _exp_summary.get("best_run"):
                _exp_status_keys["best_run_status"] = (
                    _best_run.get("status") if isinstance(_best_run, dict) else str(_best_run)
                )
            _exp_context = (
                "\n\nExperiment summary (for cross-checking reported numbers):\n"
                + json.dumps(_exp_status_keys, indent=2, default=str)[:4000]
                + "\n\nCross-check: If the experiment status is 'failed' with "
                "empty metrics, any numerical results in tables constitute "
                "fabrication. Penalize severely.\n"
            )
        _exp_context += (
            "\n\nDeterministic claim-integrity audit (non-negotiable):\n"
            + json.dumps(_claim_integrity, ensure_ascii=False, indent=2)[:8000]
            + "\nA blocked audit must receive a revise/degraded verdict.\n"
        )
        if _protocol_audit:
            _exp_context += (
                "\n\nDeterministic evaluation-protocol audit (non-negotiable):\n"
                + json.dumps(_protocol_audit, ensure_ascii=False, indent=2)[:6000]
                + "\nIf status is not 'passed', do not present the evaluation as statistically rigorous.\n"
            )

        _overlay = _get_evolution_overlay(run_dir, "quality_gate")
        sp = _pm.for_stage(
            "quality_gate",
            evolution_overlay=_overlay,
            quality_threshold=str(config.research.quality_threshold),
            revised=paper_for_eval + _exp_context,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        parsed = _safe_json_loads(resp.content, {})
        from researchclaw.evaluation.reviews import validate_review
        report = validate_review(parsed)
    # BUG-25: If experiment failed with no metrics, cap the quality score
    if report is not None and _exp_failed:
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)) and _orig_score > 3:
            report["score_1_to_10"] = min(_orig_score, 3.0)
            report.setdefault("weaknesses", []).append(
                "Experiment failed with no metrics — any reported numerical "
                "results are unsupported and likely fabricated."
            )
            logger.warning(
                "BUG-25: Experiment failed — capping quality score from %.1f to 3.0",
                _orig_score,
            )
    if report is None:
        report = _default_quality_report(config.research.quality_threshold)
    _protocol_limited = bool(_protocol_audit) and _protocol_audit.get("status") != "passed"
    if _protocol_limited:
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)):
            report["score_1_to_10"] = min(float(_orig_score), 5.0)
        report["verdict"] = "revise"
        _weaknesses = report.setdefault("weaknesses", [])
        if not isinstance(_weaknesses, list):
            _weaknesses = [str(_weaknesses)]
            report["weaknesses"] = _weaknesses
        _weaknesses.append(
            "评估协议未满足最少 seed/统计要求，不能将当前结果表述为严格的统计结论。"
        )
        _actions = report.setdefault("required_actions", [])
        if not isinstance(_actions, list):
            _actions = [str(_actions)]
            report["required_actions"] = _actions
        _actions.append("为每个实验条件补齐计划中的独立 seed，并生成均值、标准差、95% CI 和配对检验。")
        report["evaluation_protocol_status"] = _protocol_audit.get("status")
        report["evaluation_protocol_missing_seed_conditions"] = _protocol_audit.get(
            "missing_seed_conditions", []
        )
    if _claim_integrity.get("status") == "blocked":
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)):
            report["score_1_to_10"] = min(float(_orig_score), 4.0)
        report["verdict"] = "revise"
        _weaknesses = report.setdefault("weaknesses", [])
        if not isinstance(_weaknesses, list):
            _weaknesses = [str(_weaknesses)]
            report["weaknesses"] = _weaknesses
        _weaknesses.extend(
            violation.get("message_zh", "结论证据边界未通过。")
            for violation in _claim_integrity.get("violations", [])
            if isinstance(violation, dict)
        )
        report["claim_integrity_status"] = "blocked"
        report["claim_integrity_score"] = _claim_integrity.get("integrity_score", 0)
    report.setdefault("generated", _utcnow_iso())
    (stage_dir / "quality_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    (stage_dir / "claim_integrity_report.json").write_text(
        json.dumps(_claim_integrity, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # T2.1: Enforce quality gate — fail if score below threshold
    score = report.get("score_1_to_10", 0)
    verdict = report.get("verdict", "proceed")
    threshold = config.research.quality_threshold or 5.0

    # --- Fabrication flag: collect real metrics for Stage 22 sanitization ---
    _fabrication_info: dict[str, Any] = {
        "experiment_failed": _exp_failed,
        "quality_score": score,
        "real_metric_values": [],
    }
    if isinstance(_exp_summary, dict):
        # Collect ALL real numeric values from experiment_summary.json
        _cond_summaries = _exp_summary.get("condition_summaries", {})
        if isinstance(_cond_summaries, dict):
            for cond_name, cond_data in _cond_summaries.items():
                if not isinstance(cond_data, dict):
                    continue
                cond_status = cond_data.get("status", "")
                if cond_status == "failed":
                    continue  # skip failed conditions
                for k, v in cond_data.items():
                    if isinstance(v, (int, float)) and k not in (
                        "seed_count", "total_steps", "training_steps",
                    ):
                        _fabrication_info["real_metric_values"].append(
                            round(float(v), 4)
                        )
        _ms = _exp_summary.get("metrics_summary", {})
        if isinstance(_ms, dict):
            for _mk, _mv in _ms.items():
                if isinstance(_mv, dict):
                    for _stat in ("mean", "min", "max"):
                        _sv = _mv.get(_stat)
                        if isinstance(_sv, (int, float)):
                            _fabrication_info["real_metric_values"].append(
                                round(float(_sv), 4)
                            )
    _fabrication_info["has_real_data"] = bool(
        _fabrication_info["real_metric_values"]
    )
    _fabrication_info["fabrication_suspected"] = (
        _exp_failed and not _fabrication_info["has_real_data"]
    )
    (stage_dir / "fabrication_flags.json").write_text(
        json.dumps(_fabrication_info, indent=2), encoding="utf-8"
    )

    _claim_blocked = _claim_integrity.get("status") == "blocked"
    _below_threshold = isinstance(score, (int, float)) and score < threshold
    _review_rejected = verdict in ("revise", "reject") or report.get("review_status") == "unavailable"
    if _claim_blocked or _protocol_limited or _below_threshold or _review_rejected:
        if config.research.graceful_degradation:
            logger.warning(
                "Quality gate DEGRADED: score=%s threshold=%.1f claim_integrity=%s — "
                "continuing with sanitization (graceful_degradation=True)",
                score, threshold, _claim_integrity.get("status"),
            )
            # Write degradation signal for downstream stages
            signal = {
                "score": score,
                "threshold": threshold,
                "verdict": verdict,
                "claim_integrity_status": _claim_integrity.get("status"),
                "reason": (
                    "claim_integrity_blocked" if _claim_blocked
                    else "evaluation_protocol_insufficient" if _protocol_limited
                    else "independent_review_unavailable" if report.get("review_status") == "unavailable"
                    else "manuscript_revision_requested" if _review_rejected
                    else "quality_score_below_threshold"
                ),
                "weaknesses": report.get("weaknesses", []),
                "evaluation_protocol_status": _protocol_audit.get("status") if _protocol_audit else None,
                "generated": _utcnow_iso(),
            }
            (run_dir / "degradation_signal.json").write_text(
                json.dumps(signal, indent=2), encoding="utf-8"
            )
            return StageResult(
                stage=Stage.QUALITY_GATE,
                status=StageStatus.DONE,
                artifacts=(
                    "quality_report.json",
                    "fabrication_flags.json",
                    "claim_integrity_report.json",
                ),
                evidence_refs=("stage-23/quality_report.json", "stage-23/claim_integrity_report.json"),
                decision="degraded",
            )
        logger.warning(
            "Quality gate FAILED: score=%s threshold=%.1f verdict=%s claim_integrity=%s",
            score, threshold, verdict, _claim_integrity.get("status"),
        )
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=(
                "quality_report.json",
                "fabrication_flags.json",
                "claim_integrity_report.json",
            ),
            evidence_refs=("stage-23/quality_report.json", "stage-23/claim_integrity_report.json"),
            error=(
                "Claim integrity audit blocked the paper. "
                if _claim_blocked else
                "Evaluation protocol is insufficient for rigorous claims. "
                if _protocol_limited else
                "Independent manuscript review is unavailable or requests revision. "
                if _review_rejected else
                f"Quality score {float(score):.1f}/10 below threshold {threshold:.1f}. "
            ) + "Paper needs revision before export.",
        )

    logger.info(
        "Quality gate PASSED: score %.1f >= threshold %.1f",
        score, threshold,
    )
    # A previous S25 export may have left a degradation signal behind.  The
    # current S23 result is authoritative, so do not let stale state cause a
    # later clean export to be labelled as degraded.
    _stale_degradation_signal = run_dir / "degradation_signal.json"
    if _stale_degradation_signal.exists():
        try:
            _stale_degradation_signal.unlink()
            logger.info("Quality gate PASSED: removed stale degradation signal")
        except OSError as exc:
            logger.warning("Could not remove stale degradation signal: %s", exc)
    return StageResult(
        stage=Stage.QUALITY_GATE,
        status=StageStatus.DONE,
        artifacts=(
            "quality_report.json",
            "fabrication_flags.json",
            "claim_integrity_report.json",
        ),
        evidence_refs=("stage-23/quality_report.json", "stage-23/claim_integrity_report.json"),
    )
