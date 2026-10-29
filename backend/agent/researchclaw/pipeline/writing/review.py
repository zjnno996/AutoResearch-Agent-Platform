"""Writing review components."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import (
    _find_prior_file,
    _read_prior_artifact,
    active_stage_glob,
)
from researchclaw.pipeline.support.citations import _build_deterministic_citation_context
from researchclaw.pipeline.support.claims import _claim_boundary_instruction
from researchclaw.pipeline.support.context import _chat_with_prompt, _get_evolution_overlay
from researchclaw.pipeline.support.experiment_evidence import (
    _collect_experiment_evidence,
    _collect_raw_experiment_metrics,
)
from researchclaw.pipeline.writing.fallbacks import _build_deterministic_paper_revision
from researchclaw.pipeline.writing.figures import _extract_figure_prompts, _render_figure_prompts
from researchclaw.pipeline.writing.latex import _generate_latex_package
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _execute_peer_review(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    draft = _read_prior_artifact(run_dir, "paper_draft.md") or ""
    experiment_evidence = _collect_experiment_evidence(run_dir)

    # Load draft quality warnings from Stage 17 (if available)
    _quality_suffix = ""
    _quality_json_path = _find_prior_file(run_dir, "draft_quality.json")
    if _quality_json_path and _quality_json_path.exists():
        try:
            _dq = json.loads(_quality_json_path.read_text(encoding="utf-8"))
            _dq_warnings = _dq.get("overall_warnings", [])
            if _dq_warnings:
                _quality_suffix = (
                    "\n\nAUTOMATED QUALITY ISSUES (flag these in your review):\n"
                    + "\n".join(f"- {w}" for w in _dq_warnings)
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            pass

    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "peer_review")
        sp = _pm.for_stage(
            "peer_review",
            evolution_overlay=_overlay,
            topic=config.research.topic,
            draft=draft,
            experiment_evidence=experiment_evidence,
        )
        _review_user = (
            sp.user
            + _quality_suffix
            + _claim_boundary_instruction(run_dir)
            + "\nExplicitly identify every claim that exceeds this boundary and give a scoped rewrite.\n"
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            _review_user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        reviews = resp.content
    else:
        reviews = """# Reviews

## Reviewer A
- Strengths: Clear problem statement.
- Weaknesses: Limited ablation details.
- Actionable revisions: Add uncertainty analysis and stronger baselines.

