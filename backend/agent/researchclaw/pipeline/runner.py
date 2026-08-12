from __future__ import annotations

import json
import importlib
import logging
import os
import shutil
import tempfile
import time as _time
from pathlib import Path

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.evolution import EvolutionStore, extract_lessons
from researchclaw.knowledge.base import write_stage_to_kb
from researchclaw.pipeline.executor import StageResult, execute_stage
from researchclaw.pipeline.stages import (
    DECISION_ROLLBACK,
    MAX_DECISION_PIVOTS,
    NONCRITICAL_STAGES,
    STAGE_SEQUENCE,
    Stage,
    StageStatus,
)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _should_start(stage: Stage, from_stage: Stage, started: bool) -> bool:
    if started:
        return True
    return stage == from_stage


def _build_pipeline_summary(
    *,
    run_id: str,
    results: list[StageResult],
    from_stage: Stage,
    run_dir: Path | None = None,
) -> dict[str, object]:
    invocation_done = sum(1 for item in results if item.status == StageStatus.DONE)
    invocation_blocked = sum(
        1 for item in results if item.status == StageStatus.BLOCKED_APPROVAL
    )
    invocation_failed = sum(
        1 for item in results if item.status == StageStatus.FAILED
    )
    stages_executed = len(results)
    stages_done = invocation_done
    final_stage = int(results[-1].stage) if results else int(from_stage)
    final_status = results[-1].status.value if results else "no_stages"

    # A layer/repair invocation may execute only S23-S26 or S26.  Report
    # cumulative pipeline progress from the checkpoint instead of replacing a
    # complete 26-stage run with a misleading "1/1 stages" summary.
    if run_dir is not None and int(from_stage) > int(Stage.TOPIC_INIT):
        checkpoint_path = run_dir / "checkpoint.json"
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint_stage = int(checkpoint.get("last_completed_stage", 0) or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            checkpoint_stage = 0
        if checkpoint_stage > 0:
            stages_executed = max(stages_executed, checkpoint_stage)
            stages_done = max(stages_done, checkpoint_stage)
            final_stage = max(final_stage, checkpoint_stage)
            if checkpoint_stage >= int(Stage.CITATION_VERIFY):
                final_status = StageStatus.DONE.value

    prior_degraded = False
    degradation_reasons: list[str] = []
    if run_dir is not None:
        prior_summary_path = run_dir / "pipeline_summary.json"
        try:
            prior_summary = json.loads(prior_summary_path.read_text(encoding="utf-8"))
            prior_degraded = bool(prior_summary.get("degraded"))
            degradation_reasons.extend(
                str(item) for item in (prior_summary.get("degradation_reasons", []) or [])
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        signal_path = run_dir / "degradation_signal.json"
        try:
            signal = json.loads(signal_path.read_text(encoding="utf-8"))
            prior_degraded = True
            reason = str(signal.get("reason", "downstream_degradation_signal"))
            if reason:
                degradation_reasons.append(reason)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    invocation_degraded = any(r.decision == "degraded" for r in results)
    if invocation_degraded:
        degradation_reasons.append("stage_decision_degraded")

    summary: dict[str, object] = {
        "run_id": run_id,
        "stages_total": len(STAGE_SEQUENCE),
        "stages_executed": stages_executed,
        "stages_done": stages_done,
        "stages_blocked": invocation_blocked,
        "stages_failed": invocation_failed,
        "invocation_stages_executed": len(results),
        "invocation_stages_done": invocation_done,
        "degraded": prior_degraded or invocation_degraded,
        "degradation_reasons": sorted(set(degradation_reasons)),
        "from_stage": int(from_stage),
        "final_stage": final_stage,
        "final_status": final_status,
        "generated": _utcnow_iso(),
        "content_metrics": _collect_content_metrics(run_dir),
    }
    if run_dir is not None:
        readiness_path = run_dir / "stage-17" / "research_readiness.json"
        final_claim_path = run_dir / "stage-25" / "final_claim_integrity_report.json"
        readiness = {}
        final_claim = {}
        degradation = {}
        quality = {}
        protocol_audit = {}
        try:
            readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            final_claim = json.loads(final_claim_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            degradation = json.loads((run_dir / "degradation_signal.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            quality = json.loads((run_dir / "stage-23" / "quality_report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            protocol_audit = json.loads((run_dir / "stage-16" / "evaluation_protocol_audit.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        summary["scientific_status"] = {
            "readiness_level": readiness.get("readiness_level", "unknown"),
            "writing_policy": readiness.get("writing_policy", "unknown"),
            "final_claim_integrity": final_claim.get("status", "not_run"),
            "quality_verdict": quality.get("verdict", "not_run"),
            "quality_score": quality.get("score_1_to_10"),
            "export_status": "degraded" if degradation else "passed",
            "degradation_reason": degradation.get("reason") if degradation else None,
            "degradation_quality_reasons": degradation.get("quality_reasons", []) if degradation else [],
            "evaluation_protocol_status": protocol_audit.get("status", "not_run"),
            "evaluation_protocol_missing_seed_conditions": protocol_audit.get("missing_seed_conditions", []),
            "evaluation_protocol_writing_policy": protocol_audit.get("writing_policy", "unknown"),
        }
        # Repair invocations may resolve a previously blocked gate.  Keep
        # historical degradation only when it still describes current
        # artifacts; otherwise the UI sees contradictory current status.
        current_reasons = set(summary.get("degradation_reasons", []) or [])
        if final_claim.get("status") == "passed":
            current_reasons.discard("final_claim_integrity_blocked")
        if protocol_audit.get("status") == "passed":
            current_reasons.discard("evaluation_protocol_insufficient")
        summary["degradation_reasons"] = sorted(current_reasons)
        summary["degraded"] = bool(current_reasons)
    return summary


def _write_pipeline_summary(run_dir: Path, summary: dict[str, object]) -> None:
    (run_dir / "pipeline_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )


def _write_checkpoint(run_dir: Path, stage: Stage, run_id: str) -> None:
    """Write checkpoint atomically via temp file + rename to prevent corruption."""
    checkpoint = {
        "last_completed_stage": int(stage),
        "last_completed_name": stage.name,
        "run_id": run_id,
        "timestamp": _utcnow_iso(),
    }
    target = run_dir / "checkpoint.json"
    fd, tmp_path = tempfile.mkstemp(dir=run_dir, suffix=".tmp", prefix="checkpoint_")
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(checkpoint, indent=2))
        Path(tmp_path).replace(target)
    except BaseException:
        # Close fd if open() itself failed (fd not yet owned by file object);
        # harmless OSError if the with-block already closed it.
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _write_heartbeat(run_dir: Path, stage: Stage, run_id: str) -> None:
    """Write heartbeat file for sentinel watchdog monitoring."""
    import os

    heartbeat = {
        "pid": os.getpid(),
        "last_stage": int(stage),
        "last_stage_name": stage.name,
        "run_id": run_id,
        "timestamp": _utcnow_iso(),
    }
    (run_dir / "heartbeat.json").write_text(
        json.dumps(heartbeat, indent=2), encoding="utf-8"
    )


def read_checkpoint(run_dir: Path) -> Stage | None:
    """Read checkpoint and return the NEXT stage to execute, or None if no checkpoint."""
    cp_path = run_dir / "checkpoint.json"
    if not cp_path.exists():
        return None
    try:
        data = json.loads(cp_path.read_text(encoding="utf-8"))
        last_num = data.get("last_completed_stage")
        if last_num is None:
            return None
        for i, stage in enumerate(STAGE_SEQUENCE):
            if int(stage) == last_num:
                if i + 1 < len(STAGE_SEQUENCE):
                    return STAGE_SEQUENCE[i + 1]
                return None
        return None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def resume_from_checkpoint(
    run_dir: Path, default_stage: Stage = Stage.TOPIC_INIT
) -> Stage:
    """Resolve the stage to resume from using checkpoint metadata."""
    next_stage = read_checkpoint(run_dir)
    return next_stage if next_stage is not None else default_stage


def _collect_content_metrics(run_dir: Path | None) -> dict[str, object]:
    """Collect content authenticity metrics from stage outputs."""
    metrics: dict[str, object] = {
        "template_ratio": None,
        "citation_verify_score": None,
        "total_citations": None,
        "verified_citations": None,
        "degraded_sources": [],
    }
    if run_dir is None:
        return metrics

    draft_path = run_dir / "stage-20" / "paper_draft.md"
    if not draft_path.exists():
        draft_path = run_dir / "stage-17" / "paper_draft.md"
    if draft_path.exists():
        try:
            quality_module = importlib.import_module("researchclaw.quality")
            compute_template_ratio = quality_module.compute_template_ratio
            text = draft_path.read_text(encoding="utf-8")
            metrics["template_ratio"] = round(compute_template_ratio(text), 4)
        except (
            AttributeError,
            ModuleNotFoundError,
            UnicodeDecodeError,
            OSError,
            ValueError,
            TypeError,
        ):
            pass

    verify_path = None
    for candidate in [
        run_dir / "stage-26" / "verification_report.json",
        run_dir / "stage-23" / "verification_report.json",
        run_dir / "deliverables" / "verification_report.json",
    ]:
        if candidate.exists() and candidate.stat().st_size > 0:
            verify_path = candidate
            break
    if verify_path is not None:
        try:
            vdata = json.loads(verify_path.read_text(encoding="utf-8"))
            if isinstance(vdata, dict):
                summary = vdata.get("summary", vdata)
                total = summary.get("total", 0) if isinstance(summary, dict) else None
                verified = summary.get("verified", 0) if isinstance(summary, dict) else None
                if isinstance(total, int | float) and isinstance(verified, int | float):
                    total_num = int(total)
                    verified_num = int(verified)
                    metrics["total_citations"] = total_num
                    metrics["verified_citations"] = verified_num
                    if total_num > 0:
                        metrics["citation_verify_score"] = round(
                            verified_num / total_num, 4
                        )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    return metrics


def _mark_forced_writing_transition(run_dir: Path, reason: str) -> None:
    """Record that orchestration proceeded while scientific decision stayed REFINE/PIVOT."""
    stage_dir = run_dir / f"stage-{int(Stage.RESEARCH_DECISION):02d}"
    readiness_path = stage_dir / "research_readiness.json"
    decision_path = stage_dir / "decision_structured.json"
    for path in (readiness_path, decision_path):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        payload.update({
            "execution_control_decision": "proceed_to_writing",
            "forced_proceed_after_max_pivots": True,
            "forced_proceed_reason": reason,
        })
        if path == readiness_path:
            existing = str(payload.get("user_facing_status_zh", "")).strip()
            payload["user_facing_status_zh"] = (
                existing
                + " 已达到自动迭代上限，系统继续生成受限报告；这不表示科研证据已通过。"
            ).strip()
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def execute_pipeline(
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    from_stage: Stage = Stage.TOPIC_INIT,
    to_stage: Stage | None = None,
    auto_approve_gates: bool = False,
    stop_on_gate: bool = False,
    skip_noncritical: bool = False,
    kb_root: Path | None = None,
) -> list[StageResult]:
    """Execute pipeline stages sequentially from `from_stage` to `to_stage` (inclusive)."""

    results: list[StageResult] = []
    started = False
    total_stages = len(STAGE_SEQUENCE)

    for stage in STAGE_SEQUENCE:
        if to_stage is not None and int(stage) > int(to_stage):
            break
        started = _should_start(stage, from_stage, started)
        if not started:
            continue

        stage_num = int(stage)
        prefix = f"[{run_id}] Stage {stage_num:02d}/{total_stages}"

        fb_path = run_dir / "human_feedback.jsonl"
        if fb_path.exists():
            try:
                lines = [l for l in fb_path.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
                if lines:
                    print(f"{prefix} {stage.name} — {len(lines)} human feedback item(s) pending")
            except OSError:
                pass

        print(f"{prefix} {stage.name} — running...")
        _write_heartbeat(run_dir, stage, run_id)
        t0 = _time.monotonic()

        result = execute_stage(
            stage,
            run_dir=run_dir,
            run_id=run_id,
            config=config,
            adapters=adapters,
            auto_approve_gates=auto_approve_gates,
        )
        elapsed = _time.monotonic() - t0
        if result.status == StageStatus.DONE:
            arts = ", ".join(result.artifacts) if result.artifacts else "none"
            if result.decision == "degraded":
                print(
                    f"{prefix} {stage.name} — DEGRADED ({elapsed:.1f}s) "
                    f"— continuing with sanitization → {arts}"
                )
            else:
                print(f"{prefix} {stage.name} — done ({elapsed:.1f}s) → {arts}")
        elif result.status == StageStatus.FAILED:
            err = result.error or "unknown error"
            print(f"{prefix} {stage.name} — FAILED ({elapsed:.1f}s) — {err}")
        elif result.status == StageStatus.BLOCKED_APPROVAL:
            print(f"{prefix} {stage.name} — blocked (awaiting approval)")
        results.append(result)

        if kb_root is not None and result.status == StageStatus.DONE:
            try:
                stage_dir = run_dir / f"stage-{int(stage):02d}"
                write_stage_to_kb(
                    kb_root,
                    stage_id=int(stage),
                    stage_name=stage.name.lower(),
                    run_id=run_id,
                    artifacts=list(result.artifacts),
                    stage_dir=stage_dir,
                    backend=config.knowledge_base.backend,
                    topic=config.research.topic,
                )
            except Exception:  # noqa: BLE001
                pass

        if result.status == StageStatus.DONE:
            _write_checkpoint(run_dir, stage, run_id)

        # --- Heartbeat for sentinel watchdog ---
        _write_heartbeat(run_dir, stage, run_id)

        # --- PIVOT/REFINE decision handling ---
        if (
            stage == Stage.RESEARCH_DECISION
            and result.status == StageStatus.DONE
            and result.decision in DECISION_ROLLBACK
        ):
            pivot_count = _read_pivot_count(run_dir)
            # R6-4: Skip REFINE if experiment metrics are empty for consecutive cycles
            if pivot_count > 0 and _consecutive_empty_metrics(run_dir, pivot_count):
                logger.warning(
                    "Consecutive REFINE cycles produced empty metrics — forcing PROCEED"
                )
                print(
                    f"[{run_id}] Consecutive empty metrics across REFINE cycles — forcing PROCEED"
                )
            elif pivot_count < MAX_DECISION_PIVOTS:
                rollback_target = DECISION_ROLLBACK[result.decision]
                _record_decision_history(
                    run_dir, result.decision, rollback_target, pivot_count + 1
                )
                logger.info(
                    "Decision %s: rolling back to %s (attempt %d/%d)",
                    result.decision.upper(),
                    rollback_target.name,
                    pivot_count + 1,
                    MAX_DECISION_PIVOTS,
                )
                print(
                    f"[{run_id}] Decision: {result.decision.upper()} → "
                    f"rollback to {rollback_target.name} "
                    f"(attempt {pivot_count + 1}/{MAX_DECISION_PIVOTS})"
                )
                # Version existing stage directories before overwriting
                _version_rollback_stages(
                    run_dir, rollback_target, pivot_count + 1
                )
                # Recurse from rollback target
                pivot_results = execute_pipeline(
                    run_dir=run_dir,
                    run_id=run_id,
                    config=config,
                    adapters=adapters,
                    from_stage=rollback_target,
                    to_stage=to_stage,
                    auto_approve_gates=auto_approve_gates,
                    stop_on_gate=stop_on_gate,
                    skip_noncritical=skip_noncritical,
                    kb_root=kb_root,
                )
                results.extend(pivot_results)
                break  # Exit current loop; recursive call handles the rest
            else:
                # Quality gate: check if experiment results are actually usable
                _quality_ok, _quality_msg = _check_experiment_quality(
                    run_dir, pivot_count
                )
                _forced_reason = "maximum decision pivots reached"
                if not _quality_ok:
                    _forced_reason += f"; quality warning: {_quality_msg}"
                    logger.warning(
                        "Max pivot attempts (%d) reached — forcing PROCEED "
                        "with quality warning: %s",
                        MAX_DECISION_PIVOTS,
                        _quality_msg,
                    )
                    print(
                        f"[{run_id}] QUALITY WARNING: {_quality_msg}"
                    )
                    # Write quality warning to run directory
                    _qw_path = run_dir / "quality_warning.txt"
                    _qw_path.write_text(
                        f"Max pivots ({MAX_DECISION_PIVOTS}) reached.\n"
                        f"Quality gate failed: {_quality_msg}\n"
                        f"Paper will be written but may have significant issues.\n",
                        encoding="utf-8",
                    )
                else:
                    logger.warning(
                        "Max pivot attempts (%d) reached — forcing PROCEED",
                        MAX_DECISION_PIVOTS,
                    )
                _mark_forced_writing_transition(run_dir, _forced_reason)
                print(
                    f"[{run_id}] Max pivot attempts reached — forcing PROCEED"
                )

        if result.status == StageStatus.FAILED:
            if skip_noncritical and stage in NONCRITICAL_STAGES:
                logger.warning("Noncritical stage %s failed - skipping", stage.name)
            else:
                break
        if result.status == StageStatus.BLOCKED_APPROVAL and stop_on_gate:
            break

    summary = _build_pipeline_summary(
        run_id=run_id,
        results=results,
        from_stage=from_stage,
        run_dir=run_dir,
    )
    _write_pipeline_summary(run_dir, summary)

    # --- LLM trace summary: aggregate request/input/output records ---
    try:
        from researchclaw.observability.tracing import write_llm_trace_summary
        write_llm_trace_summary(run_dir)
    except Exception:  # noqa: BLE001
        logger.warning("LLM trace summary generation failed (non-blocking)")

    # --- Evolution: extract and store lessons ---
    lessons: list[object] = []
    try:
        lessons = extract_lessons(results, run_id=run_id, run_dir=run_dir)
        if lessons:
            store = EvolutionStore(run_dir / "evolution")
            store.append_many(lessons)
            logger.info("Extracted %d lessons from pipeline run", len(lessons))
    except Exception:  # noqa: BLE001
        logger.warning("Evolution lesson extraction failed (non-blocking)")

    # --- MetaClaw bridge: convert high-severity lessons to skills ---
    try:
        _metaclaw_post_pipeline(config, results, lessons, run_id, run_dir)
    except Exception:  # noqa: BLE001
        logger.warning("MetaClaw post-pipeline hook failed (non-blocking)")

    # --- Package deliverables into a single folder ---
    try:
        deliverables_dir = _package_deliverables(run_dir, run_id, config)
        if deliverables_dir is not None:
            print(f"[{run_id}] Deliverables packaged → {deliverables_dir}")
    except Exception:  # noqa: BLE001
        logger.warning("Deliverables packaging failed (non-blocking)")

    return results


def _package_deliverables(
    run_dir: Path,
    run_id: str,
    config: RCConfig,
) -> Path | None:
    """Collect all final user-facing deliverables into a single ``deliverables/`` folder.

    Returns the deliverables directory path, or None if nothing was packaged.

    Packaged artifacts (best-available version selected automatically):
    - paper_final.md          — Final paper (Markdown)
    - paper.tex               — Conference-ready LaTeX
    - references.bib          — BibTeX bibliography
    - code/                   — Experiment code package
    - verification_report.json — Citation verification report (if available)
    """
    dest = run_dir / "deliverables"
    dest.mkdir(parents=True, exist_ok=True)

    packaged: list[str] = []

    # --- 1. Final paper (Markdown) ---
    # Prefer verified version (stage 26) over base export (stage 25).
    # Older runs used stage-23/stage-22 for these artifacts, so keep them as
    # compatibility fallbacks.
    paper_md = None
    for candidate in [
        run_dir / "stage-26" / "paper_final_verified.md",
        run_dir / "stage-25" / "paper_final.md",
        run_dir / "stage-23" / "paper_final_verified.md",
        run_dir / "stage-22" / "paper_final.md",
    ]:
        if candidate.exists() and candidate.stat().st_size > 0:
            paper_md = candidate
            break
    if paper_md is not None:
        shutil.copy2(paper_md, dest / "paper_final.md")
        packaged.append("paper_final.md")

    # --- 2. LaTeX paper ---
    # IMP-13: If Stage 26 produced verified markdown, regenerate paper.tex
    # from it so that hallucinated citations removed in Stage 26 are also
    # absent from the LaTeX.  Fall back to the Stage 22 .tex otherwise.
    tex_regenerated = False
    verified_md = run_dir / "stage-26" / "paper_final_verified.md"
    if (
        paper_md is not None
        and paper_md == verified_md
        and verified_md.exists()
        and verified_md.stat().st_size > 0
    ):
        try:
            from researchclaw.templates import get_template, markdown_to_latex
            from researchclaw.pipeline.executor import _extract_paper_title

            tpl = get_template(config.export.target_conference)
            v_text = verified_md.read_text(encoding="utf-8")
            tex_content = markdown_to_latex(
                v_text,
                tpl,
                title=_extract_paper_title(v_text),
                authors=config.export.authors,
                bib_file=config.export.bib_file,
            )
            # IMP-17: Quality check — ensure regenerated LaTeX has
            # proper structure (abstract, multiple sections)
            _has_abstract = (
                "\\begin{abstract}" in tex_content
                and tex_content.split("\\begin{abstract}")[1]
                .split("\\end{abstract}")[0]
                .strip()
            )
            _section_count = tex_content.count("\\section{")
            if _has_abstract and _section_count >= 3:
                (dest / "paper.tex").write_text(tex_content, encoding="utf-8")
                packaged.append("paper.tex")
                tex_regenerated = True
                logger.info(
                    "Deliverables: regenerated paper.tex from verified markdown"
                )
            else:
                logger.warning(
                    "Regenerated paper.tex has poor structure "
                    "(abstract=%s, sections=%d) — using Stage 22 version",
                    bool(_has_abstract),
                    _section_count,
                )
        except Exception:  # noqa: BLE001
            logger.debug("paper.tex regeneration from verified md failed")

    if not tex_regenerated:
        for tex_src in [
            run_dir / "stage-25" / "paper.tex",
            run_dir / "stage-22" / "paper.tex",
            run_dir / "stage-22" / "latex_package" / "main.tex",
        ]:
            if tex_src.exists() and tex_src.stat().st_size > 0:
                shutil.copy2(tex_src, dest / "paper.tex")
                packaged.append("paper.tex")
                break

    # --- 3. References (BibTeX) ---
    # Prefer verified bib (stage 26) over base bib (stage 25)
    bib_src = None
    for candidate in [
        run_dir / "stage-26" / "references_verified.bib",
        run_dir / "stage-25" / "references.bib",
        run_dir / "stage-23" / "references_verified.bib",
        run_dir / "stage-22" / "references.bib",
        run_dir / "stage-22" / "latex_package" / "references.bib",
    ]:
        if candidate.exists() and candidate.stat().st_size > 0:
            bib_src = candidate
            break
    if bib_src is not None:
        shutil.copy2(bib_src, dest / "references.bib")
        packaged.append("references.bib")

    # --- 4. Experiment code package ---
    code_src = run_dir / "stage-25" / "code"
    if not code_src.is_dir():
        code_src = run_dir / "stage-22" / "code"
    if code_src.is_dir():
        code_dest = dest / "code"
        if code_dest.exists():
            shutil.rmtree(code_dest)
        shutil.copytree(code_src, code_dest)
        packaged.append("code/")

    # --- 5. Verification report (optional) ---
    verify_src = run_dir / "stage-26" / "verification_report.json"
    if not verify_src.exists():
        verify_src = run_dir / "stage-23" / "verification_report.json"
    if verify_src.exists() and verify_src.stat().st_size > 0:
        shutil.copy2(verify_src, dest / "verification_report.json")
        packaged.append("verification_report.json")

    # --- 5a. Evidence boundary reports (user-facing provenance) ---
    evidence_artifacts = [
        ("experiment_provenance.json", [
            run_dir / "stage-16" / "experiment_provenance.json",
            run_dir / "stage-14" / "runs" / "experiment_provenance.json",
        ]),
        ("experiment_summary.json", [
            run_dir / "stage-16" / "experiment_summary.json",
        ]),
        ("analysis.md", [
            run_dir / "stage-16" / "analysis.md",
        ]),
        ("evaluation_protocol_audit.json", [
            run_dir / "stage-16" / "evaluation_protocol_audit.json",
        ]),
        ("exp_plan_diagnostics.json", [
            run_dir / "stage-09" / "exp_plan_diagnostics.json",
        ]),
        ("research_readiness.json", [
            run_dir / "stage-17" / "research_readiness.json",
        ]),
        ("decision_structured.json", [
            run_dir / "stage-17" / "decision_structured.json",
        ]),
        ("claim_integrity_report.json", [
            run_dir / "stage-23" / "claim_integrity_report.json",
        ]),
        ("final_claim_integrity_report.json", [
            run_dir / "stage-25" / "final_claim_integrity_report.json",
        ]),
        ("reproducibility_manifest.json", [
            run_dir / "stage-25" / "reproducibility_manifest.json",
        ]),
        ("compilation_quality.json", [
            run_dir / "stage-25" / "compilation_quality.json",
            run_dir / "stage-22" / "compilation_quality.json",
        ]),
    ]
    for out_name, candidates in evidence_artifacts:
        for src in candidates:
            if src.exists() and src.stat().st_size > 0:
                shutil.copy2(src, dest / out_name)
                packaged.append(out_name)
                break

    # --- 5b. Sanitization report (degraded mode) ---
    san_src = run_dir / "stage-22" / "sanitization_report.json"
    if san_src.exists() and san_src.stat().st_size > 0:
        shutil.copy2(san_src, dest / "sanitization_report.json")
        packaged.append("sanitization_report.json")

    # --- 6. Charts (optional) ---
    charts_src = run_dir / "stage-25" / "charts"
    if not charts_src.is_dir():
        charts_src = run_dir / "stage-22" / "charts"
    if not charts_src.is_dir():
        charts_src = run_dir / "stage-16" / "charts"
    if charts_src.is_dir() and any(charts_src.iterdir()):
        charts_dest = dest / "charts"
        if charts_dest.exists():
            shutil.rmtree(charts_dest)
        shutil.copytree(charts_src, charts_dest)
        packaged.append("charts/")

    # --- 7. Conference style files (.sty, .bst) ---
    try:
        from researchclaw.templates import get_template

        tpl = get_template(config.export.target_conference)
        style_files = tpl.get_style_files()
        for sf in style_files:
            shutil.copy2(sf, dest / sf.name)
            packaged.append(sf.name)
        if style_files:
            logger.info(
                "Deliverables: bundled %d style files for %s",
                len(style_files),
                tpl.display_name,
            )
    except Exception:  # noqa: BLE001
        logger.debug("Style file bundling skipped (template lookup failed)")

    # --- 8. Verify & repair cite key coverage (IMP-12 + IMP-14) ---
    tex_path = dest / "paper.tex"
    bib_path = dest / "references.bib"
    if tex_path.exists() and bib_path.exists():
        try:
            tex_text = tex_path.read_text(encoding="utf-8")
            bib_text = bib_path.read_text(encoding="utf-8")
            import re as _re

            # IMP-15: Deduplicate .bib entries
            _seen_bib_keys: set[str] = set()
            _deduped_entries: list[str] = []
            for _bm in _re.finditer(
                r"(@\w+\{([^,]+),.*?\n\})", bib_text, _re.DOTALL
            ):
                _bkey = _bm.group(2).strip()
                if _bkey not in _seen_bib_keys:
                    _seen_bib_keys.add(_bkey)
                    _deduped_entries.append(_bm.group(1))
            if len(_deduped_entries) < len(
                list(_re.finditer(r"@\w+\{", bib_text))
            ):
                bib_text = "\n\n".join(_deduped_entries) + "\n"
                bib_path.write_text(bib_text, encoding="utf-8")
                logger.info(
                    "Deliverables: deduplicated .bib → %d entries",
                    len(_deduped_entries),
                )

            # Collect all cite keys from \cite{key1, key2}
            all_cite_keys: set[str] = set()
            for cm in _re.finditer(r"\\cite\{([^}]+)\}", tex_text):
                all_cite_keys.update(k.strip() for k in cm.group(1).split(","))
            bib_keys = set(_re.findall(r"@\w+\{([^,]+),", bib_text))
            missing = all_cite_keys - bib_keys

            # IMP-14: Strip orphaned \cite{key} from paper.tex
            if missing:
                logger.warning(
                    "Deliverables: stripping %d orphaned cite keys from "
                    "paper.tex: %s",
                    len(missing),
                    sorted(missing)[:10],
                )

                def _filter_cite(m: _re.Match[str]) -> str:
                    keys = [k.strip() for k in m.group(1).split(",")]
                    kept = [k for k in keys if k not in missing]
                    if not kept:
                        return ""
                    return "\\cite{" + ", ".join(kept) + "}"

                tex_text = _re.sub(r"\\cite\{([^}]+)\}", _filter_cite, tex_text)
                # Clean up whitespace artifacts: double spaces, space before period
                tex_text = _re.sub(r"  +", " ", tex_text)
                tex_text = _re.sub(r" ([.,;:)])", r"\1", tex_text)
                tex_path.write_text(tex_text, encoding="utf-8")
                logger.info(
                    "Deliverables: paper.tex repaired — all remaining cite "
                    "keys verified"
                )
            else:
                logger.info(
                    "Deliverables: all %d cite keys verified in references.bib",
                    len(all_cite_keys),
                )
        except Exception:  # noqa: BLE001
            logger.debug("Cite key verification/repair skipped")

    # --- 9. IMP-18: Compile LaTeX to verify paper.tex ---
    pdf_compiled = False
    if tex_path.exists() and bib_path.exists():
        try:
            from researchclaw.templates.compiler import compile_latex

            compile_result = compile_latex(tex_path, max_attempts=3, timeout=120)
            if compile_result.success:
                logger.info("IMP-18: paper.tex compiles successfully")
                # Keep the generated PDF
                pdf_path = dest / tex_path.stem
                pdf_file = dest / (tex_path.stem + ".pdf")
                if pdf_file.exists():
                    packaged.append(f"{tex_path.stem}.pdf")
                    pdf_compiled = True
            else:
                logger.warning(
                    "IMP-18: paper.tex compilation failed after %d attempts: %s",
                    compile_result.attempts,
                    compile_result.errors[:3],
                )
            if compile_result.fixes_applied:
                logger.info(
                    "IMP-18: Applied %d auto-fixes: %s",
                    len(compile_result.fixes_applied),
                    compile_result.fixes_applied,
                )
        except Exception:  # noqa: BLE001
            logger.debug("IMP-18: LaTeX compilation skipped (non-blocking)")

    if not packaged:
        # Nothing to package — remove empty dir
        dest.rmdir()
        return None

    # --- Write manifest ---
    manifest = {
        "run_id": run_id,
        "target_conference": config.export.target_conference,
        "files": packaged,
        "generated": _utcnow_iso(),
        "paper_pdf_available": pdf_compiled or (dest / "paper.pdf").exists(),
        "degraded": (run_dir / "degradation_signal.json").exists(),
        "user_notice_zh": (
            "paper.pdf 已生成，可下载。"
            if (pdf_compiled or (dest / "paper.pdf").exists())
            else "paper.tex 已生成但 paper.pdf 未成功编译；请查看 compilation_quality.json 或日志。"
        ),
        "notes": {
            "paper_final.md": "Final paper in Markdown format",
            "paper.tex": f"Conference-ready LaTeX ({config.export.target_conference})",
            "paper.pdf": "Compiled PDF when LaTeX compilation succeeds",
            "references.bib": "BibTeX bibliography (verified citations only)",
            "code/": "Experiment source code with requirements.txt",
            "verification_report.json": "Citation integrity & relevance verification",
            "experiment_provenance.json": "What actually executed and whether scientific claims are allowed",
            "experiment_summary.json": "Deterministic summary of parsed experiment metrics",
            "analysis.md": "Result analysis with evidence boundary",
            "exp_plan_diagnostics.json": "Experiment-plan parse/fallback and benchmark-validation diagnostics",
            "research_readiness.json": "Fused planning/execution/evidence score and non-negotiable writing policy",
            "decision_structured.json": "Machine-readable research decision linked to readiness and claim scope",
            "claim_integrity_report.json": "Deterministic audit of unsupported numbers, overclaims, and missing limitations",
            "final_claim_integrity_report.json": "Post-export claim audit; final polishing cannot bypass the evidence boundary",
            "reproducibility_manifest.json": "Runtime, package, hardware, protocol, and bounded dataset hash manifest",
            "compilation_quality.json": "LaTeX/PDF compilation quality checks",
            "charts/": "Result visualizations",
        },
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    logger.info(
        "Deliverables packaged: %s (%d items)",
        dest,
        len(packaged),
    )
    return dest


def _version_rollback_stages(
    run_dir: Path, rollback_target: Stage, attempt: int
) -> None:
    """Rename stage directories that will be overwritten by a PIVOT/REFINE.

    For example, if rolling back to Stage 8 (attempt 2), renames:
      stage-08/ → stage-08_v1/
      stage-09/ → stage-09_v1/
      ... up to stage-15/
    """
    import shutil

    rollback_num = int(rollback_target)
    # Stages from rollback target up to RESEARCH_DECISION (15) will be rerun
    decision_num = int(Stage.RESEARCH_DECISION)

    for stage_num in range(rollback_num, decision_num + 1):
        stage_dir = run_dir / f"stage-{stage_num:02d}"
        if stage_dir.exists():
            version_dir = run_dir / f"stage-{stage_num:02d}_v{attempt}"
            if version_dir.exists():
                shutil.rmtree(version_dir)
            stage_dir.rename(version_dir)
            logger.debug(
                "Versioned %s → %s", stage_dir.name, version_dir.name
            )


def _consecutive_empty_metrics(run_dir: Path, pivot_count: int) -> bool:
    """R6-4: Check if the current and previous REFINE cycles both produced empty metrics."""
    # Check the most recent analysis summary (stage 16) and its predecessor.
    current = run_dir / "stage-16" / "experiment_summary.json"
    prev = run_dir / f"stage-16_v{pivot_count}" / "experiment_summary.json"
    for path in (current, prev):
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Check all possible metric locations
            has_metrics = False
            ms = data.get("metrics_summary", {})
            if isinstance(ms, dict) and ms:
                has_metrics = True
            br = data.get("best_run", {})
            if isinstance(br, dict) and br.get("metrics"):
                has_metrics = True
            if has_metrics:
                return False  # At least one cycle had real metrics
        except (json.JSONDecodeError, OSError, AttributeError):
            return False
    return True  # Both cycles had empty metrics


def _check_experiment_quality(
    run_dir: Path, pivot_count: int
) -> tuple[bool, str]:
    """Quality gate before forced PROCEED.

    Returns (ok, message). ok=False means experiment results have critical
    quality issues and the forced-PROCEED paper will likely be poor.
    """
    # Find most recent experiment summary
    summary_path = run_dir / "stage-16" / "experiment_summary.json"
    if not summary_path.exists():
        for v in range(pivot_count, 0, -1):
            alt = run_dir / f"stage-16_v{v}" / "experiment_summary.json"
            if alt.exists():
                summary_path = alt
                break

    if not summary_path.exists():
        return False, "No experiment_summary.json found — no metrics produced"

    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, "experiment_summary.json is malformed"

    # Check 1: Are all metrics zero?
    ms = data.get("metrics_summary", {})
    if isinstance(ms, dict):
        values = []
        for k, v in ms.items():
            if isinstance(v, (int, float)):
                values.append(v)
        if values and all(v == 0.0 for v in values):
            return False, "All experiment metrics are zero — experiments likely failed"

    # Check 2: Zero variance across conditions (R13-1)
    # Look for ablation_warnings or condition comparison data
    ablation_warnings = data.get("ablation_warnings", [])
    conditions = data.get("conditions", data.get("condition_metrics", {}))
    if isinstance(conditions, dict) and len(conditions) >= 2:
        primary_values = []
        for cond_name, cond_data in conditions.items():
            if isinstance(cond_data, dict):
                pm = cond_data.get("primary_metric", cond_data.get("primary_metric_mean"))
                if isinstance(pm, (int, float)):
                    primary_values.append(pm)
        if len(primary_values) >= 2 and len(set(primary_values)) == 1:
            return False, (
                f"All {len(primary_values)} conditions have identical primary_metric "
                f"({primary_values[0]}) — condition implementations are likely broken"
            )

    # Check 3: Too many ablation warnings
    if isinstance(ablation_warnings, list) and len(ablation_warnings) >= 3:
        return False, (
            f"{len(ablation_warnings)} ablation warnings — most conditions "
            f"produce identical results"
        )

    # Check 4: Analysis quality score (if available)
    quality = data.get("analysis_quality", data.get("quality_score"))
    if isinstance(quality, (int, float)) and quality < 3.0:
        return False, f"Analysis quality score {quality}/10 — below minimum threshold"

    return True, "Quality checks passed"


def _read_pivot_count(run_dir: Path) -> int:
    """Read how many PIVOT/REFINE decisions have been made so far."""
    history_path = run_dir / "decision_history.json"
    if not history_path.exists():
        return 0
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
    except (json.JSONDecodeError, OSError):
        pass
    return 0


def _record_decision_history(
    run_dir: Path, decision: str, rollback_target: Stage, attempt: int
) -> None:
    """Append a decision event to the history log."""
    history_path = run_dir / "decision_history.json"
    history: list[dict[str, object]] = []
    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                history = data
        except (json.JSONDecodeError, OSError):
            pass
    history.append({
        "decision": decision,
        "rollback_target": rollback_target.name,
        "rollback_stage_num": int(rollback_target),
        "attempt": attempt,
        "timestamp": _utcnow_iso(),
    })
    history_path.write_text(
        json.dumps(history, indent=2), encoding="utf-8"
    )


logger = logging.getLogger(__name__)


def _read_quality_score(run_dir: Path) -> float | None:
    """Extract quality score from the most recent quality_report.json."""
    report_path = run_dir / "stage-23" / "quality_report.json"
    if not report_path.exists():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Try common keys: score_1_to_10, score, quality_score
            for key in ("score_1_to_10", "score", "quality_score", "overall_score"):
                if key in data:
                    return float(data[key])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def _write_iteration_context(
    run_dir: Path, iteration: int, reviews: str, quality_score: float | None
) -> None:
    """Write iteration feedback file so next round can read it."""
    ctx = {
        "iteration": iteration,
        "quality_score": quality_score,
        "reviews_excerpt": reviews[:3000] if reviews else "",
        "generated": _utcnow_iso(),
    }
    (run_dir / "iteration_context.json").write_text(
        json.dumps(ctx, indent=2), encoding="utf-8"
    )


def execute_iterative_pipeline(
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    auto_approve_gates: bool = False,
    kb_root: Path | None = None,
    max_iterations: int = 3,
    quality_threshold: float = 7.0,
    convergence_rounds: int = 2,
) -> dict[str, object]:
    """Run the full pipeline with iterative quality improvement.

    After the first full pass (stages 1-26), if the quality gate score is below
    *quality_threshold*, re-run stages 19-26 (paper writing + finalization) with
    review feedback injected.  Stop when:
      - Score >= quality_threshold, OR
      - Score hasn't improved for *convergence_rounds* consecutive iterations, OR
      - *max_iterations* reached.

    Returns a summary dict with iteration history.
    """
    iteration_scores: list[float | None] = []
    all_results: list[list[StageResult]] = []

    # --- First full pass ---
    logger.info("Iteration 1/%d: running full pipeline (stages 1-26)", max_iterations)
    results = execute_pipeline(
        run_dir=run_dir,
        run_id=f"{run_id}-iter1",
        config=config,
        adapters=adapters,
        auto_approve_gates=auto_approve_gates,
        kb_root=kb_root,
    )
    all_results.append(results)
    score = _read_quality_score(run_dir)
    iteration_scores.append(score)
    logger.info("Iteration 1 score: %s", score)

    # --- Iterative improvement ---
    for iteration in range(2, max_iterations + 1):
        # Check if we've met quality threshold
        if score is not None and score >= quality_threshold:
            logger.info(
                "Quality threshold %.1f met (score=%.1f). Stopping.",
                quality_threshold,
                score,
            )
            break

        # Check convergence (score hasn't improved)
        if len(iteration_scores) >= convergence_rounds:
            recent = iteration_scores[-convergence_rounds:]
            if all(s is not None for s in recent):
                recent_scores = [float(s) for s in recent if s is not None]
                if max(recent_scores) - min(recent_scores) < 0.5:
                    logger.info(
                        "Convergence detected: scores %s unchanged for %d rounds. Stopping.",
                        recent,
                        convergence_rounds,
                    )
                    break

        # Write iteration context with feedback from reviews
        reviews_text = ""
        reviews_path = run_dir / "stage-21" / "reviews.md"
        if reviews_path.exists():
            reviews_text = reviews_path.read_text(encoding="utf-8")
        _write_iteration_context(run_dir, iteration, reviews_text, score)

        # Re-run from PAPER_OUTLINE (stage 19) through CITATION_VERIFY (stage 26)
        logger.info(
            "Iteration %d/%d: re-running stages 19-26 with feedback",
            iteration,
            max_iterations,
        )
        results = execute_pipeline(
            run_dir=run_dir,
            run_id=f"{run_id}-iter{iteration}",
            config=config,
            adapters=adapters,
            from_stage=Stage.PAPER_OUTLINE,
            auto_approve_gates=auto_approve_gates,
            kb_root=kb_root,
        )
        all_results.append(results)
        score = _read_quality_score(run_dir)
        iteration_scores.append(score)
        logger.info("Iteration %d score: %s", iteration, score)

    # --- Build iterative summary ---
    converged = False
    if len(iteration_scores) >= convergence_rounds:
        recent_window = iteration_scores[-convergence_rounds:]
        if all(s is not None for s in recent_window):
            recent_scores = [float(s) for s in recent_window if s is not None]
            converged = max(recent_scores) - min(recent_scores) < 0.5

    summary: dict[str, object] = {
        "run_id": run_id,
        "total_iterations": len(iteration_scores),
        "iteration_scores": iteration_scores,
        "quality_threshold": quality_threshold,
        "converged": converged,
        "final_score": iteration_scores[-1] if iteration_scores else None,
        "met_threshold": score is not None and score >= quality_threshold,
        "stages_per_iteration": [len(r) for r in all_results],
        "generated": _utcnow_iso(),
    }
    (run_dir / "iteration_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )

    try:
        from researchclaw.observability.tracing import write_llm_trace_summary
        write_llm_trace_summary(run_dir)
    except Exception:  # noqa: BLE001
        logger.warning("LLM trace summary generation failed (non-blocking)")

    # --- Package deliverables into a single folder ---
    try:
        deliverables_dir = _package_deliverables(run_dir, run_id, config)
        if deliverables_dir is not None:
            print(f"[{run_id}] Deliverables packaged → {deliverables_dir}")
    except Exception:  # noqa: BLE001
        logger.warning("Deliverables packaging failed (non-blocking)")

    return summary


def _metaclaw_post_pipeline(
    config: RCConfig,
    results: list[StageResult],
    lessons: list[object],
    run_id: str,
    run_dir: Path,
) -> None:
    """MetaClaw bridge: post-pipeline hook.

    1. Convert high-severity lessons into MetaClaw skills.
    2. Record skill effectiveness feedback.
    3. Signal session end to MetaClaw proxy.
    """
    bridge = getattr(config, "metaclaw_bridge", None)
    if not bridge or not getattr(bridge, "enabled", False):
        return

    from researchclaw.llm.client import LLMClient

    # 1. Lesson-to-skill conversion
    l2s = getattr(bridge, "lesson_to_skill", None)
    if l2s and getattr(l2s, "enabled", False) and lessons:
        try:
            from researchclaw.metaclaw_bridge.lesson_to_skill import (
                convert_lessons_to_skills,
            )

            min_sev = getattr(l2s, "min_severity", "warning")
            llm = LLMClient.from_rc_config(config)
            new_skills = convert_lessons_to_skills(
                lessons,
                llm,
                getattr(bridge, "skills_dir", "~/.metaclaw/skills"),
                min_severity=min_sev,
                max_skills=getattr(l2s, "max_skills_per_run", 3),
            )
            if new_skills:
                logger.info(
                    "MetaClaw: generated %d new skills from lessons: %s",
                    len(new_skills),
                    new_skills,
                )
        except Exception:  # noqa: BLE001
            logger.warning("MetaClaw lesson-to-skill conversion failed", exc_info=True)

    # 2. Skill effectiveness feedback
    try:
        from researchclaw.metaclaw_bridge.skill_feedback import (
            SkillFeedbackStore,
            record_stage_skills,
        )
        from researchclaw.metaclaw_bridge.stage_skill_map import get_stage_config

        feedback_store = SkillFeedbackStore(run_dir / "evolution" / "skill_effectiveness.jsonl")
        for result in results:
            stage_num = int(getattr(result, "stage", 0))
            stage_name = {
                1: "topic_init", 2: "problem_decompose", 3: "search_strategy",
                4: "literature_collect", 5: "literature_screen", 6: "knowledge_extract",
                7: "synthesis", 8: "hypothesis_gen", 9: "experiment_design",
                10: "codebase_search", 11: "code_generation", 12: "sanity_check",
                13: "resource_planning", 14: "experiment_run", 15: "iterative_refine",
                16: "result_analysis", 17: "research_decision",
                18: "knowledge_summary", 19: "paper_outline", 20: "paper_draft",
                21: "peer_review", 22: "paper_revision", 23: "quality_gate",
                24: "knowledge_archive", 25: "export_publish",
                26: "citation_verify",
            }.get(stage_num, "")
            if not stage_name:
                continue

            stage_config = get_stage_config(stage_name)
            active_skills = stage_config.get("skills", [])
            status = str(getattr(result, "status", ""))
            success = "done" in status.lower()

            if active_skills:
                record_stage_skills(
                    feedback_store,
                    stage_name,
                    run_id,
                    success,
                    active_skills,
                )
    except Exception:  # noqa: BLE001
        logger.warning("MetaClaw skill feedback recording failed")

    # 3. Signal session end (fire-and-forget)
    try:
        from researchclaw.metaclaw_bridge.session import MetaClawSession
        import json as _json
        import urllib.request as _urllib_req

        session = MetaClawSession(run_id)
        end_headers = session.end()
        # Send a minimal request to signal session end
        proxy_url = getattr(bridge, "proxy_url", "http://localhost:30000")
        url = f"{proxy_url.rstrip('/')}/v1/chat/completions"
        body = _json.dumps({
            "model": "session-end",
            "messages": [{"role": "user", "content": "session complete"}],
            "max_tokens": 1,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(end_headers)
        req = _urllib_req.Request(url, data=body, headers=headers)
        try:
            _urllib_req.urlopen(req, timeout=5)
        except Exception:  # noqa: BLE001
            pass  # Best-effort signal
    except Exception:  # noqa: BLE001
        pass
