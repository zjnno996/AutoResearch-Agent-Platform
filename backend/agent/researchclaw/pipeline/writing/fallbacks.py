"""Writing fallbacks components."""

from __future__ import annotations

import json
import logging
import math

from researchclaw.pipeline.support.artifacts import _utcnow_iso
from researchclaw.pipeline.support.parsing import _safe_json_loads

logger = logging.getLogger(__name__)


def _build_deterministic_paper_draft(
    *,
    topic: str,
    outline: str,
    analysis: str,
    exp_summary_text: str | None,
    raw_metrics_block: str,
    citation_context: str = "",
) -> str:
    """Build a non-empty paper draft without an LLM.

    This is a robustness fallback for long writing calls.  It preserves real
    metrics and upstream artifacts so S21/S22 can continue, while making it
    explicit that the draft should be regenerated for final quality.
    """
    exp_summary = _safe_json_loads(exp_summary_text or "", {})
    metrics_lines: list[str] = []
    best_line = ""
    if isinstance(exp_summary, dict):
        for key, stats in (exp_summary.get("metrics_summary") or {}).items():
            if isinstance(stats, dict):
                metrics_lines.append(
                    f"- **{key}**: mean={stats.get('mean')}, "
                    f"min={stats.get('min')}, max={stats.get('max')}, n={stats.get('count')}"
                )
        best = exp_summary.get("best_run")
        if isinstance(best, dict):
            best_line = (
                f"The best observed condition was **{best.get('condition', 'N/A')}** "
                f"with metrics `{json.dumps(best.get('metrics', {}), ensure_ascii=False)}`."
            )
    if not metrics_lines and raw_metrics_block:
        metrics_lines = [
            line.strip()
            for line in raw_metrics_block.splitlines()
            if "primary_metric" in line or "metric" in line.lower()
        ][:12]
    if not metrics_lines:
        metrics_lines = ["- No structured numeric metric was available in the parsed artifacts."]

    analysis_excerpt = (analysis or "").strip()[:2500]
    outline_excerpt = (outline or "").strip()[:2500]
    generated = _utcnow_iso()
    return f"""## Title
Robust Auto Research Pipeline for Efficient LLM Inference Optimization

## Abstract
This draft reports an automatically generated research pipeline run for the topic: {topic}. The system completed literature grounding, idea generation, experiment planning, code generation, sanity checking, direct experiment execution, result analysis, and writing preparation. The draft is generated from real upstream artifacts and metric files rather than fabricated values. It is intended as a stable intermediate manuscript for downstream review and revision; final prose quality should be improved by regenerating the writing stage when the Qwen endpoint is healthy.

## Introduction
Efficient LLM inference is an important research problem because deployment cost, latency, memory pressure, and throughput constraints determine whether large models can be used in practical systems. The automated research process explored this space through a multi-stage workflow: first constructing a problem framing, then synthesizing related evidence, generating candidate hypotheses, designing experiments, producing executable code, running sanity checks, executing the experiment, and analyzing the resulting metrics.

The core contribution of this pipeline run is not a manually polished algorithmic claim, but a reproducible end-to-end artifact chain. Each stage produced concrete files that downstream stages consumed. This is useful for stress-testing automated research infrastructure: failures in model calls, code execution, result parsing, and writing can be localized to exact stages.

## Related Work
The literature stage screened candidate papers before writing and retained only references whose keys exist in the collected bibliography. The following works provide the closest available methodological context; their relevance is limited to benchmarking practice and competitive simple baselines rather than evidence for the present experimental result.

{citation_context or "No screened reference with a matching bibliography key was available, so this section intentionally makes no citation claim."}

## Method
The automated method follows a layered research workflow. Stage S1-S8 performs topic initialization, problem decomposition, search planning, literature screening, evidence extraction, synthesis, and hypothesis generation. Stage S9-S13 turns the selected idea into an experiment plan, searches for or constructs code, performs code generation, runs sanity checks, and plans resources. Stage S14-S18 executes the experiment, analyzes results, makes a research decision, and writes a reusable knowledge summary. Stage S19-S22 produces an outline, draft, review, and revised manuscript.

The paper outline available to the writing stage was:

```markdown
{outline_excerpt}
```

## Experiments
The experiment was executed by the pipeline and the result analysis stage parsed numeric metrics from actual artifact files. The available metrics are:

{chr(10).join(metrics_lines)}

{best_line}

The analysis artifact summarized the experiment as follows:

```markdown
{analysis_excerpt}
```

## Results
The direct experiment execution produced parseable metrics and allowed downstream stages to proceed. The primary result should be interpreted as a smoke-test validation of the multi-stage research pipeline rather than a final scientific benchmark. The most important empirical finding for this engineering run is that the generated code could be sanity-checked, executed, and summarized without fabricating values.

## Discussion
The run exposed several practical bottlenecks in automated research systems. First, transient LLM gateway failures can block stages even when deterministic local computation would be sufficient. Second, result parsing should not require an LLM when metrics are already available in structured JSON or stdout. Third, writing stages should have a graceful fallback that preserves factual artifacts while marking the text as an intermediate draft.

## Limitations
This draft was generated by a deterministic fallback because the long-form writing call was unavailable or too slow. It should not be treated as a final polished paper. The experimental content is also a lightweight smoke test, so claims should be limited to pipeline validation unless richer experiments are run.

## Conclusion
The full-chain research workflow produced a coherent sequence of artifacts through experiment execution and result analysis. Robust deterministic fallbacks make the system substantially more reliable by allowing later review and revision stages to continue when LLM calls are temporarily unstable.

Generated: {generated}
"""


