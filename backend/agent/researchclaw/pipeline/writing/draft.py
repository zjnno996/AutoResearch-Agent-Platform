"""Writing draft components."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import (
    _find_discussion_dir,
    _read_literature_bib,
    _read_prior_artifact,
    _utcnow_iso,
    active_stage_glob,
)
from researchclaw.pipeline.support.citations import _build_deterministic_citation_context
from researchclaw.pipeline.support.context import _build_context_preamble, _chat_with_prompt
from researchclaw.pipeline.support.domain import _detect_domain
from researchclaw.pipeline.support.experiment_evidence import _collect_raw_experiment_metrics
from researchclaw.pipeline.support.parsing import _safe_json_loads
from researchclaw.pipeline.writing.fallbacks import _build_deterministic_paper_draft
from researchclaw.pipeline.writing.figures import (
    _extract_figure_prompts,
    _read_figure_manifest,
    _render_figure_prompts,
)
from researchclaw.pipeline.writing.quality import (
    _check_ablation_effectiveness,
    _detect_result_contradictions,
    _validate_draft_quality,
)
from researchclaw.pipeline.writing.sections import _write_paper_sections
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _paper_draft_timeout_sec(config: RCConfig) -> int:
    """Allow an hour-level budget for the multi-call full-paper generation pass."""
    configured = max(1, int(getattr(config.llm, "timeout_sec", 120) or 120))
    default_timeout = max(3600, configured * 3)
    return max(60, int(os.environ.get("RESEARCHCLAW_PAPER_DRAFT_TIMEOUT_SEC", str(default_timeout)) or default_timeout))


def _execute_paper_draft(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    outline = _read_prior_artifact(run_dir, "outline.md") or ""

    # ── SHORT paper fast-path: skip heavy validation, single LLM call ──
    _paper_len = getattr(config.experiment, "paper_length", "") or "full"
    if _paper_len == "short" and llm is not None:
        preamble = _build_context_preamble(
            config, run_dir, include_analysis=True, include_experiment_data=True,
        )
        analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
        exp_summary_text = _read_prior_artifact(run_dir, "experiment_summary.json") or ""
        _extra = ""
        if exp_summary_text:
            _extra = f"\n\nExperiment Summary JSON:\n```json\n{exp_summary_text[:3000]}\n```\n"
        _short_user = (
            f"{preamble}\n\n{_extra}\n\nOutline:\n{outline}\n\n"
            "Write a SHORT WORKSHOP PAPER (2-3 pages, ~1500-2000 words total) "
            "in markdown. Include these sections:\n"
            "1. **Title** (catchy, <=14 words)\n"
            "2. **Abstract** (100-150 words)\n"
            "3. **Introduction** (300-400 words): motivation, gap, approach, contributions\n"
            "4. **Method** (300-400 words): concise method description\n"
            "5. **Experiments** (300-400 words): setup, results with real numbers from data above\n"
            "6. **Conclusion** (100-150 words)\n\n"
            "Use ## headers. Be concise and direct. "
            "Do NOT include a References section. "
            "Start DIRECTLY with '## Title'."
        )
        _short_sys = (
            "You are an academic writer producing a concise short/workshop paper. "
            "Use formal academic tone. Report only real experimental numbers."
        )
        resp = _chat_with_prompt(llm, _short_sys, _short_user, max_tokens=4000)
        draft = resp.content
        logger.info("Stage 20: Short paper mode — single LLM call (%d words)", len(draft.split()))
        (stage_dir / "paper_draft.md").write_text(draft, encoding="utf-8")
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.DONE,
            artifacts=("paper_draft.md",),
            evidence_refs=("stage-20/paper_draft.md",),
        )
    # ── END short fast-path ──

    _disc_dir = _find_discussion_dir(run_dir, config)
    _has_discussion = _disc_dir is not None
    preamble = _build_context_preamble(
        config,
        run_dir,
        include_goal=True,
        include_hypotheses=True,
        include_analysis=True,
        include_experiment_data=True,  # WS-5.1: inject real experiment data
        include_discussion=_has_discussion,
    )

    # Build discussion ablation instruction for paper draft
    discussion_ablation = ""
    if _has_discussion:
        discussion_ablation = (
            "\n\n## MULTI-AGENT DISCUSSION ABLATION (MANDATORY SECTION)\n"
            "This paper's research idea was refined through a multi-agent collaborative "
            "discussion process. You MUST include an **ablation study** comparing:\n"
            "- **Without discussion**: Individual agent syntheses (each agent independently "
            "surveyed literature and generated a synthesis)\n"
            "- **With discussion**: Post-discussion consensus synthesis (agents debated, "
            "critiqued each other's views, and reached consensus)\n\n"
            "Analyze the following dimensions in the ablation:\n"
            "1. **Coverage**: How many unique research gaps/opportunities were identified "
            "before vs after discussion\n"
            "2. **Contradiction resolution**: What conflicting viewpoints existed and how "
            "the discussion resolved them\n"
            "3. **Novel insights**: Ideas that emerged ONLY from the discussion interaction "
            "(not present in any individual synthesis)\n"
            "4. **Hypothesis quality**: Compare the specificity and testability of hypotheses "
            "derived from individual vs consensus syntheses\n\n"
            "Place this ablation in the Discussion section or as a dedicated subsection "
            "titled 'Ablation: Multi-Agent Discussion Impact'. Use the pre-discussion "
            "and post-discussion data provided in the research context.\n"
        )

    # Current analyzed evidence is authoritative. Historical attempts remain
    # auditable but cannot silently override the current claim boundary.
    exp_summary_text = _read_prior_artifact(run_dir, "experiment_summary.json")
    exp_metrics_instruction = ""
    has_real_metrics = False
    if exp_summary_text:
        exp_summary = _safe_json_loads(exp_summary_text, {})
        if isinstance(exp_summary, dict) and exp_summary.get("metrics_summary"):
            has_real_metrics = True
            exp_metrics_instruction = (
                "\n\nIMPORTANT: Use the ACTUAL experiment results provided in the context. "
                "All numbers in the Results and Experiments sections MUST reference real data. "
                "Do NOT write 'no quantitative results yet' or use placeholder numbers. "
                "Cite specific metrics with their actual values.\n"
            )

    # Collect raw experiment stdout metrics as hard constraint for the paper
    raw_metrics_block, _has_parsed_metrics = _collect_raw_experiment_metrics(run_dir)
    if raw_metrics_block:
        # BUG-23: Raw stdout alone is not sufficient — require either
        # metrics_summary data, parsed metrics from run JSONs,
        # OR at least 3 condition= patterns in raw block
        _has_condition_pattern = len(re.findall(
            r"condition[=:]", raw_metrics_block, re.IGNORECASE
        )) >= 3
        if has_real_metrics or _has_parsed_metrics or _has_condition_pattern:
            has_real_metrics = True
        exp_metrics_instruction += raw_metrics_block

    # R18-1 + R19-6: Inject paired statistical comparisons AND condition summaries
    if exp_summary_text:
        exp_summary_parsed = _safe_json_loads(exp_summary_text, {})
        if isinstance(exp_summary_parsed, dict):
            # R19-6: Inject experiment scale header so LLM knows the data richness
            _total_conds = exp_summary_parsed.get("total_conditions")
            _total_mkeys = exp_summary_parsed.get("total_metric_keys")
            if _total_conds or _total_mkeys:
                scale_block = "\n\n## EXPERIMENT SCALE\n"
                if _total_conds:
                    scale_block += f"- Total conditions tested: {_total_conds}\n"
                if _total_mkeys:
                    scale_block += f"- Total metric keys collected: {_total_mkeys}\n"
                scale_block += (
                    "- This is a MULTI-SEED experiment. Report mean +/- std across seeds.\n"
                    "- Do NOT describe results as 'single run' or 'preliminary'.\n"
                )
                exp_metrics_instruction += scale_block

            # R19-6 + R33: Inject condition summaries with CIs
            cond_summaries = exp_summary_parsed.get("condition_summaries", {})
            if isinstance(cond_summaries, dict) and cond_summaries:
                cond_block = "\n\n## PER-CONDITION SUMMARY (use in Results tables)\n"
                for cname, cdata in sorted(cond_summaries.items()):
                    cond_block += f"\n### {cname}\n"
                    if not isinstance(cdata, dict):
                        continue
                    sr = cdata.get("success_rate")
                    if sr is not None:
                        cond_block += f"- Success rate: {sr:.1%}\n"
                    ns = cdata.get("n_seeds") or cdata.get("n_seed_metrics")
                    if ns:
                        cond_block += f"- Seeds: {ns}\n"
                    ci_lo = cdata.get("ci95_low")
                    ci_hi = cdata.get("ci95_high")
                    if ci_lo is not None and ci_hi is not None:
                        try:
                            cond_block += f"- Bootstrap 95% CI: [{float(ci_lo):.4f}, {float(ci_hi):.4f}]\n"
                        except (ValueError, TypeError):
                            cond_block += f"- Bootstrap 95% CI: [{ci_lo}, {ci_hi}]\n"
                    cm = cdata.get("metrics", {})
                    if cm:
                        for mk, mv in sorted(cm.items()):
                            if isinstance(mv, (int, float)):
                                cond_block += f"- {mk}: {mv:.4f}\n"
                            else:
                                cond_block += f"- {mk}: {mv}\n"
                exp_metrics_instruction += cond_block

            # R18-1: Inject paired statistical comparisons
            paired = exp_summary_parsed.get("paired_comparisons", [])
            if paired:
                paired_block = "\n\n## PAIRED STATISTICAL COMPARISONS (use these in Results)\n"
                paired_block += f"Total: {len(paired)} paired tests computed.\n"
                for pc in paired:
                    if not isinstance(pc, dict):
                        continue
                    method = pc.get("method", "?")
                    baseline = pc.get("baseline", "?")
                    regime = pc.get("regime", "all")
                    md = pc.get("mean_diff", "?")
                    sd = pc.get("std_diff", "?")
                    ts = pc.get("t_stat", "?")
                    pv = pc.get("p_value", "?")
                    ci_lo = pc.get("ci95_low")
                    ci_hi = pc.get("ci95_high")
                    ci_str = ""
                    if ci_lo is not None and ci_hi is not None:
                        try:
                            ci_str = f", 95% CI [{float(ci_lo):.3f}, {float(ci_hi):.3f}]"
                        except (ValueError, TypeError):
                            ci_str = f", 95% CI [{ci_lo}, {ci_hi}]"
                    paired_block += (
                        f"- {method} vs {baseline} (regime={regime}): "
                        f"mean_diff={md}, std_diff={sd}, "
                        f"t={ts}, p={pv}{ci_str}\n"
                    )
                exp_metrics_instruction += paired_block

            # R24: Method naming map — translate generic condition labels
            _cond_names = list(cond_summaries.keys()) if isinstance(cond_summaries, dict) and cond_summaries else []
            if _cond_names:
                naming_block = (
                    "\n\n## METHOD NAMING (CRITICAL — do NOT use generic labels in the paper)\n"
                    "The condition labels below come from the experiment code. In the paper, "
                    "you MUST use DESCRIPTIVE algorithm names, not generic labels.\n"
                    "- If a condition name is already descriptive (e.g., 'random_search', "
                    "'bayesian_optimization', 'ppo_policy'), use it directly as a proper name.\n"
                    "- If a condition name is generic (e.g., 'baseline_1', 'method_variant_1'), "
                    "you MUST infer the algorithm from the experiment code/context and use the "
                    "real algorithm name (e.g., 'Random Search', 'Bayesian Optimization', "
                    "'PPO', 'Curiosity-Driven RL').\n"
                    "- NEVER write `baseline_1` or `method_variant_1` in the paper text.\n"
                    f"- Conditions to name: {_cond_names}\n"
                )
                exp_metrics_instruction += naming_block

            # IMP-8: Inject broken ablation warnings
            abl_warnings = exp_summary_parsed.get("ablation_warnings", [])
            if abl_warnings:
                broken_block = (
                    "\n\n## BROKEN ABLATIONS (DO NOT discuss as valid results)\n"
                    "The following ablation conditions produced IDENTICAL outputs, "
                    "indicating implementation bugs. Do NOT present their differences "
                    "as findings. Mention them ONLY in a 'Limitations' sub-section "
                    "as known implementation issues:\n"
                )
                for _aw in abl_warnings:
                    broken_block += f"- {_aw}\n"
                broken_block += (
                    "\nIf you reference these conditions, state explicitly: "
                    "'Due to an implementation defect, conditions X and Y produced "
                    "identical outputs; their comparison is therefore uninformative.'\n"
                )
                exp_metrics_instruction += broken_block

            # R25: Statistical table format requirement
            if paired:
                stat_table_block = (
                    "\n\n## STATISTICAL TABLE REQUIREMENT (MANDATORY in Results section)\n"
                    "The Results section MUST include a statistical comparison table with columns:\n"
                    "| Comparison | Mean Diff | Std Diff | t-statistic | p-value | Significance |\n"
                    "Use the PAIRED STATISTICAL COMPARISONS data above to fill this table.\n"
                    "Mark significance: *** (p<0.001), ** (p<0.01), * (p<0.05), n.s.\n"
                    "This is non-negotiable — a top-venue paper MUST have statistical tests.\n"
                )
                exp_metrics_instruction += stat_table_block

            # R26: Metric definition requirement
            exp_metrics_instruction += (
                "\n\n## METRIC DEFINITIONS (MANDATORY in Experiments section)\n"
                "The Experiments section MUST define each metric:\n"
                "- **Primary metric**: what it measures, how it is computed, range, direction "
                "(higher/lower is better), and units if applicable.\n"
                "- **Secondary metric**: same details.\n"
                "- For time-to-event metrics: explain the horizon, what constitutes success, "
                "and how failures are handled (e.g., set to max horizon).\n"
                "- These definitions MUST appear BEFORE any results tables.\n"
            )

            # R27: Multi-seed framing enforcement
            _any_seeds = any(
                (cond_summaries.get(c) or {}).get("n_seed_metrics", 0) > 1
                for c in _cond_names
            ) if _cond_names else False
            if _any_seeds:
                exp_metrics_instruction += (
                    "\n\n## MULTI-SEED EXPERIMENT FRAMING (CRITICAL)\n"
                    "This experiment uses MULTIPLE independent random seeds per condition.\n"
                    "- Report mean +/- std (or SE) for all metrics.\n"
                    "- NEVER describe this as 'a single run' or '1 benchmark-artifact run'.\n"
                    "- Frame as: 'We evaluate each method across N seeds per regime.'\n"
                    "- The seed-level data IS the evidence base — it is NOT a single observation.\n"
                    "- Include per-regime breakdowns (easy vs hard) as separate rows in tables.\n"
                )

    # BUG-003: Inject actual evaluated datasets as a hard constraint
    if exp_summary_text:
        _ds_parsed = _safe_json_loads(exp_summary_text, {})
        if isinstance(_ds_parsed, dict):
            _datasets: set[str] = set()
            # Extract from condition names (often contain dataset info)
            for _cname in (_ds_parsed.get("condition_summaries") or {}).keys():
                _datasets.add(str(_cname))
            # Extract from explicit "datasets" field if present
            for _ds in (_ds_parsed.get("datasets") or []):
                if isinstance(_ds, str):
                    _datasets.add(_ds)
            # Extract from "benchmark" or "dataset" fields
            for _key in ("benchmark", "dataset", "dataset_name"):
                _dv = _ds_parsed.get(_key)
                if isinstance(_dv, str) and _dv:
                    _datasets.add(_dv)
            if _datasets:
                exp_metrics_instruction += (
                    "\n\n## ACTUAL EVALUATED DATASETS (HARD CONSTRAINT)\n"
                    "The following datasets/conditions were ACTUALLY tested in experiments:\n"
                    + "".join(f"- {d}\n" for d in sorted(_datasets))
                    + "\nCRITICAL: Do NOT claim evaluation on any dataset not listed above.\n"
                    "Do NOT fabricate results for datasets you did not run experiments on.\n"
                    "If you reference other datasets, clearly state they are 'not evaluated "
                    "in this work' or are 'left for future work'.\n"
                )

    # P7: Ablation effectiveness check
    if exp_summary_text:
        _exp_parsed_p7 = _safe_json_loads(exp_summary_text, {})
        if isinstance(_exp_parsed_p7, dict):
            _abl_warnings = _check_ablation_effectiveness(_exp_parsed_p7)
            if _abl_warnings:
                _abl_block = (
                    "\n\n## ABLATION EFFECTIVENESS WARNINGS\n"
                    "The following ablations showed minimal effect (within 5%% of baseline). "
                    "Discuss this honestly — it may indicate the ablated component is not "
                    "important, or the ablation was not properly implemented:\n"
                )
                for _aw in _abl_warnings:
                    _abl_block += f"- {_aw}\n"
                exp_metrics_instruction += _abl_block
                logger.warning("P7: Ablation effectiveness warnings: %s", _abl_warnings)

    # P10: Contradiction detection
    if exp_summary_text:
        _exp_parsed_p10 = _safe_json_loads(exp_summary_text, {})
        if isinstance(_exp_parsed_p10, dict):
            _contradictions = _detect_result_contradictions(_exp_parsed_p10)
            if _contradictions:
                _contra_block = (
                    "\n\n## RESULT INTERPRETATION ADVISORIES (CRITICAL — read before writing)\n"
                )
                for _ca in _contradictions:
                    _contra_block += f"- {_ca}\n"
                exp_metrics_instruction += _contra_block
                logger.warning("P10: Contradiction advisories: %s", _contradictions)

    # R10: HARD BLOCK — refuse to write paper when all data is simulated
    all_simulated = True
    for stage_subdir in sorted(active_stage_glob(run_dir, "stage-*/runs")):
        for run_file in sorted(stage_subdir.glob("*.json")):
            if run_file.name == "results.json":
                continue
            try:
                _payload = json.loads(run_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(_payload, dict) and _payload.get("status") != "simulated":
                all_simulated = False
                break
        if not all_simulated:
            break

    if all_simulated:
        logger.error(
            "BLOCKED: All experiment data is simulated (mode='simulated'). "
            "Cannot write a paper based on formulaic fake data. "
            "Switch to experiment.mode='sandbox' and re-run."
        )
        (stage_dir / "paper_draft.md").write_text(
            "# Paper Draft Blocked\n\n"
            "**Reason**: All experiment results are from simulated mode "
            "(formulaic data: `0.3 + idx * 0.03`). "
            "These are not real experimental results.\n\n"
            "**Action Required**: Set `experiment.mode: 'sandbox'` in "
            "config.arc.yaml and re-run the pipeline.",
            encoding="utf-8",
        )
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.FAILED,
            artifacts=("paper_draft.md",),
            evidence_refs=(),
        )

    # R4-2: HARD BLOCK — refuse to write paper with no real data (ML/empirical domains)
    # For non-empirical domains (math proofs, theoretical economics), allow proceeding
    _domain_id, _domain_name, _domain_venues = _detect_domain(
        config.research.topic, config.research.domains
    )
    _empirical_domains = {"ml", "engineering", "biology", "chemistry"}
    if not has_real_metrics:
        if _domain_id in _empirical_domains:
            logger.error(
                "BLOCKED: Cannot write paper — experiment produced NO metrics. "
                "The pipeline will not fabricate results."
            )
            (stage_dir / "paper_draft.md").write_text(
                "# Paper Draft Blocked\n\n"
                "**Reason**: Experiment stage produced no metrics (status: failed/timeout). "
                "Cannot write a paper without real experimental data.\n\n"
                "**Action Required**: Fix experiment execution or increase time_budget_sec.",
                encoding="utf-8",
            )
            return StageResult(
                stage=Stage.PAPER_DRAFT,
                status=StageStatus.FAILED,
                artifacts=("paper_draft.md",),
                evidence_refs=(),
            )
        else:
            logger.warning(
                "No experiment metrics found, but domain '%s' may be non-empirical "
                "(theoretical/mathematical). Proceeding with paper draft.",
                _domain_name,
            )

    # R11-5: Experiment quality minimum threshold before paper writing
    # Parse analysis.md for quality rating and condition completeness
    analysis_text = _read_prior_artifact(run_dir, "analysis.md") or ""
    _quality_warnings: list[str] = []

    # Check 1: Was the analysis quality rating very low?
    import re as _re_q
    _rating_match = _re_q.search(
        r"(?:quality\s+rating|result\s+quality)[:\s]*\**(\d+)\s*/\s*10",
        analysis_text,
        _re_q.IGNORECASE,
    )
    if _rating_match:
        _analysis_rating = int(_rating_match.group(1))
        if _analysis_rating <= 3:
            _quality_warnings.append(
                f"Analysis rated experiment quality {_analysis_rating}/10"
            )
        # BUG-23: If quality rating is ≤ 2, force has_real_metrics = False
        # to prevent fabricated results even if stdout had stray numbers.
        # R5-BUG-05: Skip override when _has_parsed_metrics is True — the
        # analysis.md may be stale (from pre-refinement Stage 14) while
        # Stage 13 refinement produced real parsed metrics.
        if _analysis_rating <= 2 and has_real_metrics and not _has_parsed_metrics:
            logger.warning(
                "BUG-23 guard: Analysis quality %d/10 ≤ 2 — "
                "overriding has_real_metrics to False (experiment likely failed)",
                _analysis_rating,
            )
            has_real_metrics = False

    # Check 2: Are baselines missing?
    _analysis_lower = analysis_text.lower()
    if "no" in _analysis_lower and "baseline" in _analysis_lower:
        if any(phrase in _analysis_lower for phrase in [
            "no baseline", "no bo", "no random", "baselines are missing",
            "missing baselines", "baseline coverage is missing",
        ]):
            _quality_warnings.append("Baselines appear to be missing from results")

    # Check 3: Is the metric undefined?
    if any(phrase in _analysis_lower for phrase in [
        "metric is undefined", "primary_metric is undefined",
        "undefined metric", "metric undefined",
    ]):
        _quality_warnings.append("Primary metric is undefined (direction/units/formula unknown)")

    # Check 4: Very few conditions completed
    _condition_count = len(_re_q.findall(
        r"condition[=:\s]+\w+.*?(?:mean|primary_metric)",
        raw_metrics_block or "",
        _re_q.IGNORECASE,
    ))

    if _quality_warnings:
        _warning_block = "\n".join(f"  - {w}" for w in _quality_warnings)
        logger.warning(
            "Stage 17: Experiment quality concerns detected before paper writing:\n%s",
            _warning_block,
        )
        # Inject quality warnings into the paper writing prompt so the LLM
        # writes an appropriately hedged paper
        exp_metrics_instruction += (
            "\n\n## EXPERIMENT QUALITY WARNINGS (address these honestly in the paper)\n"
            + "\n".join(f"- {w}" for w in _quality_warnings)
            + "\n\nBecause of these issues, the paper MUST:\n"
            "- Use hedged language ('preliminary', 'pilot', 'initial exploration')\n"
            "- NOT claim definitive comparisons between methods\n"
            "- Dedicate a substantial Limitations section to these gaps\n"
            "- Frame the contribution as methodology/framework, not empirical findings\n"
        )
        # Save warnings for tracking
        (stage_dir / "quality_warnings.json").write_text(
            json.dumps(_quality_warnings, indent=2), encoding="utf-8"
        )

    # R4-2: Anti-fabrication data integrity instruction
    exp_metrics_instruction += (
        "\n\n## CRITICAL: Data Integrity Rules\n"
        "- You may ONLY report numbers that appear in the experiment data above\n"
        "- If the experiment data is incomplete (fewer conditions than planned), report\n"
        "  ONLY the conditions that were actually run\n"
        "- Do NOT extrapolate, interpolate, or 'fill in' missing cells in tables\n"
        "- Do NOT invent confidence intervals, p-values, or statistical tests unless\n"
        "  the actual data supports them\n"
        "- If only N conditions completed, simply report results for those N conditions\n"
        "  without repeating apologies or disclaimers about missing conditions\n"
        "- Any table cell without real data must show '—' (not a plausible number)\n"
        "- FORBIDDEN: generating numbers that 'look right' based on your training data\n"
    )

    # IMP-6 + FA: Inject chart references into paper draft prompt
    # Prefer FigureAgent's figure_plan.json (rich descriptions) over raw file scan
    # BUG-FIX: figure_plan.json may be a list (from FigureAgent planner) or a dict
    # (from executor overwrite).  The orchestrator writes a list at planning time;
    # the executor overwrites with a dict only when figure_count > 0.  If the
    # FigureAgent renders 0 charts the list persists, and calling .get() on it
    # raises AttributeError.
    _fa_descriptions = ""
    # Charts and figure plans are generated in stage-16 (RESULT_ANALYSIS),
    # also check stage-14 as a legacy fallback.
    for _fig_stage_pattern in ("stage-16*", "stage-14*"):
        for _fig_stage_dir in sorted(active_stage_glob(run_dir, _fig_stage_pattern)):
            for _fp_name in ("figure_plan_final.json", "figure_plan.json"):
                _fp_path = _fig_stage_dir / _fp_name
                if not _fp_path.exists():
                    continue
                try:
                    _fp_data = json.loads(_fp_path.read_text(encoding="utf-8"))
                    if isinstance(_fp_data, dict):
                        _fa_descriptions = _fp_data.get("figure_descriptions", "")
                    elif isinstance(_fp_data, list) and _fp_data:
                        _desc_parts = ["## PLANNED FIGURES (from figure plan)\n"]
                        for _fig in _fp_data:
                            if isinstance(_fig, dict):
                                _fid = _fig.get("figure_id", "unnamed")
                                _ftitle = _fig.get("title", "")
                                _fcap = _fig.get("caption", "")
                                _fsec = _fig.get("section", "results")
                                _desc_parts.append(
                                    f"- **{_fid}** ({_fsec}): {_ftitle}\n  {_fcap}"
                                )
                        if len(_desc_parts) > 1:
                            _fa_descriptions = "\n".join(_desc_parts)
                except (json.JSONDecodeError, OSError):
                    pass
                if _fa_descriptions:
                    break
            if _fa_descriptions:
                break
        if _fa_descriptions:
            break

    if _fa_descriptions:
        exp_metrics_instruction += "\n\n" + _fa_descriptions
        logger.info("Stage 17: Injected FigureAgent figure descriptions into paper draft prompt")
    else:
        # Fallback: scan for chart PNG files in stage-16 (RESULT_ANALYSIS) and stage-14
        _chart_files: list[str] = []
        for _chart_stage_pattern in ("stage-16*", "stage-14*"):
            for _chart_stage_dir in sorted(active_stage_glob(run_dir, _chart_stage_pattern)):
                _charts_path = _chart_stage_dir / "charts"
                if _charts_path.is_dir():
                    for _cf in sorted(_charts_path.glob("*.png")):
                        _chart_files.append(_cf.name)
        if _chart_files:
            _manifest = _read_figure_manifest(run_dir)
            _n = len(_chart_files)
            _chart_block = (
                f"\n\n## AVAILABLE DATA FIGURES — ALL {_n} MUST be embedded\n"
                f"The following {_n} figures were generated from actual experiment "
                "data. You MUST reference **ALL** of them in the Results / "
                "Analysis sections using markdown image syntax: "
                "`![Caption](charts/filename.png)`\n"
                "Each figure MUST have a descriptive caption and 2-3 sentences "
                "of discussion explaining what the figure shows.\n\n"
            )
            for _cf_name in _chart_files:
                _meta = _manifest.get(_cf_name, {})
                _title = _meta.get(
                    "title",
                    _cf_name.replace("_", " ").replace(".png", "").title(),
                )
                _caption = _meta.get("caption", "")
                _section = _meta.get("paper_section", "Results")
                _chart_block += (
                    f"- `charts/{_cf_name}` — **{_title}** "
                    f"(place in {_section})\n"
                )
                if _caption:
                    _chart_block += f"  Suggested caption: {_caption}\n"
            _chart_block += (
                "\nCRITICAL: A paper missing ANY of the above figures will be "
                "desk-rejected. Distribute them across Results and Analysis "
                "sections as indicated.\n"
            )
            exp_metrics_instruction += _chart_block
            logger.info(
                "Stage 17: Injected %d chart references (ALL required) "
                "into paper draft prompt",
                len(_chart_files),
            )

    # WS-5.5 → FIGURE_PROMPT: Build figure_prompt_instruction for non-data figures
    _venue_style = "IEEE two-column conference"
    if not str(getattr(config.export, "target_conference", "")).lower().startswith("ieee"):
        _venue_style = "NeurIPS / ICML / ICLR"
    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    if _paper_len_cfg == "short":
        _venue_style = "workshop / short paper"

    figure_prompt_instruction = (
        "\n\n## FIGURE GENERATION INSTRUCTIONS (MANDATORY)\n"
        "This paper uses a hybrid figure pipeline:\n\n"
        "### A. Non-data figures (architecture, method, pipeline, concept diagrams)\n"
        "For EVERY non-data figure, emit a structured FIGURE_PROMPT block "
        "using HTML comments **inline in the markdown** at the position where the "
        "figure should appear. Format:\n\n"
        "```\n"
        "<!-- FIGURE_PROMPT\n"
        "figure_id: unique_snake_case_id\n"
        "figure_type: architecture_diagram | method_flowchart | pipeline_overview | "
        "concept_illustration | system_diagram | comparison_illustration | "
        "attention_visualization\n"
        "section: Method | Results | Introduction | Discussion\n"
        "caption: \"Descriptive caption for the figure.\"\n"
        "aspect_ratio: 16:9\n"
        "prompt: |\n"
        "  A professional academic figure for a {venue} paper. "
        "[Describe: subject, layout (left-to-right / top-to-bottom), "
        "components (boxes, arrows, layers), labels, colors, annotations.] "
        "Use clean vector-style lines on a white background. "
        "Text must be legible at column width (~3.25 in). "
        "Color palette: blues and grays with one accent color for novelty.\n"
        "-->\n"
        "```\n"
        f"Replace {{venue}} with the target venue style: **{_venue_style}**.\n\n"
        "Then reference it in text:\n"
        "*[Figure N: <caption> — to be generated]*\n\n"
        "#### Required non-data figures:\n"
        "1. **Teaser figure** in the Introduction section — a high-level "
        "conceptual illustration showing the core idea/motivation at a glance "
        "(figure_type: concept_illustration). This is the FIRST figure in the "
        "paper and should convey the key insight without requiring method details. "
        "Place it right after the first introductory paragraph.\n"
        "2. **Framework / architecture overview** in the Method section "
        "(figure_type: architecture_diagram or pipeline_overview). This shows "
        "the full system/method pipeline with components, data flow, and key modules.\n"
        "3. **Method detail figure** — at least ONE additional figure in the Method "
        "section showing a key algorithmic step, comparison diagram, or attention "
        "visualization (figure_type: method_flowchart | comparison_illustration | "
        "attention_visualization).\n\n"
        "#### Prompt quality rules:\n"
        "- Prompt MUST be >= 50 words and self-contained.\n"
        "- Describe spatial layout (e.g., 'three boxes arranged left-to-right "
        "connected by arrows').\n"
        "- Specify labels for every component.\n"
        "- Mention the color scheme (e.g., 'blue boxes for encoder, orange for "
        "decoder, gray arrows for data flow').\n"
        "- Include the academic style: 'clean, professional, suitable for a "
        f"top-tier {_venue_style} paper'.\n\n"
        "### B. Data figures (charts, plots, heatmaps)\n"
        "Continue using `![Caption](charts/filename.png)` for pre-generated "
        "data charts, OR inline LaTeX/TikZ code blocks for simple data plots.\n"
        "Do NOT use FIGURE_PROMPT for data-driven visualizations.\n"
    )

    # P5: Extract hyperparameters from results.json for paper Method section
    _hp_table = ""
    for _s14_dir in sorted(active_stage_glob(run_dir, "stage-14*")):
        for _run_file in sorted(_s14_dir.glob("runs/*.json")):
            try:
                _run_data = json.loads(_run_file.read_text(encoding="utf-8"))
                if isinstance(_run_data, dict) and _run_data.get("hyperparameters"):
                    _hp = _run_data["hyperparameters"]
                    if isinstance(_hp, dict) and _hp:
                        _hp_table = "\n\n## HYPERPARAMETERS (include as a table in the Method section)\n"
                        _hp_table += "| Hyperparameter | Value |\n|---|---|\n"
                        for _hk, _hv in sorted(_hp.items()):
                            _hp_table += f"| {_hk} | {_hv} |\n"
                        _hp_table += (
                            "\nThis table MUST appear in the Method/Experiments section. "
                            "Include ALL hyperparameters used, with justification for key choices.\n"
                        )
                        break
            except (json.JSONDecodeError, OSError):
                continue
        if _hp_table:
            break
    # Also check staging dirs for results.json
    if not _hp_table:
        for _staging_dir in sorted(active_stage_glob(run_dir, "stage-*/runs/_docker_*")):
            _rjson = _staging_dir / "results.json"
            if _rjson.is_file():
                try:
                    _rdata = json.loads(_rjson.read_text(encoding="utf-8"))
                    if isinstance(_rdata, dict) and _rdata.get("hyperparameters"):
                        _hp = _rdata["hyperparameters"]
                        if isinstance(_hp, dict) and _hp:
                            _hp_table = "\n\n## HYPERPARAMETERS (include as a table in the Method section)\n"
                            _hp_table += "| Hyperparameter | Value |\n|---|---|\n"
                            for _hk, _hv in sorted(_hp.items()):
                                _hp_table += f"| {_hk} | {_hv} |\n"
                            _hp_table += (
                                "\nThis table MUST appear in the Method/Experiments section. "
                                "Include ALL hyperparameters used, with justification for key choices.\n"
                            )
                            break
                except (json.JSONDecodeError, OSError):
                    continue
    if _hp_table:
        exp_metrics_instruction += _hp_table

    if _paper_len_cfg in {"deterministic", "fallback"}:
        draft = _build_deterministic_paper_draft(
            topic=config.research.topic,
            outline=outline,
            analysis=_read_prior_artifact(run_dir, "analysis.md") or "",
            exp_summary_text=exp_summary_text,
            raw_metrics_block=raw_metrics_block,
            citation_context=_build_deterministic_citation_context(run_dir),
        )
        (stage_dir / "paper_draft.md").write_text(draft, encoding="utf-8")
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.DONE,
            artifacts=("paper_draft.md",),
            evidence_refs=("stage-20/paper_draft.md",),
        )

    # F2.6: Build citation list from references.bib / candidates with cite_keys
    citation_instruction = ""
    bib_text = _read_literature_bib(run_dir)

    # P3: Pre-verify citations before paper draft — remove hallucinated refs
    if bib_text and bib_text.strip():
        from researchclaw.literature.verify import (
            filter_verified_bibtex,
            verify_citations as _verify_cit,
        )
        try:
            _pre_report = _verify_cit(bib_text, inter_verify_delay=0.5)
            _kept = _pre_report.verified + _pre_report.suspicious
            _removed = _pre_report.hallucinated
            if _removed > 0:
                bib_text = filter_verified_bibtex(
                    bib_text, _pre_report, include_suspicious=True
                )
                (stage_dir / "references_preverified.bib").write_text(
                    bib_text, encoding="utf-8"
                )
                logger.info(
                    "P3: Pre-verification kept %d/%d citations (removed %d hallucinated)",
                    _kept, _pre_report.total, _removed,
                )
        except Exception as exc:
            logger.warning("P3: Pre-verification failed, using original bib: %s", exc)

    candidates_text = _read_prior_artifact(run_dir, "candidates.jsonl")
    if candidates_text:
        cite_lines: list[str] = []
        for row_text in candidates_text.strip().splitlines():
            row = _safe_json_loads(row_text, {})
            if isinstance(row, dict) and row.get("cite_key"):
                authors_info = ""
                if isinstance(row.get("authors"), list) and row["authors"]:
                    first_author = row["authors"][0]
                    if isinstance(first_author, dict):
                        # BUG-38: name may be non-str (tuple/list) — force str
                        _name = first_author.get("name", "")
                        authors_info = _name if isinstance(_name, str) else str(_name)
                    elif isinstance(first_author, str):
                        authors_info = first_author
                    if len(row["authors"]) > 1:
                        authors_info += " et al."
                title = row.get("title", "")
                cite_lines.append(
                    f"- [{row['cite_key']}] → TITLE: \"{title}\" "
                    f"| {authors_info} "
                    f"({row.get('venue', '')}, {row.get('year', '')}, "
                    f"cited {row.get('citation_count', 0)} times) "
                    f"| ONLY cite this key when discussing: {title}"
                )
        if cite_lines:
            citation_instruction = (
                "\n\nAVAILABLE REFERENCES (use [cite_key] to cite in the text):\n"
                + "\n".join(cite_lines)
                + "\n\nCRITICAL CITATION RULES:\n"
                "- In the body text, cite using [cite_key] format, e.g. [smith2024transformer].\n"
                "- Do NOT write a References section — it will be auto-generated from the bibliography file.\n"
                "- Do NOT invent any references or arXiv IDs not in the above list.\n"
                "- You may cite a subset, but NEVER fabricate citations or change arXiv IDs.\n"
                "- SEMANTIC MATCHING: Before citing a reference, verify that its TITLE matches\n"
                "  the concept you are discussing. Do NOT use an unrelated cite_key just\n"
                "  because it sounds similar.\n"
                "- If no reference in the list matches the concept you want to cite,\n"
                "  write 'prior work has shown...' WITHOUT a citation, rather than using\n"
                "  a mismatched reference.\n"
                "- Each [cite_key] MUST correspond to the paper whose title is shown\n"
                "  next to that key in the list above. Cross-check before citing.\n"
                "\nCITATION QUANTITY & QUALITY CONSTRAINTS:\n"
                "- Cite 25-40 unique references in the paper body. The Related Work\n"
                "  section alone should cite at least 15 references.\n"
                "- Every citation MUST be directly relevant to the paper's topic.\n"
                "- DO NOT cite papers from unrelated domains (wireless communication, "
                "manufacturing, UAV, etc.).\n"
                "- Prefer well-known, highly-cited papers over obscure ones.\n"
                "- If unsure whether a paper exists or is relevant, DO NOT cite it.\n"
            )

    if llm is not None:
        _pm = prompts or PromptManager()
        topic_constraint = _pm.block("topic_constraint", topic=config.research.topic)

        # --- Section-by-section writing (3 calls) for conference-grade depth ---
        import signal as _signal

        class _PaperDraftTimeout(Exception):
            pass

        def _paper_timeout_handler(signum, frame):  # noqa: ARG001
            raise _PaperDraftTimeout()

        _timeout_sec = _paper_draft_timeout_sec(config)
        _old_handler = _signal.signal(_signal.SIGALRM, _paper_timeout_handler)
        _signal.alarm(max(1, _timeout_sec))
        try:
            draft = _write_paper_sections(
                llm=llm,
                pm=_pm,
                run_dir=run_dir,
                preamble=preamble,
                topic_constraint=topic_constraint,
                exp_metrics_instruction=exp_metrics_instruction,
                citation_instruction=citation_instruction,
                outline=outline,
                model_name=config.llm.primary_model,
                discussion_ablation=discussion_ablation,
                figure_prompt_instruction=figure_prompt_instruction,
                target_conference=str(getattr(config.export, "target_conference", "") or ""),
                target_pages=int(getattr(config.export, "target_pages", 0) or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Stage 20: LLM paper draft generation failed/timed out after %ss — using deterministic factual draft: %s",
                _timeout_sec,
                exc,
            )
            draft = _build_deterministic_paper_draft(
                topic=config.research.topic,
                outline=outline,
                analysis=_read_prior_artifact(run_dir, "analysis.md") or "",
                exp_summary_text=exp_summary_text,
                raw_metrics_block=raw_metrics_block,
            )
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, _old_handler)

        # R7: Strip LLM-generated References section — it often fabricates arXiv IDs.
        import re as _re_r7
        ref_pattern = _re_r7.compile(
            r'^(#{1,2}\s*References.*)', _re_r7.MULTILINE | _re_r7.DOTALL
        )
        ref_match = ref_pattern.search(draft)
        if ref_match:
            draft = draft[:ref_match.start()].rstrip()
            logger.info("Stage 20: Stripped LLM-generated References section (R7 fix)")
    else:
        # Build template with real data if available
        results_section = "Template results summary."
        if exp_summary_text:
            exp_summary = _safe_json_loads(exp_summary_text, {})
            if isinstance(exp_summary, dict) and exp_summary.get("metrics_summary"):
                lines = ["Experiment results:"]
                for mk, mv in exp_summary["metrics_summary"].items():
                    if isinstance(mv, dict):
                        lines.append(
                            f"- {mk}: mean={mv.get('mean')}, min={mv.get('min')}, "
                            f"max={mv.get('max')}, n={mv.get('count')}"
                        )
                results_section = "\n".join(lines)

        draft = f"""# Draft Title

