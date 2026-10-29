"""Writing sanitize components."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

from researchclaw.pipeline.support.artifacts import (
    _read_prior_artifact,
    _utcnow_iso,
    active_stage_glob,
)
from researchclaw.pipeline.support.parsing import _safe_json_loads

logger = logging.getLogger(__name__)


def _sanitize_fabricated_data(
    paper: str,
    run_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Replace unverified numerical data in markdown tables with '---'.

    Loads experiment_summary.json as ground truth, extracts all verified
    metric values, then scans markdown tables in Results/Experiment sections.
    Numbers not matching any verified value (within 1% relative tolerance)
    are replaced with ``---``.

    Returns (sanitized_paper, sanitization_report).
    """
    import re as _re_san

    # --- 1. Build verified values set from experiment_summary.json ---
    verified_values: set[float] = set()
    exp_path = run_dir / "stage-16" / "experiment_summary.json"
    if not exp_path.exists():
        # Try versioned result-analysis outputs and legacy locations.
        candidates = [
            *sorted(active_stage_glob(run_dir, "stage-16*/experiment_summary.json"), reverse=True),
            run_dir / "stage-14" / "experiment_summary.json",
            *sorted(active_stage_glob(run_dir, "stage-14*/experiment_summary.json"), reverse=True),
        ]
        exp_path = next((candidate for candidate in candidates if candidate.exists()), exp_path)

    if exp_path.exists():
        try:
            exp_data = json.loads(exp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            exp_data = {}

        def _collect_numbers(obj: Any, depth: int = 0) -> None:
            if depth > 10:
                return
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                verified_values.add(float(obj))
            elif isinstance(obj, dict):
                for v in obj.values():
                    _collect_numbers(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    _collect_numbers(v, depth + 1)

        # Extract from well-known keys
        for key in (
            "metrics_summary", "condition_summaries", "best_run",
            "condition_metrics", "conditions", "ablation_results",
        ):
            if key in exp_data:
                _collect_numbers(exp_data[key])

    if not verified_values:
        report: dict[str, Any] = {
            "sanitized": False,
            "reason": "no verified values found in experiment_summary.json",
            "tables_processed": 0,
            "numbers_replaced": 0,
        }
        return paper, report

    def _is_verified(num: float) -> bool:
        """Check if num matches any verified value within 1% relative tolerance."""
        for v in verified_values:
            if v == 0.0:
                if abs(num) < 1e-9:
                    return True
            elif abs(num - v) / abs(v) <= 0.01:
                return True
        return False

    # --- 2. Find and sanitize markdown tables ---
    # Match markdown table blocks (header + separator + data rows)
    table_pat = _re_san.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)+"  # one or more pipe-delimited lines
        r")",
        _re_san.MULTILINE,
    )
    # Match numbers in table cells (integers, decimals, percentages, scientific)
    num_pat = _re_san.compile(
        r"(?<![a-zA-Z_])"  # not preceded by letter/underscore
        r"(-?\d+\.?\d*(?:[eE][+-]?\d+)?)"
        r"(%?)"  # optional percent
        r"(?![a-zA-Z_])"  # not followed by letter/underscore
    )

    numbers_replaced = 0
    numbers_kept = 0
    tables_processed = 0
    replaced_values: list[str] = []

    def _sanitize_table(match: _re_san.Match[str]) -> str:
        nonlocal numbers_replaced, numbers_kept, tables_processed
        table_text = match.group(0)
        lines = table_text.split("\n")

        # Check if this looks like a results/experiment table
        # (heuristic: has a separator row with dashes)
        has_separator = any(
            _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            for line in lines
        )
        if not has_separator:
            return table_text

        tables_processed += 1
        sanitized_lines: list[str] = []
        for i, line in enumerate(lines):
            # Skip header row and separator row
            is_separator = bool(
                _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            )
            is_header = i == 0  # first line is typically the header
            if is_separator or is_header:
                sanitized_lines.append(line)
                continue

            def _replace_num(m: _re_san.Match[str]) -> str:
                nonlocal numbers_replaced, numbers_kept
                num_str = m.group(1)
                pct = m.group(2)
                try:
                    val = float(num_str)
                except ValueError:
                    return m.group(0)
                if _is_verified(val):
                    numbers_kept += 1
                    return m.group(0)
                numbers_replaced += 1
                replaced_values.append(num_str + pct)
                return "---"

            sanitized_lines.append(num_pat.sub(_replace_num, line))
        return "\n".join(sanitized_lines)

    sanitized = table_pat.sub(_sanitize_table, paper)

    report = {
        "sanitized": numbers_replaced > 0,
        "tables_processed": tables_processed,
        "numbers_replaced": numbers_replaced,
        "numbers_kept": numbers_kept,
        "verified_values_count": len(verified_values),
        "replaced_samples": replaced_values[:20],
        "generated": _utcnow_iso(),
    }
    return sanitized, report