## Reviewer B
- Strengths: Reproducibility focus.
- Weaknesses: Discussion underdeveloped.
- Actionable revisions: Expand limitations and broader impact.
"""
    (stage_dir / "reviews.md").write_text(reviews, encoding="utf-8")
    return StageResult(
        stage=Stage.PEER_REVIEW,
        status=StageStatus.DONE,
        artifacts=("reviews.md",),
        evidence_refs=("stage-21/reviews.md",),
    )


def _execute_paper_revision(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    draft = _read_prior_artifact(run_dir, "paper_draft.md") or ""
    reviews = _read_prior_artifact(run_dir, "reviews.md") or ""
    draft_word_count = len(draft.split())

    # R4-2: Collect real metrics for anti-fabrication guard in revision
    # BUG-47: _collect_raw_experiment_metrics returns tuple[str, bool], must unpack
    _raw_metrics_tuple = _collect_raw_experiment_metrics(run_dir)
    raw_metrics_revision = _raw_metrics_tuple[0] if isinstance(_raw_metrics_tuple, tuple) else (_raw_metrics_tuple or "")
    data_integrity_revision = ""
    if raw_metrics_revision:
        data_integrity_revision = (
            raw_metrics_revision
            + "\nDATA INTEGRITY: Do NOT add new numbers that are not in the "
            "experiment data above. If a reviewer asks for additional results "
            "you do not have, state 'Due to computational constraints, "
            "this analysis was not conducted' instead of fabricating data.\n"
        )

    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    _deterministic_revision = _paper_len_cfg in {"deterministic", "fallback"}
    if _deterministic_revision:
        revised = _build_deterministic_paper_revision(
            topic=config.research.topic,
            draft=draft,
            reviews=reviews,
            raw_metrics_block=raw_metrics_revision,
            citation_context=_build_deterministic_citation_context(run_dir),
            exp_summary_text=_read_prior_artifact(run_dir, "experiment_summary.json") or "",
        )
    elif llm is not None:
        _pm = prompts or PromptManager()
        try:
            _ws_revision = _pm.block("writing_structure")
        except (KeyError, Exception):  # noqa: BLE001
            _ws_revision = ""
        # IMP-20/25/31/24: Load style blocks for revision prompt
        _rev_blocks: dict[str, str] = {}
        for _bname in ("academic_style_guide", "narrative_writing_rules",
                        "anti_hedging_rules", "anti_repetition_rules"):
            try:
                _rev_blocks[_bname] = _pm.block(_bname)
            except (KeyError, Exception):  # noqa: BLE001
                _rev_blocks[_bname] = ""
        # Load draft quality directives from Stage 17
        _quality_prefix = ""
        _quality_json_path = _find_prior_file(run_dir, "draft_quality.json")
        if _quality_json_path and _quality_json_path.exists():
            try:
                _dq = json.loads(_quality_json_path.read_text(encoding="utf-8"))
                _dq_directives = _dq.get("revision_directives", [])
                if _dq_directives:
                    _quality_prefix = (
                        "MANDATORY QUALITY FIXES (address ALL of these):\n"
                        + "\n".join(f"- {d}" for d in _dq_directives)
                        + "\n\n"
                    )
            except Exception:  # noqa: BLE001
                pass

        _overlay = _get_evolution_overlay(run_dir, "paper_revision")
        sp = _pm.for_stage(
            "paper_revision",
            evolution_overlay=_overlay,
            topic_constraint=_pm.block("topic_constraint", topic=config.research.topic),
            writing_structure=_ws_revision,
            draft=draft,
            reviews=(
                _quality_prefix
                + reviews
                + data_integrity_revision
                + _claim_boundary_instruction(run_dir)
            ),
            **_rev_blocks,
        )
        # R10-Fix2: Ensure max_tokens is sufficient for full paper revision
        revision_max_tokens = sp.max_tokens
        if revision_max_tokens and draft_word_count > 0:
            # ~1.5 tokens per word, 20% headroom
            min_tokens_needed = int(draft_word_count * 1.5 * 1.2)
            if revision_max_tokens < min_tokens_needed:
                revision_max_tokens = min_tokens_needed
                logger.info(
                    "Stage 19: Increased max_tokens from %d to %d to fit full paper revision",
                    sp.max_tokens,
                    revision_max_tokens,
                )

        # R10-Fix4: Retry on timeout for paper revision (critical stage)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=revision_max_tokens,
            retries=2,
        )
        revised = resp.content
        revised_word_count = len(revised.split())
        # Length guard: if revision is shorter than 80% of draft, retry once
        if draft_word_count > 500 and revised_word_count < int(draft_word_count * 0.8):
            logger.warning(
                "Paper revision (%d words) is shorter than draft (%d words). "
                "Retrying with stronger length enforcement.",
                revised_word_count,
                draft_word_count,
            )
            retry_user = (
                f"CRITICAL LENGTH REQUIREMENT: The draft is {draft_word_count} words. "
                f"Your revision MUST be at least {draft_word_count} words — ideally longer. "
                f"Do NOT summarize or condense ANY section. Copy each section verbatim "
                f"and ONLY make targeted improvements to address reviewer comments. "
                f"If a section has no reviewer comments, include it UNCHANGED.\n\n"
                + sp.user
            )
            resp2 = _chat_with_prompt(
                llm, sp.system, retry_user,
                json_mode=sp.json_mode, max_tokens=revision_max_tokens,
            )
            revised2 = resp2.content
            revised2_word_count = len(revised2.split())
            if revised2_word_count >= int(draft_word_count * 0.8):
                revised = revised2
            elif revised2_word_count > revised_word_count:
                # Retry improved but still not enough — use the longer version
                revised = revised2
                logger.warning(
                    "Retry improved (%d → %d words) but still shorter than draft (%d).",
                    revised_word_count,
                    revised2_word_count,
                    draft_word_count,
                )
            else:
                # Both attempts produced short output — preserve full original draft
                logger.warning(
                    "Retry also produced short output (%d words). "
                    "Falling back to FULL ORIGINAL DRAFT to prevent content loss.",
                    revised2_word_count,
                )
                # Extract useful revision points as appendix
                revision_words = revised.split()
                revision_summary = (
                    " ".join(revision_words[:500]) + "\n\n*(Revision summary truncated)*"
                    if len(revision_words) > 500
                    else revised
                )
                if revision_summary.strip():
                    # Save revision notes to internal file, not paper body
                    (stage_dir / "revision_notes_internal.md").write_text(
                        revision_summary, encoding="utf-8"
                    )
                revised = draft
    else:
        revised = draft
    (stage_dir / "paper_revised.md").write_text(revised, encoding="utf-8")

    _revision_artifacts = ["paper_revised.md"]
    _topic = config.research.topic if hasattr(config, "research") else ""

    # Reuse figures from S20 (PAPER_DRAFT) instead of regenerating
    _s20_figures_dir = None
    for _sd in sorted(active_stage_glob(run_dir, "stage-20*"), reverse=True):
        _candidate = _sd / "figures"
        if _candidate.is_dir() and any(_candidate.iterdir()):
            _s20_figures_dir = _candidate
            break

    _fig_prompts = _extract_figure_prompts(revised, topic=_topic)
    if _fig_prompts:
        _s22_figures_dir = stage_dir / "figures"
        _s22_figures_dir.mkdir(parents=True, exist_ok=True)

        # Copy existing figures from S20 before rendering new ones
        _reused_ids: set[str] = set()
        if _s20_figures_dir:
            import shutil as _shutil_fig
            for _existing_fig in _s20_figures_dir.iterdir():
                if _existing_fig.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".eps"}:
                    _dest = _s22_figures_dir / _existing_fig.name
                    if not _dest.exists():
                        _shutil_fig.copy2(_existing_fig, _dest)
            # Mark prompts as already rendered if their output file exists
            for _fp in _fig_prompts:
                _fid = _fp.get("figure_id", "")
                # Check common naming patterns
                for _ext in (".png", ".jpg", ".jpeg"):
                    _candidate_name = _s22_figures_dir / f"{_fid}{_ext}"
                    if _candidate_name.exists():
                        _fp["output_path"] = str(_candidate_name)
                        _fp["success"] = True
                        _reused_ids.add(_fid)
                        break
            if _reused_ids:
                logger.info(
                    "Stage 22: Reused %d/%d figures from S20",
                    len(_reused_ids), len(_fig_prompts),
                )

        # Only render prompts that don't already have figures
        _new_prompts = [fp for fp in _fig_prompts if fp.get("figure_id") not in _reused_ids]
        if _new_prompts:
            _new_prompts = _render_figure_prompts(
                _new_prompts, stage_dir, config, llm, topic=_topic,
            )
            # Merge back into _fig_prompts
            _new_map = {fp["figure_id"]: fp for fp in _new_prompts}
            for _fp in _fig_prompts:
                if _fp["figure_id"] in _new_map:
                    _fp.update(_new_map[_fp["figure_id"]])
            logger.info(
                "Stage 22: Rendered %d new figures (reused %d from S20)",
                len(_new_prompts), len(_reused_ids),
            )

        _fp_path = stage_dir / "figure_prompts.json"
        _fp_path.write_text(
            json.dumps(_fig_prompts, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _revision_artifacts.append("figure_prompts.json")
        for _fp in _fig_prompts:
            if _fp.get("success") and _fp.get("output_path"):
                _rel = Path(_fp["output_path"]).name
                _revision_artifacts.append(f"figures/{_rel}")
        logger.info(
            "Stage 22: Total %d figure prompts → %s",
            len(_fig_prompts), _fp_path,
        )

    # --- Generate LaTeX package for Overleaf ---
    try:
        _latex_llm = None if _deterministic_revision else llm
        _latex_artifacts = _generate_latex_package(stage_dir, run_dir, config, revised, llm=_latex_llm)
        _revision_artifacts.extend(_latex_artifacts)
        logger.info("Stage 22: LaTeX package generated → %s", stage_dir / "latex_package.zip")
    except Exception:
        logger.warning("Stage 22: LaTeX package generation failed", exc_info=True)

    return StageResult(
        stage=Stage.PAPER_REVISION,
        status=StageStatus.DONE,
        artifacts=tuple(_revision_artifacts),
        evidence_refs=tuple(f"stage-22/{a}" for a in _revision_artifacts),
    )