## Abstract
Template draft abstract.

## Introduction
Template introduction for {config.research.topic}.

## Related Work
Template related work.

## Method
Template method description.

## Experiments
Template experimental setup.

## Results
{results_section}

## Limitations
Template limitations.

## Conclusion
Template conclusion.

## References
Template references.

Generated: {_utcnow_iso()}
"""
    (stage_dir / "paper_draft.md").write_text(draft, encoding="utf-8")

    # Extract FIGURE_PROMPT blocks, render via NanoBanana, write figure_prompts.json
    _topic = config.research.topic if hasattr(config, "research") else ""
    _fig_prompts = _extract_figure_prompts(draft, topic=_topic)
    _draft_artifacts = ["paper_draft.md"]
    if _fig_prompts:
        _fig_prompts = _render_figure_prompts(
            _fig_prompts, stage_dir, config, llm, topic=_topic,
        )
        _fp_path = stage_dir / "figure_prompts.json"
        _fp_path.write_text(
            json.dumps(_fig_prompts, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _draft_artifacts.append("figure_prompts.json")
        for _fp in _fig_prompts:
            if _fp.get("success") and _fp.get("output_path"):
                _rel = Path(_fp["output_path"]).name
                _draft_artifacts.append(f"figures/{_rel}")
        logger.info(
            "Stage 20: Extracted %d figure prompts → %s",
            len(_fig_prompts), _fp_path,
        )

    # Validate draft quality (section balance + bullet density)
    _validate_draft_quality(draft, stage_dir=stage_dir)

    return StageResult(
        stage=Stage.PAPER_DRAFT,
        status=StageStatus.DONE,
        artifacts=tuple(_draft_artifacts),
        evidence_refs=tuple(f"stage-20/{a}" for a in _draft_artifacts),
    )