def _enforce_engineering_report_boundary(paper: str, run_dir: Path) -> tuple[str, bool]:
    """Rewrite empirical sections when execution only supports an engineering smoke report."""
    readiness = _safe_json_loads(_read_prior_artifact(run_dir, "research_readiness.json") or "", {})
    policy = str(readiness.get("writing_policy", "") or "") if isinstance(readiness, dict) else ""
    if policy not in {"engineering_report_only", "no_empirical_claims"}:
        return paper, False

    provenance = _safe_json_loads(_read_prior_artifact(run_dir, "experiment_provenance.json") or "", {})
    executed = bool(provenance.get("real_code_execution") or provenance.get("executed")) if isinstance(provenance, dict) else False
    implementation = str(provenance.get("implementation", "unrecorded") or "unrecorded") if isinstance(provenance, dict) else "unrecorded"
    execution_mode = str(provenance.get("execution_mode", "unrecorded") or "unrecorded") if isinstance(provenance, dict) else "unrecorded"

    # Real executed metrics are valid descriptive engineering evidence. Keep
    # the domain paper intact and only remove wording that upgrades those
    # observations into broad scientific superiority. The legacy section
    # replacement below is reserved for runs without real empirical evidence.
    if executed and implementation not in {"synthetic_fallback", "simulated", "unrecorded"}:
        paper = re.sub(r"state[- ]of[- ]the[- ]art", "representative", paper, flags=re.IGNORECASE)
        paper = re.sub(
            r"(?i)\b(?:proves?|definitively establishes)\b",
            "provides bounded evidence suggesting",
            paper,
        )
        paper = re.sub(r"\s*\[cite_key:[^\]]+\]", "", paper, flags=re.IGNORECASE)
        paper = re.sub(r"<!--\s*FIGURE_PROMPT\b.*?-->", "", paper, flags=re.IGNORECASE | re.DOTALL)
        paper = re.sub(
            r"(?im)^\s*\*?\[Figure[^\n]*?to be generated\]\*?\s*$",
            "",
            paper,
        )
        paper = re.sub(r"\n{3,}", "\n\n", paper)
        return paper, True

    method_match = re.search(r"^#\s+([^:\n]+)", paper, re.MULTILINE)
    method_name = method_match.group(1).strip() if method_match else "The Proposed Workflow"
    safe_title = f"# {method_name}: An Engineering Smoke Test for Visual-Inertial Research Automation"
    paper = re.sub(r"^#\s+.*$", safe_title, paper, count=1, flags=re.MULTILINE)

    replacements = {
        "Abstract": (
            "This paper documents an evidence-bounded engineering implementation of a visual-inertial research workflow. "
            "The system connects literature-derived design, executable code generation, runtime provenance capture, result parsing, and IEEE-formatted reporting. "
            f"The generated program was {'executed successfully' if executed else 'not confirmed as successfully executed'} using the {execution_mode} path with a {implementation} implementation. "
            "The run is intentionally classified as a pipeline smoke test: it verifies artifact flow and basic executability, but it does not establish accuracy, robustness, statistical significance, or superiority over baselines. "
            "We therefore report implementation behavior, evidence boundaries, and the experiments required before scientific performance claims can be made."
        ),
        "Introduction": (
            "Visual-inertial sensing combines cameras and inertial measurement units in a software pipeline whose scientific evaluation normally requires real trajectories, synchronized sensor data, controlled failure conditions, and matched baselines. "
            "This work examines a narrower question: whether an automated research workflow can turn a visual-inertial design specification into executable code, capture provenance, parse its output, and export an IEEE-formatted report without overstating the evidence. "
            "The current artifact uses a synthetic fallback and a single pipeline smoke test. Its contribution is therefore an auditable engineering integration and a concrete validation plan, not a validated visual-inertial algorithm or a performance comparison."
        ),
        "Related Work": (
            "Visual-inertial research spans geometric estimation, learned sensor fusion, uncertainty modeling, and benchmark design. "
            "The automated literature stage in this run did not produce a verified bibliography, so this engineering report intentionally avoids attributing detailed performance statements to unverified references. "
            "A submission-grade revision must repeat literature retrieval against authoritative indexes, verify each citation, and connect every technical comparison to a resolvable source."
        ),
        "Experimental Setup": (
            "The evaluation was limited to an engineering smoke test of the generated code and artifact pipeline. "
            f"Execution provenance records the mode as `{execution_mode}` and the implementation as `{implementation}`. "
            "The available run does not contain a validated benchmark protocol with multiple independent seeds, paired baseline executions, confidence intervals, or significance tests. "
            "Consequently, this setup is suitable for checking software execution and result ingestion only."
        ),
        "Experiments": (
            "The evaluation was limited to an engineering smoke test of the generated code and artifact pipeline. "
            f"Execution provenance records the mode as `{execution_mode}` and the implementation as `{implementation}`. "
            "No benchmark-quality comparison was completed. Scientific evaluation requires real IMU datasets, controlled train-test separation, repeated seeds, competitive baselines, and prespecified statistical analysis."
        ),
        "Results": (
            f"The principal verified outcome is that the generated program {'completed with a successful process return status' if executed else 'did not provide a confirmed successful execution record'}. "
            "Artifacts were produced and parsed by the pipeline. The single smoke output is not interpreted as a trajectory-error estimate or a comparative performance result. "
            "No claim of improved accuracy, robustness, generalization, ablation benefit, or statistical significance is supported by this run."
        ),
        "Discussion": (
            "This run supports an engineering conclusion: the automated workflow can connect code generation, execution provenance, analysis, and manuscript export. "
            "It does not support a scientific conclusion about the proposed visual-inertial method. A future evaluation must replace the synthetic fallback with the intended implementation and execute matched baselines on real datasets under identical protocols."
        ),
        "Limitations": (
            "The current evidence is limited to one successful execution of a synthetic fallback. There are no verified real-dataset runs, repeated random seeds, paired baseline results, confidence intervals, significance tests, or externally verified citations. "
            "The conceptual method description has not been validated as a faithful implementation, and the available output cannot establish visual-inertial accuracy, robustness, generalization, computational efficiency, or comparative benefit. "
            "These limitations must be resolved before the artifact is treated as a scientific paper rather than an engineering pipeline report."
        ),
        "Conclusion": (
            "We demonstrated an evidence-bounded engineering workflow for visual-inertial research automation. "
            "The code path executed and produced traceable artifacts, while the integrity gate correctly limits the outcome to a smoke-test report. "
            "Scientific performance conclusions remain deferred until benchmark-quality experiments with repeated trials, baselines, and statistical analysis are completed."
        ),
    }
    for heading, body in replacements.items():
        pattern = re.compile(
            rf"(^##\s*(?:\d+\.?\s*)?{re.escape(heading)}\s*$).*?(?=^##\s|\Z)",
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if pattern.search(paper):
            paper = pattern.sub(lambda match, text=body: f"{match.group(1)}\n\n{text}\n\n", paper, count=1)

    if not re.search(r"(?im)^##\s*(?:\d+\.?\s*)?Limitations\s*$", paper):
        limitations = f"## Limitations\n\n{replacements['Limitations']}\n\n"
        conclusion = re.search(r"(?im)^##\s*(?:\d+\.?\s*)?Conclusion\s*$", paper)
        insert_at = conclusion.start() if conclusion else len(paper)
        paper = paper[:insert_at] + limitations + paper[insert_at:]

    # Avoid global-superiority wording in background/method prose.
    paper = re.sub(r"state[- ]of[- ]the[- ]art", "representative", paper, flags=re.IGNORECASE)
    # Unverified citation placeholders and image-generation instructions are
    # internal scaffolding and must never leak into a user-facing manuscript.
    paper = re.sub(r"\s*\[cite_key:[^\]]+\]", "", paper, flags=re.IGNORECASE)
    paper = re.sub(r"<!--\s*FIGURE_PROMPT\b.*?-->", "", paper, flags=re.IGNORECASE | re.DOTALL)
    paper = re.sub(
        r"(?im)^\s*\*?\[Figure[^\n]*?to be generated\]\*?\s*$",
        "",
        paper,
    )
    paper = re.sub(r"\n{3,}", "\n\n", paper)
    return paper, True


def _remove_missing_markdown_figures(paper: str, run_dir: Path) -> tuple[str, list[str]]:
    """Drop image references that cannot resolve to any generated artifact."""
    missing: list[str] = []
    image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

    def _replace(match: re.Match[str]) -> str:
        target = match.group(2).strip().split()[0]
        relative = Path(target)
        candidates = [run_dir / relative]
        candidates.extend(stage_dir / relative for stage_dir in active_stage_glob(run_dir, "stage-*") if stage_dir.is_dir())
        candidates.extend(stage_dir / "charts" / relative.name for stage_dir in active_stage_glob(run_dir, "stage-*") if stage_dir.is_dir())
        if any(candidate.is_file() for candidate in candidates):
            return match.group(0)
        missing.append(target)
        return f"*Figure omitted because the generated artifact `{target}` was unavailable.*"

    return image_pattern.sub(_replace, paper), missing


def _repair_markdown_figure_references(paper: str) -> str:
    """Replace stale hard-coded figure numbers with stable LaTeX references."""
    number_to_label: dict[str, str] = {}
    pattern = re.compile(
        r"!\[([^\]]+)\]\([^)]+\)\s*\n\s*\*Figure\s+(\d+)\s*:",
        re.IGNORECASE,
    )
    for match in pattern.finditer(paper):
        caption = match.group(1)
        label_key = re.sub(r"[^a-z0-9]+", "_", caption.lower()).strip("_")[:30]
        if label_key:
            number_to_label[match.group(2)] = f"fig:{label_key}"
    if not number_to_label:
        return paper

    repaired: list[str] = []
    for line in paper.splitlines():
        if re.match(r"^\s*\*Figure\s+\d+\s*:", line, re.IGNORECASE):
            repaired.append(line)
            continue
        for number, label in number_to_label.items():
            line = re.sub(
                rf"\bFigure\s+{re.escape(number)}\b",
                lambda _match, target=label: rf"Figure \ref{{{target}}}",
                line,
            )
        repaired.append(line)
    return "\n".join(repaired) + ("\n" if paper.endswith("\n") else "")
