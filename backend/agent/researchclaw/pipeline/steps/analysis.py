"""Result analysis and fallback evidence collection."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.ideation.roles import (
    _multi_perspective_generate,
    _synthesize_perspectives,
)
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import (
    _collect_json_context,
    _read_prior_artifact,
    _utcnow_iso,
)
from researchclaw.pipeline.support.context import _build_context_preamble
from researchclaw.pipeline.support.experiment_evidence import _collect_experiment_results
from researchclaw.pipeline.support.paths import backend_services_directory
from researchclaw.pipeline.support.visuals import _run_chart_generation
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _execute_result_analysis(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    # --- Agentic analysis: let the LLM read files, write scripts, produce summary ---
    from researchclaw.pipeline.result_analysis import ResultAnalysisRuntime
    _agent_rt = ResultAnalysisRuntime()
    _agent_result = _agent_rt.execute(
        stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
    )
    # If the agent produced both analysis.md and experiment_summary.json, use
    # them directly and skip the legacy hardcoded collection path.
    _agent_summary = stage_dir / "experiment_summary.json"
    _agent_analysis = stage_dir / "analysis.md"
    if _agent_summary.is_file() and _agent_analysis.is_file():
        try:
            _parsed = json.loads(_agent_summary.read_text(encoding="utf-8"))
            if isinstance(_parsed, dict) and _parsed.get("metrics_summary"):
                logger.info(
                    "S16: Using agentic experiment_summary.json (%d metric keys)",
                    len(_parsed["metrics_summary"]),
                )
                # Still run chart generation from the legacy path
                _run_chart_generation(stage_dir, run_dir, config, llm)
                artifacts = ["analysis.md", "experiment_summary.json"]
                if (stage_dir / "charts").is_dir() and any((stage_dir / "charts").iterdir()):
                    artifacts.append("charts/")
                return StageResult(
                    stage=Stage.RESULT_ANALYSIS,
                    status=StageStatus.DONE,
                    artifacts=tuple(artifacts),
                    evidence_refs=tuple(f"stage-16/{a}" for a in artifacts),
                )
        except (json.JSONDecodeError, OSError):
            pass
    logger.info("S16: Agentic analysis insufficient, falling back to legacy path")

    # --- Legacy: Collect experiment data ---
    exp_data = _collect_experiment_results(run_dir)
    runs_dir = _read_prior_artifact(run_dir, "runs/") or ""
    context = ""
    if runs_dir:
        context = _collect_json_context(Path(runs_dir), max_files=30)

    # --- R13-1: Merge Stage 13 (ITERATIVE_REFINE) results if available ---
    # Stage 13 stores richer per-condition metrics in refinement_log.json
    # that _collect_experiment_results() misses (it only scans runs/ dirs).
    _refine_log_text = _read_prior_artifact(run_dir, "refinement_log.json")
    if _refine_log_text:
        try:
            _refine_data = json.loads(_refine_log_text)
            _best_iter = None
            _best_ver = _refine_data.get("best_version", "")
            _refine_iterations = _refine_data.get("iterations", [])
            if not isinstance(_refine_iterations, list):
                logger.warning(
                    "R13-1: refinement_log.json has non-list iterations=%r; "
                    "skipping refinement merge",
                    type(_refine_iterations).__name__,
                )
                _refine_iterations = []
            for _it in _refine_iterations:
                if not isinstance(_it, dict):
                    continue
                _sbx = _it.get("sandbox", {})
                _it_metrics = _sbx.get("metrics", {})
                if _it.get("version_dir", "") == _best_ver and _it_metrics:
                    _best_iter = _it
                    break
            # If no version match, take the first iteration with metrics
            if _best_iter is None:
                for _it in _refine_iterations:
                    if not isinstance(_it, dict):
                        continue
                    _sbx = _it.get("sandbox", {})
                    if _sbx.get("metrics"):
                        _best_iter = _it
                        break
            if _best_iter is not None:
                _sbx = _best_iter.get("sandbox", {})
                _refine_metrics = _sbx.get("metrics", {})
                if _refine_metrics and (
                    not exp_data["metrics_summary"]
                    or len(_refine_metrics) > len(exp_data["metrics_summary"])
                ):
                    # Refinement has richer data — rebuild metrics_summary from it
                    _new_summary: dict[str, dict[str, float | None]] = {}
                    for _mk, _mv in _refine_metrics.items():
                        try:
                            _fv = float(_mv)
                            _new_summary[_mk] = {
                                "min": round(_fv, 6),
                                "max": round(_fv, 6),
                                "mean": round(_fv, 6),
                                "count": 1,
                            }
                        except (ValueError, TypeError):
                            pass
                    if _new_summary:
                        exp_data["metrics_summary"] = _new_summary
                        # Also update best_run with refinement data
                        exp_data["best_run"] = {
                            "run_id": "iterative-refine-best",
                            "task_id": "sandbox-main",
                            "status": "completed",
                            "metrics": {
                                k: v for k, v in _refine_metrics.items()
                            },
                            "elapsed_sec": _sbx.get("elapsed_sec", 0),
                            "stdout": "",  # omit for brevity
                            "stderr": _sbx.get("stderr", ""),
                            "timed_out": _sbx.get("timed_out", False),
                        }
                        # Rebuild latex table
                        _ltx = [
                            r"\begin{table}[h]", r"\centering",
                            r"\caption{Experiment Results (Best Refinement Iteration)}",
                            r"\begin{tabular}{lrrrr}", r"\hline",
                            r"Metric & Min & Max & Mean & N \\", r"\hline",
                        ]
                        for _col in sorted(_new_summary.keys()):
                            _s = _new_summary[_col]
                            _ltx.append(
                                f"{_col} & {_s['min']:.4f} & {_s['max']:.4f} "
                                f"& {_s['mean']:.4f} & {_s['count']} \\\\"
                            )
                        _ltx.extend([r"\hline", r"\end{tabular}", r"\end{table}"])
                        exp_data["latex_table"] = "\n".join(_ltx)
                        # Count unique conditions (keys without 'seed' and not ending in _mean/_std)
                        _conditions = {
                            k for k in _refine_metrics
                            if "seed" not in k and not k.endswith("_std")
                        }
                        exp_data["runs"] = [exp_data["best_run"]]
                        # Store condition count for accurate reporting
                        exp_data["best_run"]["condition_count"] = len(_conditions)
                        if not context:
                            context = json.dumps(
                                {"refinement_best_metrics": _refine_metrics},
                                indent=2, default=str,
                            )
                        logger.info(
                            "R13-1: Merged %d metrics from refinement_log (best_metric=%.4f)",
                            len(_refine_metrics),
                            _refine_data.get("best_metric", 0),
                        )
        except (json.JSONDecodeError, OSError, KeyError):
            logger.warning("R13-1: Failed to parse refinement_log.json, using Stage 14 data")

    # --- R19-2: Extract PAIRED comparisons from refinement stdout ---
    from researchclaw.experiment.sandbox import extract_paired_comparisons as _extract_paired

    _all_paired: list[dict[str, object]] = []
    # First: from _collect_experiment_results (Stage 14 runs/)
    if exp_data.get("paired_comparisons"):
        _all_paired.extend(exp_data["paired_comparisons"])
    # Second: from refinement_log iterations (Stage 13)
    if _refine_log_text:
        try:
            _rl = json.loads(_refine_log_text)
            _refine_iterations = _rl.get("iterations", [])
            if not isinstance(_refine_iterations, list):
                _refine_iterations = []
            for _it in _refine_iterations:
                if not isinstance(_it, dict):
                    continue
                for _sbx_key in ("sandbox", "sandbox_after_fix"):
                    _sbx_stdout = (_it.get(_sbx_key) or {}).get("stdout", "")
                    if _sbx_stdout:
                        _all_paired.extend(_extract_paired(_sbx_stdout))
        except (json.JSONDecodeError, OSError):
            pass

    # --- R19-3: Build structured condition_summaries from metrics ---
    _condition_summaries: dict[str, dict[str, Any]] = {}
    _ms = exp_data.get("metrics_summary", {})
    _best_metrics = {}
    if exp_data.get("best_run") and isinstance(exp_data["best_run"], dict):
        _best_metrics = exp_data["best_run"].get("metrics", {})

    # Group metrics by condition prefix (e.g., "ppo/primary_metric" → condition "ppo")
    for _mk, _mv in _best_metrics.items():
        parts = _mk.split("/")
        if len(parts) >= 2:
            cond = parts[0]
            metric_name = parts[-1]
            if cond not in _condition_summaries:
                _condition_summaries[cond] = {"metrics": {}}
            try:
                _condition_summaries[cond]["metrics"][metric_name] = float(_mv)
            except (ValueError, TypeError):
                pass

    # BUG-09 fix: If no condition summaries were built (metrics don't use
    # condition/metric format), try to extract from metrics_summary or
    # structured_results so FigureAgent has data to work with.
    if not _condition_summaries and _ms:
        # Try to parse condition data from metrics_summary keys
        for _mk, _mv in _ms.items():
            parts = _mk.split("/")
            if len(parts) >= 2:
                cond = parts[0]
                metric_name = parts[-1]
                if cond not in _condition_summaries:
                    _condition_summaries[cond] = {"metrics": {}}
                try:
                    _val = float(_mv) if not isinstance(_mv, dict) else None
                    if _val is not None:
                        _condition_summaries[cond]["metrics"][metric_name] = _val
                except (ValueError, TypeError):
                    pass
    if not _condition_summaries:
        # Last resort: build from structured_results condition keys
        _sr = exp_data.get("structured_results", {})
        if isinstance(_sr, dict):
            for _sk, _sv in _sr.items():
                if isinstance(_sv, dict) and _sk not in ("metadata", "config"):
                    _condition_summaries[_sk] = {"metrics": {}}
                    for _smk, _smv in _sv.items():
                        try:
                            _condition_summaries[_sk]["metrics"][_smk] = float(_smv)
                        except (ValueError, TypeError):
                            pass

    # R33: Build per-seed data structure (needed for CIs and paired tests below)
    _seed_data: dict[str, dict[int, float]] = {}  # {condition: {seed: value}}
    for _mk, _mv in _best_metrics.items():
        parts = _mk.split("/")
        # Pattern: condition/regime/seed_id/primary_metric
        if len(parts) >= 4 and parts[-1] == config.experiment.metric_key:
            cond = parts[0]
            try:
                seed_id = int(parts[2])
                val = float(_mv)
                _seed_data.setdefault(cond, {})[seed_id] = val
            except (ValueError, TypeError):
                pass

    # Enrich condition summaries with seed counts, success rates, and CIs
    for _ck, _cv in _condition_summaries.items():
        # Look for success_rate in metrics
        sr_key = f"{_ck}/success_rate"
        if sr_key in _best_metrics:
            try:
                _cv["success_rate"] = float(_best_metrics[sr_key])
            except (ValueError, TypeError):
                pass
        # Count seed-level entries to estimate n_seeds
        _seed_count = 0
        for _mk in _best_metrics:
            if _mk.startswith(f"{_ck}/") and "seed" in _mk.lower():
                _seed_count += 1
        if _seed_count > 0:
            _cv["n_seed_metrics"] = _seed_count

        # R33: Compute mean ± std and bootstrap 95% CI from per-seed data
        if _ck in _seed_data and len(_seed_data[_ck]) >= 3:
            _vals = list(_seed_data[_ck].values())
            import statistics as _stats_mod
            _mean = _stats_mod.mean(_vals)
            _std = _stats_mod.stdev(_vals)
            _cv["metrics"][f"{config.experiment.metric_key}_mean"] = round(_mean, 6)
            _cv["metrics"][f"{config.experiment.metric_key}_std"] = round(_std, 6)
            _cv["n_seeds"] = len(_vals)
            # Bootstrap 95% CI
            import random as _rng
            _rng.seed(42)
            _boot_means = []
            for _ in range(1000):
                _sample = [_rng.choice(_vals) for _ in range(len(_vals))]
                _boot_means.append(_stats_mod.mean(_sample))
            _boot_means.sort()
            _ci_low = round(_boot_means[int(0.025 * len(_boot_means))], 6)
            _ci_high = round(_boot_means[int(0.975 * len(_boot_means))], 6)
            # IMP-16: Sanity check — CI must contain the mean
            if _ci_low > _mean or _ci_high < _mean:
                logger.warning(
                    "Bootstrap CI [%.4f, %.4f] does not contain mean %.4f "
                    "for condition %s — replacing CI with mean ± 1.96*SE",
                    _ci_low, _ci_high, _mean, _ck,
                )
                _se = _std / (len(_vals) ** 0.5)
                _ci_low = round(_mean - 1.96 * _se, 6)
                _ci_high = round(_mean + 1.96 * _se, 6)
            _cv["ci95_low"] = _ci_low
            _cv["ci95_high"] = _ci_high

    # Count totals
    _total_conditions = len(_condition_summaries) if _condition_summaries else None
    _total_metrics = len(_best_metrics) if _best_metrics else None

    # --- R33: Pipeline-level paired computation as fallback ---
    # If the experiment code's PAIRED lines are sparse or suspicious (e.g.,
    # all identical t-stats), compute fresh paired tests from per-seed data.
    # (_seed_data was built above before condition summary enrichment)
    if len(_seed_data) >= 2:
        # Find common seeds across conditions
        _all_seeds_sets = [set(v.keys()) for v in _seed_data.values()]
        _common_seeds = set.intersection(*_all_seeds_sets) if _all_seeds_sets else set()

        if len(_common_seeds) >= 3:
            _cond_names_sorted = sorted(_seed_data.keys())
            _pipeline_paired: list[dict[str, object]] = []
            # Compare each condition against the first baseline (alphabetically)
            _baseline_cond = _cond_names_sorted[0]
            for _other_cond in _cond_names_sorted[1:]:
                _diffs = []
                for _sid in sorted(_common_seeds):
                    _diffs.append(
                        _seed_data[_other_cond][_sid] - _seed_data[_baseline_cond][_sid]
                    )
                if _diffs:
                    import statistics
                    _n = len(_diffs)
                    _mean_d = statistics.mean(_diffs)
                    _std_d = statistics.stdev(_diffs) if _n > 1 else 0.0
                    _t = (_mean_d / (_std_d / (_n ** 0.5))) if _std_d > 0 else 0.0
                    _df = _n - 1
                    # Two-tailed p-value using t-distribution
                    import math
                    try:
                        from scipy.stats import t as _t_dist
                        _p = float(2 * _t_dist.sf(abs(_t), _df))
                    except ImportError:
                        _p = 2 * (1 - 0.5 * (1 + math.erf(abs(_t) / (2 ** 0.5))))
                        if _df < 30:
                            _p = min(1.0, _p * (1 + 2.5 / max(_df, 1)))
                    _pipeline_paired.append({
                        "method": _other_cond,
                        "baseline": _baseline_cond,
                        "mean_diff": round(_mean_d, 6),
                        "std_diff": round(_std_d, 6),
                        "t_stat": round(_t, 4),
                        "p_value": round(_p, 6),
                        "n_seeds": _n,
                        "source": "pipeline_computed",
                    })

            # Use pipeline-computed if experiment code's are suspicious
            _exp_t_stats = {round(p.get("t_stat", 0), 4) for p in _all_paired}
            _all_identical = len(_exp_t_stats) <= 1 and len(_all_paired) > 1
            if _pipeline_paired and (_all_identical or len(_all_paired) < len(_pipeline_paired)):
                logger.info(
                    "R33: Using %d pipeline-computed paired tests (experiment code had %d, identical=%s)",
                    len(_pipeline_paired), len(_all_paired), _all_identical,
                )
                _all_paired = _pipeline_paired

    # --- P8: Detect identical conditions (broken ablations) ---
    _ablation_warnings: list[str] = []
    if _condition_summaries and len(_condition_summaries) >= 2:
        _cond_names = sorted(_condition_summaries.keys())
        for _i in range(len(_cond_names)):
            for _j in range(_i + 1, len(_cond_names)):
                _c1, _c2 = _cond_names[_i], _cond_names[_j]
                _s1 = _condition_summaries[_c1]
                _s2 = _condition_summaries[_c2]
                # Compare mean values for all shared metrics
                _shared_keys = set(_s1.keys()) & set(_s2.keys())
                if not _shared_keys:
                    continue
                _all_equal = True
                for _sk in _shared_keys:
                    _v1 = _s1[_sk].get("mean") if isinstance(_s1[_sk], dict) else _s1[_sk]
                    _v2 = _s2[_sk].get("mean") if isinstance(_s2[_sk], dict) else _s2[_sk]
                    if _v1 != _v2:
                        _all_equal = False
                        break
                if _all_equal and _shared_keys:
                    _warn = (
                        f"ABLATION FAILURE: Conditions '{_c1}' and '{_c2}' produce "
                        f"identical outputs across all {len(_shared_keys)} metrics. "
                        f"The ablation is invalid — the differentiating parameter "
                        f"is likely not used in the code."
                    )
                    _ablation_warnings.append(_warn)
                    logger.warning("P8: %s", _warn)
                elif _shared_keys:
                    # R5-BUG-03: Also flag near-identical conditions (< 1% relative diff)
                    _near_identical = True
                    for _sk in _shared_keys:
                        _v1 = _s1[_sk].get("mean") if isinstance(_s1[_sk], dict) else _s1[_sk]
                        _v2 = _s2[_sk].get("mean") if isinstance(_s2[_sk], dict) else _s2[_sk]
                        try:
                            _v1f, _v2f = float(_v1), float(_v2)
                            _denom = max(abs(_v1f), abs(_v2f), 1e-12)
                            if abs(_v1f - _v2f) / _denom > 0.01:
                                _near_identical = False
                                break
                        except (TypeError, ValueError):
                            _near_identical = False
                            break
                    if _near_identical:
                        _warn = (
                            f"ABLATION WARNING: Conditions '{_c1}' and '{_c2}' produce "
                            f"near-identical outputs (<1% relative difference) across "
                            f"all {len(_shared_keys)} metrics. The ablation may be trivial."
                        )
                        _ablation_warnings.append(_warn)
                        logger.warning("P8: %s", _warn)

    # --- Write structured experiment summary ---
    summary_payload = {
        "metrics_summary": exp_data["metrics_summary"],
        "total_runs": len(exp_data["runs"]),
        "best_run": exp_data["best_run"],
        "latex_table": exp_data["latex_table"],
        "generated": _utcnow_iso(),
    }
    # R13-1: Detect zero-variance across conditions (all conditions identical primary metric)
    if _condition_summaries and len(_condition_summaries) >= 2:
        _primary_vals = []
        for _cs in _condition_summaries.values():
            if isinstance(_cs, dict):
                # Try 'metrics' dict first (actual structure), then 'primary_metric' fallback
                _metrics = _cs.get("metrics", {})
                if isinstance(_metrics, dict) and _metrics:
                    _pv_candidate = next(iter(_metrics.values()), None)
                    if isinstance(_pv_candidate, dict):
                        _pv_candidate = _pv_candidate.get("mean")
                    if isinstance(_pv_candidate, (int, float)):
                        _primary_vals.append(_pv_candidate)
                        continue
                _pm = _cs.get("primary_metric", {})
                _pv = _pm.get("mean") if isinstance(_pm, dict) else _pm
                if isinstance(_pv, (int, float)):
                    _primary_vals.append(_pv)
        if len(_primary_vals) >= 2 and len(set(_primary_vals)) == 1:
            _zv_warn = (
                f"ZERO VARIANCE: All {len(_primary_vals)} conditions have "
                f"identical primary_metric ({_primary_vals[0]}). "
                f"Experiment condition wiring is likely broken."
            )
            _ablation_warnings.append(_zv_warn)
            logger.warning("R13-1: %s", _zv_warn)

    if _ablation_warnings:
        summary_payload["ablation_warnings"] = _ablation_warnings
    if _all_paired:
        summary_payload["paired_comparisons"] = _all_paired
    if _condition_summaries:
        summary_payload["condition_summaries"] = _condition_summaries
        summary_payload["condition_metrics"] = _condition_summaries  # alias for quality gate
        summary_payload["total_conditions"] = _total_conditions
    if _total_metrics:
        summary_payload["total_metric_keys"] = _total_metrics
    (stage_dir / "experiment_summary.json").write_text(
        json.dumps(summary_payload, indent=2, default=str), encoding="utf-8"
    )
    if exp_data["latex_table"]:
        (stage_dir / "results_table.tex").write_text(
            exp_data["latex_table"], encoding="utf-8"
        )

    # --- Build data-augmented prompt ---
    preamble = _build_context_preamble(
        config, run_dir, include_goal=True, include_hypotheses=True
    )
    data_context = ""
    if exp_data["metrics_summary"]:
        lines = ["\n## Quantitative Results"]
        for mk, mv in exp_data["metrics_summary"].items():
            if isinstance(mv, dict):
                lines.append(
                    f"- {mk}: mean={mv.get('mean', '?')}, min={mv.get('min', '?')}, "
                    f"max={mv.get('max', '?')}, n={mv.get('count', '?')}"
                )
        data_context = "\n".join(lines)

    # Append structured results if available
    if exp_data.get("structured_results"):
        structured_text = json.dumps(
            exp_data["structured_results"], indent=2, default=str
        )
        # Truncate to avoid blowing up context
        if len(structured_text) > 6000:
            structured_text = structured_text[:6000] + "\n... (truncated)"
        data_context += (
            f"\n\n## Structured Experiment Results (from results.json)\n"
            f"```json\n{structured_text}\n```"
        )

    # P8: Inject ablation warnings into data context
    if _ablation_warnings:
        data_context += "\n\nCRITICAL ABLATION WARNINGS:\n"
        for _aw in _ablation_warnings:
            data_context += f"- {_aw}\n"
        data_context += (
            "\nYou MUST address these in your analysis. Identical conditions "
            "mean the ablation design is broken and the comparison is meaningless.\n"
        )

    if llm is not None:
        _pm = prompts or PromptManager()
        from researchclaw.prompts import DEBATE_ROLES_ANALYSIS  # noqa: PLC0415

        # --- Multi-perspective debate ---
        perspectives_dir = stage_dir / "perspectives"
        variables = {
            "preamble": preamble,
            "data_context": data_context,
            "context": context,
        }
        perspectives = _multi_perspective_generate(
            llm, DEBATE_ROLES_ANALYSIS, variables, perspectives_dir
        )
        # --- Synthesize into unified analysis ---
        analysis = _synthesize_perspectives(
            llm, perspectives, "analysis_synthesize", _pm
        )
    else:
        # Template with real data if available
        ms = exp_data["metrics_summary"]
        metrics_block = ""
        if ms:
            for mk, mv in ms.items():
                if isinstance(mv, dict):
                    metrics_block += (
                        f"- **{mk}**: mean={mv.get('mean')}, "
                        f"min={mv.get('min')}, max={mv.get('max')}, n={mv.get('count')}\n"
                    )
        else:
            metrics_block = f"- Primary metric key: `{config.experiment.metric_key}`\n- No quantitative data yet.\n"

        analysis = f"""# Result Analysis

