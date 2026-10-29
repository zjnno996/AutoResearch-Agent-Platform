"""Writing outline components."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import _find_discussion_dir, _read_prior_artifact
from researchclaw.pipeline.support.claims import _baseline_only_outline_violations
from researchclaw.pipeline.support.context import (
    _build_context_preamble,
    _chat_with_prompt,
    _get_evolution_overlay,
)
from researchclaw.pipeline.support.templates import _default_paper_outline
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _execute_paper_outline(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    decision = _read_prior_artifact(run_dir, "decision.md") or ""
    _disc_dir = _find_discussion_dir(run_dir, config)
    _has_discussion = _disc_dir is not None
    preamble = _build_context_preamble(
        config,
        run_dir,
        include_analysis=True,
        include_decision=True,
        include_experiment_data=True,
        include_discussion=_has_discussion,
    )

    # Build discussion ablation instruction if data exists
    discussion_ablation = ""
    if _has_discussion:
        discussion_ablation = (
            "\n\nIMPORTANT — MULTI-AGENT DISCUSSION ABLATION:\n"
            "The research idea for this paper was refined through a multi-agent "
            "discussion process (S8). Include an ablation section in the outline "
            "that compares pre-discussion individual syntheses vs post-discussion "
            "consensus. This should appear in the Discussion or Ablation Studies "
            "section of the paper. The discussion data is provided in the preamble.\n"
        )

    # WS-5.2: Read iteration feedback if available (multi-round iteration)
    feedback = ""
    iter_ctx_path = run_dir / "iteration_context.json"
    if iter_ctx_path.exists():
        try:
            ctx = json.loads(iter_ctx_path.read_text(encoding="utf-8"))
            iteration = ctx.get("iteration", 1)
            prev_score = ctx.get("quality_score")
            reviews_excerpt = ctx.get("reviews_excerpt", "")
            if iteration > 1 and reviews_excerpt:
                feedback = (
                    f"\n\n## Iteration {iteration} Feedback\n"
                    f"Previous quality score: {prev_score}/10\n"
                    f"Reviewer feedback to address:\n{reviews_excerpt[:2000]}\n"
                    f"\nYou MUST address these reviewer concerns in this revision.\n"
                )
        except (json.JSONDecodeError, KeyError):
            pass

    if llm is not None:
        _pm = prompts or PromptManager()
        # IMP-20: Pass academic style guide block for outline stage
        try:
            _asg = _pm.block("academic_style_guide")
        except (KeyError, Exception):
            _asg = ""
        _overlay = _get_evolution_overlay(run_dir, "paper_outline")
        sp = _pm.for_stage(
            "paper_outline",
            evolution_overlay=_overlay,
            preamble=preamble,
            topic_constraint=_pm.block("topic_constraint", topic=config.research.topic),
            feedback=feedback,
            analysis=analysis,
            decision=decision,
            academic_style_guide=_asg,
            discussion_ablation=discussion_ablation,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        outline = resp.content
        scope_violations = _baseline_only_outline_violations(run_dir, outline)
        if scope_violations:
            logger.warning(
                "Baseline-only outline violated executable scope (%s); retrying once",
                "; ".join(scope_violations),
            )
            retry_instruction = (
                "\n\nYour previous outline violated the executable experiment scope: "
                + "; ".join(scope_violations)
                + ". This project has proposed_methods: []. Rewrite the complete outline. "
                "Do not create, brand, name, or acronymize any method, protocol, framework, "
                "system, contribution, or audit. Use an unbranded descriptive title such as "
                "'A Reproducibility Audit of Linear SGD and Random Forest on UCI-HAR'. "
                "The only method names allowed are the two actually executed baselines."
            )
            resp = _chat_with_prompt(
                llm,
                sp.system,
                sp.user + retry_instruction,
                json_mode=sp.json_mode,
                max_tokens=sp.max_tokens,
            )
            outline = resp.content
        # Reasoning models may consume all tokens on CoT — retry with more
        if not outline.strip() and sp.max_tokens:
            logger.warning("Empty outline from LLM — retrying with 2x tokens")
            resp = _chat_with_prompt(
                llm,
                sp.system,
                sp.user,
                json_mode=sp.json_mode,
                max_tokens=sp.max_tokens * 2,
            )
            outline = resp.content
        if not outline.strip():
            logger.warning("LLM returned empty outline — using default")
            outline = _default_paper_outline(config.research.topic)
    else:
        outline = _default_paper_outline(config.research.topic)
    (stage_dir / "outline.md").write_text(outline, encoding="utf-8")
    return StageResult(
        stage=Stage.PAPER_OUTLINE,
        status=StageStatus.DONE,
        artifacts=("outline.md",),
        evidence_refs=("stage-19/outline.md",),
    )
