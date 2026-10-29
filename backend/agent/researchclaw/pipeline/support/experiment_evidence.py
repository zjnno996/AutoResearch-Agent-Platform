"""Executed experiment evidence for analysis and defensible writing."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

import yaml

from researchclaw.pipeline.support.artifacts import _read_prior_artifact, active_stage_glob
from researchclaw.pipeline.support.parsing import _safe_json_loads

logger = logging.getLogger(__name__)


def _collect_experiment_results(
    run_dir: Path,
    metric_key: str = "",
    metric_direction: str = "maximize",
) -> dict[str, Any]:
    """Aggregate experiment metrics from runs/ directory across prior stages.

    Returns a dict with ``runs``, ``metrics_summary``, ``best_run``,
    ``latex_table``, and optionally ``structured_results``.
    """
    runs_data: list[dict[str, Any]] = []
    structured_results: Any = None

    # Scan all stage dirs for runs/ subdirectory
    for stage_subdir in sorted(active_stage_glob(run_dir, "stage-*/runs")):
        # Check for structured results.json first
        results_json = stage_subdir / "results.json"
        if results_json.exists() and structured_results is None:
            try:
                structured_results = json.loads(
                    results_json.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                pass

        for run_file in sorted(stage_subdir.glob("*.json")):
            if run_file.name == "results.json":
                continue  # Already handled above
            parsed = _safe_json_loads(run_file.read_text(encoding="utf-8"), {})
            if isinstance(parsed, dict) and "metrics" in parsed:
                # Also check for structured_results inside run payload
                if "structured_results" in parsed and structured_results is None:
                    structured_results = parsed["structured_results"]
                runs_data.append(parsed)
            elif isinstance(parsed, dict) and "key_metrics" in parsed:
                # Simulated mode uses key_metrics
                parsed["metrics"] = parsed.pop("key_metrics")
                runs_data.append(parsed)

    # Fallback: synthesise runs_data from structured_results if it has summaries
    if not runs_data and isinstance(structured_results, dict):
        _summaries = structured_results.get("summaries", [])
        if isinstance(_summaries, list):
            for _s in _summaries:
                if not isinstance(_s, dict):
                    continue
                _synth_metrics: dict[str, float] = {}
                for _mk, _mv in _s.items():
                    if _mk == "method":
                        continue
                    try:
                        _synth_metrics[_mk] = float(_mv)
                    except (ValueError, TypeError):
                        if isinstance(_mv, list):
                            continue
                if _synth_metrics:
                    runs_data.append({
                        "metrics": _synth_metrics,
                        "condition": _s.get("method", "unknown"),
                    })

    if not runs_data:
        result: dict[str, Any] = {"runs": [], "metrics_summary": {}, "best_run": None, "latex_table": ""}
        if structured_results is not None:
            result["structured_results"] = structured_results
        return result

    # Aggregate metrics across runs
    all_metric_keys: set[str] = set()
    for r in runs_data:
        m = r.get("metrics") or {}
        if isinstance(m, dict):
            all_metric_keys.update(m.keys())

    metrics_summary: dict[str, dict[str, float | None]] = {}
    for key in sorted(all_metric_keys):
        values = []
        for r in runs_data:
            m = r.get("metrics") or {}
            if isinstance(m, dict) and key in m:
                try:
                    values.append(float(m[key]))
                except (ValueError, TypeError):
                    pass
        if values:
            metrics_summary[key] = {
                "min": round(min(values), 6),
                "max": round(max(values), 6),
                "mean": round(sum(values) / len(values), 6),
                "count": len(values),
            }

    # Find best run using metric_key and metric_direction
    best_run: dict[str, Any] | None = None
    if runs_data:

        def _primary_metric(r: dict[str, Any]) -> float:
            m = r.get("metrics") or {}
            if isinstance(m, dict):
                # Try specific metric_key first
                if metric_key and metric_key in m:
                    try:
                        return float(m[metric_key])
                    except (ValueError, TypeError):
                        pass
                # Fallback to first metric
                for v in m.values():
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        _cmp = min if metric_direction == "minimize" else max
        best_run = _cmp(runs_data, key=_primary_metric)

    # Build LaTeX table
    latex_lines = [
        r"\begin{table}[h]",
        r"\centering",
        r"\caption{Experiment Results}",
    ]
    if metrics_summary:
        cols = sorted(metrics_summary.keys())
        header = "Metric & Min & Max & Mean & N \\\\"
        latex_lines.append(r"\begin{tabular}{l" + "r" * 4 + "}")
        latex_lines.append(r"\hline")
        latex_lines.append(header)
        latex_lines.append(r"\hline")
        for col in cols:
            s = metrics_summary[col]
            row = f"{col} & {s['min']:.4f} & {s['max']:.4f} & {s['mean']:.4f} & {s['count']} \\\\"
            latex_lines.append(row)
        latex_lines.append(r"\hline")
        latex_lines.append(r"\end{tabular}")
    else:
        latex_lines.append(r"\begin{tabular}{l}")
        latex_lines.append("No experiment data available \\\\")
        latex_lines.append(r"\end{tabular}")
    latex_lines.append(r"\end{table}")

    # R18-1: Extract paired statistical comparisons from stdout
    from researchclaw.experiment.sandbox import extract_paired_comparisons

    paired_comparisons: list[dict[str, object]] = []
    for r in runs_data:
        stdout = r.get("stdout", "")
        if stdout:
            paired_comparisons.extend(extract_paired_comparisons(stdout))

    collected: dict[str, Any] = {
        "runs": runs_data,
        "metrics_summary": metrics_summary,
        "best_run": best_run,
        "latex_table": "\n".join(latex_lines),
    }
    if paired_comparisons:
        collected["paired_comparisons"] = paired_comparisons
    if structured_results is not None:
        collected["structured_results"] = structured_results
    return collected


def _build_experiment_fact_contract(run_dir: Path) -> str:
    """Build a compact factual contract from executable and observed artifacts.

    Generated prose is deliberately excluded.  This prevents a later writing
    model from replacing an official split with a plausible-looking split or
    confusing precomputed features with raw sensor windows.
    """
    try:
        plan = yaml.safe_load(_read_prior_artifact(run_dir, "exp_plan.yaml") or "") or {}
    except yaml.YAMLError:
        plan = {}
    summary = _safe_json_loads(
        _read_prior_artifact(run_dir, "experiment_summary.json") or "{}", {}
    )
    if not isinstance(plan, dict):
        plan = {}
    if not isinstance(summary, dict):
        summary = {}

    facts: list[str] = [
        "## NON-NEGOTIABLE EXECUTED-EXPERIMENT FACT CONTRACT",
        "These facts come from the executable plan, dataset files, and result JSON. "
        "They supersede any conflicting prose in earlier goals, hypotheses, decisions, or outlines.",
    ]
    datasets = plan.get("datasets", [])
    if isinstance(datasets, list):
        for dataset in datasets:
            if not isinstance(dataset, dict):
                continue
            facts.append(f"- Dataset: {dataset.get('name', 'unspecified')}")
            if dataset.get("source"):
                facts.append(f"- Dataset source: {dataset['source']}")
            if dataset.get("split_strategy"):
                facts.append(f"- Split policy: {dataset['split_strategy']}")
            if dataset.get("preprocessing"):
                facts.append(f"- Input representation/preprocessing: {dataset['preprocessing']}")

    baselines = plan.get("baselines", [])
    baseline_names = [
        str(item.get("name")) if isinstance(item, dict) else str(item)
        for item in baselines if str(item).strip()
    ] if isinstance(baselines, list) else []
    if baseline_names:
        facts.append(f"- Actually requested methods: {baseline_names}")
    proposed = plan.get("proposed_methods", [])
    if isinstance(proposed, list) and not proposed:
        facts.append(
            "- Proposed methods: none. Do not invent or brand a new model, method, framework, "
            "protocol, algorithm, or acronym; frame the document as a baseline reproduction/audit."
        )
    seeds = plan.get("seeds") or (plan.get("evaluation_protocol", {}) or {}).get("independent_seeds")
    if isinstance(seeds, list):
        facts.append(f"- Exact random seeds: {seeds}")
    metrics = plan.get("metrics")
    if isinstance(metrics, list):
        facts.append(f"- Exact reported metrics: {metrics}")
    if summary:
        facts.append(
            f"- Observed scale: {summary.get('condition_count', len(summary.get('condition_summaries', {})))} "
            f"conditions and {summary.get('total_runs', 0)} per-seed runs."
        )
        condition_summaries = summary.get("condition_summaries", {})
        if isinstance(condition_summaries, dict):
            for condition, condition_data in condition_summaries.items():
                if not isinstance(condition_data, dict):
                    continue
                observed = condition_data.get("metrics", {})
                if isinstance(observed, dict) and observed:
                    facts.append(
                        f"- Exact observed aggregate for {condition}: "
                        + json.dumps(observed, sort_keys=True, ensure_ascii=False)
                    )
                seed_metrics = condition_data.get("seed_metrics", {})
                if isinstance(seed_metrics, dict) and seed_metrics:
                    facts.append(
                        f"- Exact per-seed observations for {condition}: "
                        + json.dumps(seed_metrics, sort_keys=True, ensure_ascii=False)
                    )
        comparisons = summary.get("paired_comparisons", [])
        if isinstance(comparisons, list) and comparisons:
            facts.append(
                "- Exact paired statistical tests: "
                + json.dumps(comparisons, sort_keys=True, ensure_ascii=False)
            )
        facts.append(
            "- Evidence granularity: only aggregate and per-seed Accuracy/Macro-F1 are available. "
            "No per-activity, per-class, raw-signal, latency, memory, energy, or deep-model results "
            "were executed; do not describe or plot them."
        )

    # Recover the exact official subject partition and matrix dimensions from
    # the archived real dataset rather than trusting model-generated prose.
    stage11 = run_dir / "stage-11"
    subject_train_paths = sorted(stage11.rglob("subject_train.txt")) if stage11.exists() else []
    subject_test_paths = sorted(stage11.rglob("subject_test.txt")) if stage11.exists() else []
    if subject_train_paths and subject_test_paths:
        try:
            train_tokens = subject_train_paths[0].read_text(encoding="utf-8").split()
            test_tokens = subject_test_paths[0].read_text(encoding="utf-8").split()
            train_subjects = sorted({int(float(value)) for value in train_tokens})
            test_subjects = sorted({int(float(value)) for value in test_tokens})
            facts.append(f"- Official train subjects ({len(train_subjects)}): {train_subjects}")
            facts.append(f"- Official test subjects ({len(test_subjects)}): {test_subjects}")
            facts.append(f"- Observed sample counts: train={len(train_tokens)}, test={len(test_tokens)}")
            x_train = subject_train_paths[0].parent / "X_train.txt"
            if x_train.exists():
                first_row = x_train.open("r", encoding="utf-8").readline().split()
                if first_row:
                    facts.append(f"- Observed input feature count: {len(first_row)} precomputed features")
        except (OSError, ValueError):
            pass
    facts.append(
        "- Never replace the exact subject IDs with a contiguous range and never call the "
        "561-dimensional precomputed feature matrix raw sensor input."
    )
    return "\n".join(facts)


def _detect_runtime_issues(sandbox_result: Any) -> str:
    """Detect NaN/Inf in metrics and extract stderr warnings from sandbox run.

    Returns a formatted string describing all runtime issues, or empty string
    if no issues are found.
    """
    import math

    issues: list[str] = []

    # Check metrics for NaN/Inf
    metrics = getattr(sandbox_result, "metrics", {}) or {}
    for key, val in metrics.items():
        try:
            fval = float(val)
            if math.isnan(fval):
                issues.append(f"METRIC NaN: '{key}' returned NaN — likely a division by zero or invalid computation in code")
            elif math.isinf(fval):
                issues.append(f"METRIC Inf: '{key}' returned Infinity — likely overflow or unbounded computation")
        except (TypeError, ValueError):
            pass

    # Check stdout for NaN values (word boundary to avoid matching "Nanotechnology" etc.)
    stdout = getattr(sandbox_result, "stdout", "") or ""
    _nan_re = re.compile(r"\bnan\b", re.IGNORECASE)
    if _nan_re.search(stdout):
        nan_lines = [
            line.strip()
            for line in stdout.splitlines()
            if _nan_re.search(line)
        ]
        if nan_lines:
            issues.append(
                f"NaN values detected in output:\n" + "\n".join(nan_lines[:10])
            )

    # Extract meaningful warnings from stderr
    stderr = getattr(sandbox_result, "stderr", "") or ""
    if stderr.strip():
        warning_lines = []
        for line in stderr.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            # Keep RuntimeWarning, ValueError, ZeroDivisionError, etc.
            if any(
                kw in line_stripped
                for kw in (
                    "Warning",
                    "Error",
                    "Traceback",
                    "Exception",
                    "divide",
                    "overflow",
                    "invalid value",
                    "NaN",
                    "inf",
                )
            ):
                warning_lines.append(line_stripped)
        if warning_lines:
            issues.append(
                "Runtime warnings/errors from stderr:\n"
                + "\n".join(warning_lines[:15])
            )

    # Check for identical metric values across all entries in stdout
    # (e.g., all algorithms reporting convergence_rate=1.0)
    stdout = getattr(sandbox_result, "stdout", "") or ""
    if stdout:
        from collections import Counter

        metric_values_by_name: dict[str, list[float]] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            parts = line.rsplit(":", 1)
            if len(parts) != 2:
                continue
            try:
                fval = float(parts[1].strip())
            except (ValueError, TypeError):
                continue
            # Extract metric suffix (e.g. "convergence_rate" from "UCB (Stochastic) convergence_rate")
            name = parts[0].strip()
            metric_suffix = name.split()[-1] if name.split() else name
            metric_values_by_name.setdefault(metric_suffix, []).append(fval)

        for metric_name, vals in metric_values_by_name.items():
            if len(vals) >= 3:
                unique = set(vals)
                if len(unique) <= 2:
                    issues.append(
                        f"DUMMY METRIC: '{metric_name}' has only {len(unique)} unique value(s) "
                        f"across {len(vals)} entries ({unique}) — likely a placeholder. "
                        f"Implement real measurement logic (e.g., track iterations to convergence)."
                    )

    # R5-3: Check for diverging loss values (fast-fail indicator)
    for key, val in metrics.items():
        try:
            fval = float(val)
            if "loss" in key.lower() and fval > 100:
                issues.append(
                    f"DIVERGING LOSS: '{key}' = {fval} (>100) — the optimization is "
                    f"diverging. Reduce learning rate, check gradient computation, "
                    f"or add gradient clipping."
                )
        except (TypeError, ValueError):
            pass

    if not issues:
        return ""

    return (
        "## Runtime Issues Detected\n\n"
        "The experiment code ran but produced problematic results. "
        "Fix the ROOT CAUSE of these issues in the code:\n\n"
        + "\n\n".join(f"- {issue}" for issue in issues)
    )


def _collect_raw_experiment_metrics(run_dir: Path) -> tuple[str, bool]:
    """Collect raw experiment metric lines from stdout for paper writing.

    Returns a tuple of (formatted block, has_parsed_metrics).
    ``has_parsed_metrics`` is True when at least one run had a non-empty
    ``metrics`` dict in its JSON payload — a reliable signal of real data.
    """
    metric_lines: list[str] = []
    run_count = 0
    has_parsed_metrics = False

    for stage_subdir in sorted(active_stage_glob(run_dir, "stage-*/runs")):
        for run_file in sorted(stage_subdir.glob("*.json")):
            if run_file.name == "results.json":
                continue
            try:
                payload = json.loads(run_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue

            # R10: Skip simulated data — only collect real experiment results
            if payload.get("status") == "simulated":
                continue

            run_count += 1

            # Extract from parsed metrics (check both 'metrics' and 'key_metrics')
            metrics = payload.get("metrics", {}) or payload.get("key_metrics", {})
            if isinstance(metrics, dict) and metrics:
                has_parsed_metrics = True
                for k, v in metrics.items():
                    metric_lines.append(f"  {k}: {v}")

            # Also extract from stdout for full detail
            # BUG-23: Filter out infrastructure lines that are NOT experiment results
            _INFRA_KEYS = {
                "SEED_COUNT", "TIME_ESTIMATE", "TRAINING_STEPS",
                "REGISTERED_CONDITIONS", "METRIC_DEF", "GPU_MEMORY",
                "BATCH_SIZE", "NUM_WORKERS", "TOTAL_PARAMS",
                "time_budget_sec", "max_epochs", "num_seeds",
            }
            stdout = payload.get("stdout", "")
            if stdout:
                for line in stdout.splitlines():
                    line = line.strip()
                    if ":" in line:
                        parts = line.rsplit(":", 1)
                        try:
                            float(parts[1].strip())
                            key_part = parts[0].strip().split("/")[-1]  # last segment
                            if key_part in _INFRA_KEYS:
                                continue  # skip infrastructure lines
                            metric_lines.append(f"  {line}")
                        except (ValueError, TypeError, IndexError):
                            pass

    # R19-4 + R23-1: Collect metrics from refinement_log.json (Stage 13).
    # If refinement has richer data than Stage 12 runs/, REPLACE Stage 12 data
    # to avoid confusing the paper writer with conflicting sources.
    _refine_lines: list[str] = []
    _refine_run_count = 0
    # Scan ALL refinement logs across versions, pick the richest
    _best_refine_metrics: dict[str, Any] = {}
    _best_refine_stdout = ""
    for _rl_path in [*active_stage_glob(run_dir, "stage-15/refinement_log.json"),
                     *active_stage_glob(run_dir, "stage-13/refinement_log.json")]:
        try:
            _rlog = json.loads(_rl_path.read_text(encoding="utf-8"))
            _best_ver = _rlog.get("best_version", "")
            _rlog_iters = _rlog.get("iterations", [])
            if not isinstance(_rlog_iters, list):
                _rlog_iters = []
            for _it in _rlog_iters:
                for _sbx_key in ("sandbox", "sandbox_after_fix"):
                    _sbx = _it.get(_sbx_key, {})
                    if not isinstance(_sbx, dict):
                        continue
                    _sbx_metrics = _sbx.get("metrics", {})
                    if isinstance(_sbx_metrics, dict) and len(_sbx_metrics) > len(_best_refine_metrics):
                        _best_refine_metrics = _sbx_metrics
                        _best_refine_stdout = _sbx.get("stdout", "")
        except (json.JSONDecodeError, OSError):
            pass

    if _best_refine_metrics and len(_best_refine_metrics) > len(metric_lines) // 2:
        # Refinement has richer data — REPLACE Stage 12 data to avoid conflicts
        metric_lines = []
        run_count = 1
        for k, v in _best_refine_metrics.items():
            metric_lines.append(f"  {k}: {v}")
        # Also extract PAIRED and metric lines from stdout
        if _best_refine_stdout:
            for _line in _best_refine_stdout.splitlines():
                _line = _line.strip()
                if _line.startswith("PAIRED:"):
                    metric_lines.append(f"  {_line}")
                elif ":" in _line:
                    parts = _line.rsplit(":", 1)
                    try:
                        float(parts[1].strip())
                        metric_lines.append(f"  {_line}")
                    except (ValueError, TypeError, IndexError):
                        pass
    elif _best_refine_metrics:
        # Refinement has some data but not richer — append to existing
        run_count += 1
        for k, v in _best_refine_metrics.items():
            metric_lines.append(f"  {k}: {v}")
        if _best_refine_stdout:
            for _line in _best_refine_stdout.splitlines():
                _line = _line.strip()
                if _line.startswith("PAIRED:"):
                    metric_lines.append(f"  {_line}")

    if not metric_lines:
        return "", has_parsed_metrics

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for line in metric_lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)

    # BUG-29: Reformat raw metric lines into human-readable condition summaries
    # to prevent LLM from pasting raw path-style lines into the paper
    _grouped: dict[str, list[str]] = {}
    _ungrouped: list[str] = []
    for line in unique[:200]:
        stripped = line.strip()
        # Match pattern: condition/env/step/metric: value
        parts = stripped.split("/")
        if len(parts) >= 3 and ":" in parts[-1]:
            cond = parts[0]
            detail = "/".join(parts[1:])
            _grouped.setdefault(cond, []).append(f"  - {detail}")
        else:
            _ungrouped.append(stripped)

    formatted_lines: list[str] = []
    if _grouped:
        for cond, details in sorted(_grouped.items()):
            formatted_lines.append(f"## Condition: {cond}")
            formatted_lines.extend(details[:30])
    if _ungrouped:
        formatted_lines.extend(_ungrouped)

    return (
        f"\n\nACTUAL EXPERIMENT DATA (from {run_count} run(s) — use ONLY these numbers):\n"
        "```\n"
        + "\n".join(formatted_lines[:200])
        + "\n```\n"
        "CRITICAL: Every number in the Results table MUST come from the data above. "
        "Do NOT round excessively, do NOT invent numbers, do NOT change values. "
        f"The experiment ran {run_count} time(s) — state this accurately in the methodology.\n"
        "NEVER paste raw metric paths (like 'condition/env/step/metric: value') "
        "into the paper. Always convert to formatted LaTeX tables or inline prose.\n"
    ), has_parsed_metrics


def _collect_experiment_evidence(run_dir: Path) -> str:
    """Collect actual experiment parameters and results for peer review."""
    evidence_parts: list[str] = []

    # Put the deterministic fact contract first.  It is derived from the
    # executed result artifact and archived dataset files, and therefore takes
    # precedence over any legacy smoke-run wrapper metadata.
    fact_contract = _build_experiment_fact_contract(run_dir)
    if fact_contract:
        evidence_parts.append(fact_contract)

    summary_text = _read_prior_artifact(run_dir, "experiment_summary.json")
    summary_payload = _safe_json_loads(summary_text or "{}", {})
    authoritative_total_runs = 0
    if isinstance(summary_payload, dict) and summary_payload:
        authoritative_total_runs = int(summary_payload.get("total_runs", 0) or 0)
        review_summary = {
            "total_runs": authoritative_total_runs,
            "condition_count": summary_payload.get("condition_count"),
            "condition_summaries": summary_payload.get("condition_summaries", {}),
            "paired_comparisons": summary_payload.get("paired_comparisons", []),
            "experiment_provenance": summary_payload.get("experiment_provenance", {}),
        }
        evidence_parts.append(
            "### Authoritative Executed Experiment Summary\n```json\n"
            + json.dumps(review_summary, indent=2, ensure_ascii=False)
            + "\n```"
        )

    # 1. Read experiment code to find actual trial count, methods used
    exp_dir = _read_prior_artifact(run_dir, "experiment/")
    if exp_dir and Path(exp_dir).is_dir():
        main_py = Path(exp_dir) / "main.py"
        if main_py.exists():
            code = main_py.read_text(encoding="utf-8")
            evidence_parts.append(f"### Actual Experiment Code (main.py)\n```python\n{code[:3000]}\n```")

    # 2. Read sandbox run results (actual metrics, runtime, stderr).  A domain
    # results.json stores metrics under conditions/seeds, unlike the generic
    # smoke wrapper whose top-level `metrics` field may be null.
    runs_text = _read_prior_artifact(run_dir, "runs/")
    if runs_text and Path(runs_text).is_dir():
        for run_file in sorted(Path(runs_text).glob("*.json"))[:5]:
            payload = _safe_json_loads(run_file.read_text(encoding="utf-8"), {})
            if isinstance(payload, dict):
                if isinstance(payload.get("conditions"), dict):
                    summary = {
                        "seeds": payload.get("seeds", []),
                        "conditions": payload.get("conditions", {}),
                        "statistical_tests": payload.get("statistical_tests", {}),
                    }
                else:
                    summary = {
                        "metrics": payload.get("metrics"),
                        "elapsed_sec": payload.get("elapsed_sec"),
                        "timed_out": payload.get("timed_out"),
                    }
                stderr = payload.get("stderr", "")
                if stderr:
                    summary["stderr_excerpt"] = stderr[:500]
                evidence_parts.append(
                    f"### Run Result: {run_file.name}\n```json\n{json.dumps(summary, indent=2)}\n```"
                )

    # 3. Read refinement log for actual iteration count
    refine_log_text = _read_prior_artifact(run_dir, "refinement_log.json")
    if refine_log_text:
        try:
            rlog = json.loads(refine_log_text)
            summary = {
                "iterations_executed": len(rlog.get("iterations", []) if isinstance(rlog.get("iterations"), list) else []),
                "converged": rlog.get("converged"),
                "stop_reason": rlog.get("stop_reason"),
                "best_metric": rlog.get("best_metric"),
            }
            evidence_parts.append(
                f"### Refinement Summary\n```json\n{json.dumps(summary, indent=2)}\n```"
            )
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. Report the scientific per-seed run count.  Counting JSON wrapper
    # files is wrong for domain experiments that persist all seeds in one file.
    actual_run_count = authoritative_total_runs
    if actual_run_count > 0:
        evidence_parts.append(
            f"### Actual Trial Count\n"
            f"**The experiment contains {actual_run_count} executed per-seed condition runs.** "
            f"If the paper claims a different number of trials, this is a CRITICAL discrepancy."
        )

    if not evidence_parts:
        return ""

    return (
        "\n\n## Actual Experiment Evidence\n"
        "Use the evidence below to verify the paper's methodology claims.\n\n"
        + "\n\n".join(evidence_parts)
    )