## Metrics Summary
{metrics_block}
## Comparative Findings
- Proposed approach results from {len(exp_data["runs"])} run(s) collected.

## Statistical Checks
- Recommend confidence interval and seed-wise variance reporting.

## Limitations
- Limited runs and synthetic constraints.

## Conclusion
- Proceed to decision stage with moderate confidence.

Generated: {_utcnow_iso()}
"""
    (stage_dir / "analysis.md").write_text(analysis, encoding="utf-8")

    artifacts = ["analysis.md", "experiment_summary.json"]
    if (stage_dir / "results_table.tex").exists():
        artifacts.append("results_table.tex")

    # IMP-6 + FA: Generate charts early (Stage 16) so paper draft can reference them
    # Try FigureAgent first (multi-agent intelligent charts), fall back to visualize.py
    _figure_plan_saved = False
    if config.experiment.figure_agent.enabled and llm is not None:
        try:
            from researchclaw.agents.figure_agent import FigureOrchestrator
            from researchclaw.agents.figure_agent.orchestrator import FigureAgentConfig as _FACfg

            _fa_cfg = _FACfg(
                enabled=True,
                min_figures=config.experiment.figure_agent.min_figures,
                max_figures=config.experiment.figure_agent.max_figures,
                max_iterations=config.experiment.figure_agent.max_iterations,
                render_timeout_sec=config.experiment.figure_agent.render_timeout_sec,
                use_docker=config.experiment.figure_agent.use_docker,
                docker_image=config.experiment.figure_agent.docker_image,
                output_format=config.experiment.figure_agent.output_format,
                gemini_api_key=config.experiment.figure_agent.gemini_api_key,
                gemini_model=config.experiment.figure_agent.gemini_model,
                nano_banana_enabled=config.experiment.figure_agent.nano_banana_enabled,
                strict_mode=config.experiment.figure_agent.strict_mode,
                dpi=config.experiment.figure_agent.dpi,
            )
            _fa = FigureOrchestrator(llm, _fa_cfg, stage_dir=stage_dir)

            # Build conditions list from condition_summaries
            _fa_conditions = list(_condition_summaries.keys()) if _condition_summaries else []

            # BUG-09 fix: pass best_run metrics as fallback data if
            # structured_results is empty, so Planner has some data to chart
            _fa_exp_results = exp_data.get("structured_results", {})
            if not _fa_exp_results and _best_metrics:
                _fa_exp_results = {"best_run_metrics": _best_metrics}

            # Read paper draft for Decision Agent analysis
            _paper_draft = (
                _read_prior_artifact(run_dir, "paper_draft.md")
                or _read_prior_artifact(run_dir, "outline.md")
                or ""
            )

            _fa_plan = _fa.orchestrate({
                "experiment_results": _fa_exp_results,
                "condition_summaries": _condition_summaries,
                "metrics_summary": exp_data.get("metrics_summary", {}),
                "metric_key": config.experiment.metric_key,
                "conditions": _fa_conditions,
                "topic": _read_prior_artifact(run_dir, "topic.md") or config.research.topic,
                "hypothesis": _read_prior_artifact(run_dir, "hypotheses.md") or "",
                "paper_draft": _paper_draft,
                "output_dir": str(stage_dir / "charts"),
            })

            if _fa_plan.figure_count > 0:
                # Save figure plan for Stage 17 to read
                (stage_dir / "figure_plan.json").write_text(
                    json.dumps(_fa_plan.to_dict(), indent=2, default=str),
                    encoding="utf-8",
                )
                _figure_plan_saved = True
                for _cf_name in _fa_plan.get_chart_files():
                    artifacts.append(f"charts/{_cf_name}")
                logger.info(
                    "Stage 16: FigureAgent generated %d charts (%d passed review, %.1fs)",
                    _fa_plan.figure_count,
                    _fa_plan.passed_count,
                    _fa_plan.elapsed_sec,
                )
            else:
                logger.warning("Stage 16: FigureAgent produced no charts, falling back")
        except Exception as _fa_exc:
            logger.warning("Stage 16: FigureAgent failed (%s), falling back to visualize.py", _fa_exc)

    # Fallback: legacy visualize.py chart generation
    if not _figure_plan_saved:
        try:
            from researchclaw.experiment.visualize import (
                generate_all_charts as _gen_charts_early,
            )

            _charts_dir = stage_dir / "charts"
            _early_charts = _gen_charts_early(
                run_dir,
                _charts_dir,
                metric_key=config.experiment.metric_key,
            )
            if _early_charts:
                for _cp in _early_charts:
                    artifacts.append(f"charts/{_cp.name}")
                logger.info(
                    "Stage 16: Generated %d early charts (legacy) for paper embedding",
                    len(_early_charts),
                )
        except Exception as _chart_exc:
            logger.warning("Stage 16: Early chart generation failed: %s", _chart_exc)

    # --- Register baseline results to shared registry ---
    _shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
    if _shared_dir:
        try:
            import sys as _sys_rr
            _services_dir = str(backend_services_directory())
            if _services_dir not in _sys_rr.path:
                _sys_rr.path.insert(0, _services_dir)
            from result_registry import ResultRegistry
            _registry = ResultRegistry(_shared_dir)
            _summary_path = stage_dir / "experiment_summary.json"
            if _summary_path.exists():
                _summary = json.loads(_summary_path.read_text(encoding="utf-8"))
                _best = _summary.get("best_run", {})
                _metrics = _best.get("metrics", {})
                if _metrics:
                    _registry.register(
                        project_id=getattr(config.project, "name", "unknown"),
                        description=f"{config.research.topic[:100]} - baseline results",
                        model="",
                        dataset="",
                        task=config.research.topic[:50],
                        metrics={k: float(v) for k, v in _metrics.items() if isinstance(v, (int, float))},
                        tags=[d.lower() for d in config.research.domains] + ["baseline"],
                        source_stage=int(Stage.RESULT_ANALYSIS),
                    )
                    logger.info("Registered %d metrics to shared results registry", len(_metrics))
                    print(f"[SHARED RESULTS] Registered baseline metrics: {list(_metrics.keys())}", flush=True)
        except Exception as _rr_exc:
            logger.debug("Shared results registration failed: %s", _rr_exc)

    return StageResult(
        stage=Stage.RESULT_ANALYSIS,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-16/{a}" for a in artifacts),
    )