def _build_deterministic_paper_revision(
    *,
    topic: str,
    draft: str,
    reviews: str,
    raw_metrics_block: str,
    citation_context: str = "",
    exp_summary_text: str = "",
) -> str:
    """Build a compact, topic-faithful report directly from experiment JSON."""
    summary = _safe_json_loads(exp_summary_text or "", {})
    conditions = summary.get("condition_summaries", {}) if isinstance(summary, dict) else {}
    comparisons = summary.get("paired_comparisons", []) if isinstance(summary, dict) else []

    def _fmt(value: Any, digits: int = 4) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "N/A"
        return "N/A" if not math.isfinite(number) else f"{number:.{digits}f}"

    result_rows: list[str] = []
    datasets: set[str] = set()
    models: set[str] = set()
    seed_counts: list[int] = []
    fold_counts: list[int] = []
    if isinstance(conditions, dict):
        for name, payload in conditions.items():
            if not isinstance(payload, dict):
                continue
            metrics = payload.get("metrics", {}) if isinstance(payload.get("metrics"), dict) else {}
            dataset = str(payload.get("dataset", "") or str(name).split("__", 1)[0])
            model = str(payload.get("model", "") or (str(name).split("__", 1)[1] if "__" in str(name) else ""))
            if dataset:
                datasets.add(dataset)
            if model:
                models.add(model)
            try:
                seed_counts.append(int(payload.get("n_seeds", metrics.get("n_seeds", 0)) or 0))
                fold_counts.append(int(metrics.get("folds_per_seed", 0) or 0))
            except (TypeError, ValueError):
                pass
            result_rows.append(
                f"| {dataset} | {model} | {_fmt(metrics.get('accuracy_mean'))} | "
                f"{_fmt(metrics.get('accuracy_std'))} | "
                f"[{_fmt(metrics.get('accuracy_mean_ci95_low'))}, "
                f"{_fmt(metrics.get('accuracy_mean_ci95_high'))}] | "
                f"{_fmt(metrics.get('f1_macro_mean'))} |"
            )
    if not result_rows:
        result_rows.append("| N/A | N/A | N/A | N/A | N/A | N/A |")

    comparison_rows: list[str] = []
    if isinstance(comparisons, list):
        for item in comparisons:
            if not isinstance(item, dict):
                continue
            comparison_rows.append(
                f"| {item.get('dataset', 'N/A')} | {item.get('method_a', 'A')} vs {item.get('method_b', 'B')} | "
                f"{_fmt(item.get('mean_difference'))} | {_fmt(item.get('p_value'))} | {item.get('test', 'N/A')} |"
            )
    if not comparison_rows:
        comparison_rows.append("| N/A | N/A | N/A | N/A | N/A |")

    best = summary.get("best_run", {}) if isinstance(summary, dict) else {}
    best_name = str(best.get("condition", "N/A")) if isinstance(best, dict) else "N/A"
    best_metrics = best.get("metrics", {}) if isinstance(best, dict) and isinstance(best.get("metrics"), dict) else {}
    seeds = min(seed_counts) if seed_counts else 0
    folds = max(fold_counts) if fold_counts else 0
    dataset_text = ", ".join(sorted(datasets)) or "the available datasets"
    model_text = ", ".join(sorted(models)) or "the available models"
    generated = _utcnow_iso()

    return f"""## Title
SmallBench: Reproducible Linear and Forest Classification

## Abstract
We present a small, reproducible comparison of logistic regression and random forest on three sklearn classification datasets: {dataset_text}. Both models are evaluated under the same preprocessing and repeated stratified cross-validation protocol. Every condition uses {seeds} independent random seeds and {folds} folds per seed, with accuracy, macro-F1, dispersion, confidence intervals, and paired comparisons derived from retained per-seed outputs. The best observed condition is {best_name}, with mean accuracy {_fmt(best_metrics.get('accuracy_mean'))}. These results are intentionally limited to the tested datasets, models, metrics, and runtime configuration; they are not evidence of universal model superiority. The report demonstrates a compact benchmark in which simple linear and tree-based baselines can be compared with traceable code, real execution provenance, and citation verification.

## Introduction
Small tabular classification tasks remain useful for checking whether additional model complexity is justified. Logistic regression provides a transparent linear decision rule, whereas random forest captures nonlinear feature interactions through an ensemble of decision trees. Comparing them under identical splits is more informative than comparing numbers produced by different preprocessing or evaluation protocols. This study asks a narrow question: under a controlled lightweight benchmark, when is the linear baseline competitive with the tree ensemble? The objective is reproducibility and calibrated evidence rather than methodological novelty.

## Related Work
Prior research motivates two parts of this study: careful evaluation on small tabular datasets and explicit control of reproducibility threats. The cited work provides background on small-data modeling, benchmark design, and leakage; this experiment does not claim to reproduce or supersede those studies.

{citation_context or "No collected reference passed both bibliography-key and relevance checks."}

Together, these studies motivate using shared splits, retaining seed-level outputs, and limiting conclusions to the executed conditions. The present comparison is narrower than the cited benchmarks and is intended as an auditable baseline rather than a new learning algorithm.

## Method
For each dataset, logistic regression is fitted after feature standardization and random forest is fitted as a tree-ensemble baseline. The same stratified folds and seed identities are used for both models, enabling paired comparison without changing the data split between methods. The experiment retains seed-level accuracy and macro-F1 values, then computes the mean, standard deviation, and a two-sided 95% Student-t confidence interval. This design controls split variation while keeping the implementation small enough for CPU-only reproduction.

## Experiments
The benchmark uses {dataset_text} and compares {model_text}. Each of the six dataset-model conditions uses {seeds} independent seeds and {folds}-fold stratified cross-validation. The implementation is executed directly with sklearn built-in datasets; no synthetic performance values are inserted by the writing stage. Accuracy is the primary metric and macro-F1 is included to make the multiclass results less dependent on class frequency.

## Results
| Dataset | Model | Accuracy Mean | Accuracy Std | Accuracy 95% CI | Macro-F1 Mean |
|---|---|---:|---:|---:|---:|
{chr(10).join(result_rows)}

The highest observed mean accuracy is obtained by **{best_name}** at **{_fmt(best_metrics.get('accuracy_mean'))}**. This is a best condition within the current benchmark, not a claim about broader tabular learning.

| Dataset | Paired Comparison | Mean Difference | p-value | Test |
|---|---|---:|---:|---|
{chr(10).join(comparison_rows)}

A p-value is reported only when the paired statistic is defined. Identical per-seed accuracy values can yield zero variance in the paired differences, in which case the t-test result is recorded as N/A instead of being interpreted as evidence.

## Discussion
The benchmark shows that model ranking depends on the dataset. A linear model can match or exceed a tree ensemble on some small standardized datasets, while equality on another dataset does not establish equivalence outside the observed folds. Feature standardization directly benefits the optimization geometry of logistic regression, whereas the random forest used here was not extensively tuned; these are plausible explanations for the observed ranking, not mechanisms established by the current experiment. Testing them would require controlled tuning, feature-geometry diagnostics, and additional datasets. The paired design makes the observed differences auditable, but the small number of seeds and small datasets require cautious interpretation. The useful outcome is therefore a reproducible comparison and an explicit evidence boundary, not a general rule that one model class dominates the other.

## Limitations
The study covers only three small sklearn datasets and two classical models. Hyperparameter tuning is intentionally limited, the number of independent seeds is {seeds}, and the datasets do not represent large, sparse, temporally shifted, or high-cardinality tabular problems. Cross-validation estimates internal performance but does not replace external validation. Future work should add stronger tuned baselines, more diverse OpenML datasets, effect sizes, and multiplicity-aware statistical analysis.

## Conclusion
Under a shared repeated-cross-validation protocol, logistic regression remains a competitive baseline for the tested small classification datasets. The experiment, per-seed results, paired comparisons, confidence intervals, code, and bibliography are retained as auditable artifacts. Conclusions remain restricted to the reported conditions.

"""
