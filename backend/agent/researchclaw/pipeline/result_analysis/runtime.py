"""S16 RESULT_ANALYSIS runtime — agentic analysis via claw-engine turn loop.

The agent reads raw experiment output files, writes analysis scripts,
runs them, and produces experiment_summary.json + analysis.md.
This adapts to any data format the experiment happened to produce.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import statistics
import time
from pathlib import Path
from typing import Any

from researchclaw.config import RCConfig
from researchclaw.adapters import AdapterBundle
from researchclaw.pipeline.claw_engine import AgentTurnLoop, StageSession
from researchclaw.pipeline.result_analysis.system_prompt import (
    build_system_prompt,
    build_user_message,
)
from researchclaw.pipeline.stages import Stage, StageStatus

logger = logging.getLogger(__name__)

_COLLECT_EXTENSIONS = frozenset({
    ".json", ".csv", ".tsv", ".txt", ".yaml", ".yml", ".log", ".md",
})

_SKIP_DIRS = frozenset({
    "__pycache__", ".git", "codebases", "datasets", "checkpoints",
})


class ResultAnalysisRuntime:
    """Orchestration for S16 RESULT_ANALYSIS using claw-engine turn loop."""

    def execute(
        self,
        stage_dir: Path,
        run_dir: Path,
        config: RCConfig,
        adapters: AdapterBundle,
        *,
        llm: Any | None = None,
    ) -> Any:
        from researchclaw.pipeline.executor import StageResult

        stage_dir.mkdir(parents=True, exist_ok=True)
        session = StageSession(stage_dir=stage_dir, stage_name="result_analysis")
        session.log("INIT", "ResultAnalysisRuntime started")

        llm = self._resolve_coding_llm(llm, config)
        if llm is None:
            session.log_error("INIT", "No LLM client available")
            return StageResult(
                stage=Stage.RESULT_ANALYSIS,
                status=StageStatus.FAILED,
                artifacts=(),
                error="No LLM client available for agentic result analysis",
            )

        llm_config = llm.config
        session.log("INIT", f"LLM: {llm_config.primary_model}")

        python_path = getattr(config.experiment.sandbox, "python_path", "") or ""

        workspace = self._prepare_workspace(stage_dir, run_dir, config)
        session.log("INIT", f"Workspace: {workspace}")

        data_files = self._list_data_files(workspace)
        session.log("INIT", f"Found {len(data_files)} data files")

        topic = getattr(config.research, "topic", "") or ""
        metric_key = getattr(config.experiment, "metric_key", "primary_metric")
        metric_direction = getattr(config.experiment, "metric_direction", "minimize")

        deterministic = self._write_deterministic_analysis(
            workspace=workspace,
            stage_dir=stage_dir,
            topic=topic,
            metric_key=metric_key,
            metric_direction=metric_direction,
            data_files=data_files,
            session=session,
        )
        if deterministic["success"]:
            artifacts = [
                "analysis.md", "experiment_summary.json",
                "experiment_provenance.json",
            ]
            session.log(
                "RESULT",
                "Deterministic result analysis SUCCEEDED; skipping agentic LLM analysis loop.",
            )
            return StageResult(
                stage=Stage.RESULT_ANALYSIS,
                status=StageStatus.DONE,
                artifacts=tuple(artifacts),
                evidence_refs=tuple(f"stage-16/{a}" for a in artifacts),
            )

        session.log(
            "EXECUTE",
            "Deterministic result analysis did not find usable metrics; entering agentic Qwen analysis loop.",
        )

        system_prompt = build_system_prompt(
            python_path=python_path,
            workspace_path=str(workspace),
        )

        user_message = build_user_message(
            workspace_path=str(workspace),
            data_files=data_files,
            metric_key=metric_key,
            metric_direction=metric_direction,
            topic=topic,
        )

        (stage_dir / "result_analysis_system_prompt.md").write_text(
            system_prompt, encoding="utf-8",
        )

        allowed_reads = self._build_allowed_reads(config, run_dir)

        loop = AgentTurnLoop(
            llm_config=llm_config,
            workspace=workspace,
            system_prompt=system_prompt,
            session=session,
            allowed_read_dirs=allowed_reads,
            bash_timeout=300,
            max_iterations=25,
            python_path=python_path,
            trace_prefix="result_analysis",
        )

        session.log("EXECUTE", "Starting result analysis turn loop...")
        turn_result = loop.run_turn(user_message)

        summary_path = workspace / "experiment_summary.json"
        analysis_path = workspace / "analysis.md"

        has_summary = summary_path.is_file()
        has_analysis = analysis_path.is_file()

        if has_summary:
            try:
                self._attach_provenance_to_summary(summary_path, workspace)
                shutil.copy2(summary_path, stage_dir / "experiment_summary.json")
                session.log("RESULT", "Copied experiment_summary.json to stage_dir")
            except OSError as exc:
                session.log_error("RESULT", f"Failed to copy summary: {exc}")

        if has_analysis:
            try:
                shutil.copy2(analysis_path, stage_dir / "analysis.md")
                session.log("RESULT", "Copied analysis.md to stage_dir")
            except OSError as exc:
                session.log_error("RESULT", f"Failed to copy analysis: {exc}")

        success = has_summary and has_analysis and not turn_result.errors
        session.log(
            "RESULT",
            f"Result analysis {'SUCCEEDED' if success else 'FAILED'}: "
            f"{turn_result.iterations} iters, {turn_result.tool_calls} tool calls, "
            f"summary={'yes' if has_summary else 'no'}, "
            f"analysis={'yes' if has_analysis else 'no'}, "
            f"{turn_result.elapsed_sec:.1f}s",
        )

        artifacts = []
        if has_analysis:
            artifacts.append("analysis.md")
        if has_summary:
            artifacts.append("experiment_summary.json")
        provenance = self._load_experiment_provenance(workspace)
        if provenance:
            (stage_dir / "experiment_provenance.json").write_text(
                json.dumps(provenance, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            artifacts.append("experiment_provenance.json")

        # Copy charts if the agent generated any
        ws_charts = workspace / "charts"
        if ws_charts.is_dir() and any(ws_charts.iterdir()):
            stage_charts = stage_dir / "charts"
            try:
                if stage_charts.exists():
                    shutil.rmtree(stage_charts)
                shutil.copytree(ws_charts, stage_charts, dirs_exist_ok=True)
                artifacts.append("charts/")
                session.log("RESULT", "Copied charts/ to stage_dir")
            except OSError:
                pass

        if not success:
            return StageResult(
                stage=Stage.RESULT_ANALYSIS,
                status=StageStatus.FAILED,
                artifacts=tuple(artifacts),
                error="Agentic analysis did not produce required outputs",
                decision="retry",
            )

        return StageResult(
            stage=Stage.RESULT_ANALYSIS,
            status=StageStatus.DONE,
            artifacts=tuple(artifacts),
            evidence_refs=tuple(f"stage-16/{a}" for a in artifacts),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_deterministic_analysis(
        workspace: Path,
        stage_dir: Path,
        topic: str,
        metric_key: str,
        metric_direction: str,
        data_files: list[str],
        session: StageSession,
    ) -> dict[str, Any]:
        """Create minimal but real analysis artifacts without an LLM.

        This path handles straightforward result formats and prevents transient
        model gateway failures from blocking later writing stages.
        """
        metric_rows: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        seen_payloads: set[str] = set()
        administrative_files = {
            "run_report.json", "experiment_provenance.json",
            "sanity_report.json", "refinement_log.json",
            "experiment_summary.json",
        }
        for rel in data_files:
            filename = Path(rel).name
            is_raw_result = (
                filename.startswith("results")
                or filename.startswith("metrics")
            )
            if (
                not rel.endswith(".json")
                or filename in administrative_files
                or not is_raw_result
            ):
                continue
            path = workspace / rel
            if str(path) in seen_paths:
                continue
            seen_paths.add(str(path))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            payload_fingerprint = json.dumps(data, sort_keys=True, ensure_ascii=False)
            if payload_fingerprint in seen_payloads:
                continue
            seen_payloads.add(payload_fingerprint)
            ResultAnalysisRuntime._collect_metric_rows(data, rel, metric_rows)

        numeric_values: dict[str, list[float]] = {}
        for row in metric_rows:
            if row.get("row_type") in {"paired_comparison", "metadata"}:
                continue
            metrics = row.get("metrics", {})
            if not isinstance(metrics, dict):
                continue
            for key, value in metrics.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric_values.setdefault(str(key), []).append(float(value))

        if not numeric_values:
            return {"success": False, "reason": "no_numeric_metrics"}

        metrics_summary = {
            key: {
                "min": min(values),
                "max": max(values),
                "mean": sum(values) / len(values),
                "count": len(values),
            }
            for key, values in sorted(numeric_values.items())
            if values
        }

        primary = metric_key if metric_key in numeric_values else (
            "primary_metric" if "primary_metric" in numeric_values else sorted(numeric_values)[0]
        )
        candidates = [
            row for row in metric_rows
            if row.get("row_type") not in {"paired_comparison", "metadata"}
            if isinstance(row.get("metrics"), dict)
            and isinstance(row["metrics"].get(primary), (int, float))
            and not isinstance(row["metrics"].get(primary), bool)
        ]
        reverse = str(metric_direction).lower() == "maximize"
        best_run = None
        if candidates:
            best = sorted(candidates, key=lambda item: float(item["metrics"][primary]), reverse=reverse)[0]
            best_run = {
                "condition": str(best.get("condition", best.get("source", "run"))),
                "source": str(best.get("source", "")),
                "metrics": best.get("metrics", {}),
            }

        condition_summaries: dict[str, Any] = {}
        for row in metric_rows:
            if row.get("row_type") in {"paired_comparison", "metadata"}:
                continue
            condition = str(row.get("condition", row.get("source", "run")))
            metrics = {
                k: v for k, v in (row.get("metrics", {}) or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            if not metrics:
                continue
            entry: dict[str, Any] = {
                "metrics": metrics,
                "n_seeds": int(metrics.get("n_seeds", 0) or 0),
            }
            seed_metrics = row.get("seed_metrics")
            if isinstance(seed_metrics, dict):
                entry["seed_metrics"] = seed_metrics
                entry["n_seeds"] = len(seed_metrics)
                seed_values = [
                    float(values[metric_key])
                    for values in seed_metrics.values()
                    if isinstance(values, dict)
                    and isinstance(values.get(metric_key), (int, float))
                    and not isinstance(values.get(metric_key), bool)
                ]
                if len(seed_values) >= 2:
                    mean_value = statistics.mean(seed_values)
                    sem = statistics.stdev(seed_values) / math.sqrt(len(seed_values))
                    # Two-sided 95% Student-t critical values for the small
                    # seed counts commonly used by the lightweight runner.
                    t_critical = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}.get(
                        len(seed_values), 1.96
                    )
                    margin = t_critical * sem
                    entry["metrics"][f"{metric_key}_ci95_low"] = mean_value - margin
                    entry["metrics"][f"{metric_key}_ci95_high"] = mean_value + margin
            if not entry["n_seeds"]:
                entry["n_seeds"] = 1
            for meta_key in ("dataset", "model", "method", "seed"):
                if meta_key in row:
                    entry[meta_key] = row[meta_key]
            condition_summaries[condition] = entry

        latex_cols = ["condition", primary]
        latex_rows = []
        for condition, info in list(condition_summaries.items())[:20]:
            value = info.get("metrics", {}).get(primary)
            if isinstance(value, (int, float)):
                latex_rows.append(f"{condition} & {float(value):.6f} \\\\")
        latex_table = ""
        if latex_rows:
            latex_table = (
                "\\begin{tabular}{lr}\n"
                f"{latex_cols[0]} & {latex_cols[1]} \\\\\n"
                "\\hline\n"
                + "\n".join(latex_rows)
                + "\n\\end{tabular}"
            )

        provenance = ResultAnalysisRuntime._load_experiment_provenance(workspace)
        paired_comparisons = [
            dict(row.get("comparison", {}))
            for row in metric_rows
            if row.get("row_type") == "paired_comparison"
            and isinstance(row.get("comparison"), dict)
        ]
        observed_seed_runs = sum(
            int(info.get("n_seeds", 0) or 0)
            for info in condition_summaries.values()
            if isinstance(info, dict)
        )
        summary = {
            "metrics_summary": metrics_summary,
            "total_runs": observed_seed_runs or sum(
                1 for row in metric_rows
                if row.get("row_type") not in {"paired_comparison", "metadata"}
            ),
            "condition_count": len(condition_summaries),
            "best_run": best_run,
            "condition_summaries": condition_summaries,
            "paired_comparisons": paired_comparisons,
            "latex_table": latex_table,
            "generated_by": "deterministic_result_analysis",
            "metric_key": primary,
            "metric_direction": metric_direction,
            "experiment_provenance": provenance,
            "experiment_scope": provenance.get(
                "experiment_scope", "unclassified_experiment",
            ),
            "scientific_claims_allowed": bool(
                provenance.get("scientific_claims_allowed", False),
            ),
        }
        analysis = ResultAnalysisRuntime._render_analysis_markdown(
            topic=topic,
            summary=summary,
            data_files=data_files,
        )

        (workspace / "experiment_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (workspace / "analysis.md").write_text(analysis, encoding="utf-8")
        (workspace / "experiment_provenance.json").write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        shutil.copy2(workspace / "experiment_summary.json", stage_dir / "experiment_summary.json")
        shutil.copy2(workspace / "analysis.md", stage_dir / "analysis.md")
        shutil.copy2(
            workspace / "experiment_provenance.json",
            stage_dir / "experiment_provenance.json",
        )
        session.log(
            "RESULT",
            f"Deterministic analysis wrote {len(metric_rows)} metric row(s), primary={primary}",
        )
        return {"success": True, "summary": summary}

    @staticmethod
    def _load_experiment_provenance(workspace: Path) -> dict[str, Any]:
        candidates = sorted(workspace.rglob("experiment_provenance.json"))
        for path in candidates:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(loaded, dict):
                return loaded
        return {
            "executed": False,
            "real_code_execution": False,
            "experiment_scope": "unclassified_experiment",
            "scientific_claims_allowed": False,
            "claim_status": "missing_execution_provenance",
            "display_status_zh": "缺少实验执行来源，不能用于科研性能结论",
        }

    @staticmethod
    def _attach_provenance_to_summary(summary_path: Path, workspace: Path) -> None:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(summary, dict):
            return
        provenance = ResultAnalysisRuntime._load_experiment_provenance(workspace)
        summary["experiment_provenance"] = provenance
        summary["experiment_scope"] = provenance.get(
            "experiment_scope", "unclassified_experiment",
        )
        summary["scientific_claims_allowed"] = bool(
            provenance.get("scientific_claims_allowed", False),
        )
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _collect_metric_rows(data: Any, source: str, rows: list[dict[str, Any]]) -> None:
        if isinstance(data, dict):
            if isinstance(data.get("results"), dict):
                ResultAnalysisRuntime._collect_metric_rows(data["results"], source, rows)
            collected_named_conditions = False
            if isinstance(data.get("conditions"), dict):
                for condition_name, condition_payload in data["conditions"].items():
                    if not isinstance(condition_payload, dict):
                        continue
                    sub_numeric = {
                        k: float(v)
                        for k, v in condition_payload.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)
                    }
                    summary_metrics: dict[str, float] = {}
                    condition_summary = condition_payload.get("summary", {})
                    if isinstance(condition_summary, dict):
                        for metric_name, metric_stats in condition_summary.items():
                            if not isinstance(metric_stats, dict):
                                continue
                            mean_value = metric_stats.get("mean")
                            if isinstance(mean_value, (int, float)) and not isinstance(mean_value, bool):
                                summary_metrics[str(metric_name)] = float(mean_value)
                            std_value = metric_stats.get("std")
                            if isinstance(std_value, (int, float)) and not isinstance(std_value, bool):
                                summary_metrics[f"{metric_name}_std"] = float(std_value)
                            ci_value = metric_stats.get("ci_95", metric_stats.get("ci95"))
                            if (
                                isinstance(ci_value, list)
                                and len(ci_value) >= 2
                                and all(isinstance(item, (int, float)) for item in ci_value[:2])
                            ):
                                summary_metrics[f"{metric_name}_ci95_low"] = float(ci_value[0])
                                summary_metrics[f"{metric_name}_ci95_high"] = float(ci_value[1])
                    sub_numeric.update(summary_metrics)
                    if sub_numeric:
                        row: dict[str, Any] = {
                            "source": source,
                            "condition": str(condition_name),
                            "metrics": sub_numeric,
                        }
                        for meta_key in ("dataset", "model", "method", "seed"):
                            if meta_key in condition_payload:
                                row[meta_key] = condition_payload[meta_key]
                        seed_metrics = condition_payload.get(
                            "seed_metrics", condition_payload.get("seeds")
                        )
                        if isinstance(seed_metrics, dict):
                            row["seed_metrics"] = seed_metrics
                        rows.append(row)
                        collected_named_conditions = True
            if isinstance(data.get("statistical_tests"), dict):
                for test_name, test_payload in data["statistical_tests"].items():
                    if not isinstance(test_payload, dict):
                        continue
                    comparison = dict(test_payload)
                    comparison.setdefault("test", str(test_name))
                    rows.append({
                        "source": source,
                        "condition": str(test_name),
                        "row_type": "paired_comparison",
                        "comparison": comparison,
                        "metrics": {
                            key: float(value)
                            for key, value in test_payload.items()
                            if isinstance(value, (int, float)) and not isinstance(value, bool)
                        },
                    })
            numeric = {
                k: float(v)
                for k, v in data.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            if numeric and not collected_named_conditions:
                condition = str(data.get("condition") or data.get("name") or Path(source).stem)
                rows.append({"source": source, "condition": condition, "metrics": numeric})
            for key, value in data.items():
                if key in {"conditions", "datasets", "statistical_tests"}:
                    continue
                if isinstance(value, dict):
                    sub_numeric = {
                        k: float(v)
                        for k, v in value.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)
                    }
                    if sub_numeric:
                        row = {"source": source, "condition": str(key), "metrics": sub_numeric}
                        if key in {"dataset", "metadata", "configuration", "config"}:
                            row["row_type"] = "metadata"
                        rows.append(row)
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, dict):
                            sub_numeric = {
                                k: float(v)
                                for k, v in item.items()
                                if isinstance(v, (int, float)) and not isinstance(v, bool)
                            }
                            if sub_numeric:
                                row = {
                                    "source": source,
                                    "condition": str(item.get("condition") or item.get("name") or f"{key}_{index}"),
                                    "metrics": sub_numeric,
                                }
                                if key == "paired_comparisons":
                                    row["row_type"] = "paired_comparison"
                                    row["comparison"] = dict(item)
                                rows.append(row)
        elif isinstance(data, list):
            for index, item in enumerate(data):
                if isinstance(item, dict):
                    ResultAnalysisRuntime._collect_metric_rows(
                        {f"row_{index}": item}, source, rows,
                    )

    @staticmethod
    def _render_analysis_markdown(
        topic: str,
        summary: dict[str, Any],
        data_files: list[str],
    ) -> str:
        primary = summary.get("metric_key", "primary_metric")
        best = summary.get("best_run") or {}
        metrics_summary = summary.get("metrics_summary", {})
        lines = [
            "# Experiment Result Analysis",
            "",
            "## Experiment Status",
            str(
                (summary.get("experiment_provenance") or {}).get(
                    "display_status_zh",
                    "缺少实验执行来源，不能用于科研性能结论",
                )
            ),
            "",
            "## Summary",
            f"Topic: {topic or 'N/A'}",
            f"Parsed {summary.get('condition_count', len(summary.get('condition_summaries', {})))} "
            f"experimental condition(s) and {summary.get('total_runs', 0)} per-seed run(s) "
            "from actual result files.",
            "",
            "## Methods",
            "The analysis was generated deterministically from pipeline artifacts without "
            "fabricating metrics. Per-condition seed observations, confidence intervals, and "
            "stored paired tests are reported when present in the executed result file.",
            "",
            "## Results",
        ]
        if best:
            value = (best.get("metrics") or {}).get(primary)
            lines.append(f"Best condition by `{primary}`: `{best.get('condition')}` with value `{value}`.")
        for key, stats in metrics_summary.items():
            lines.append(
                f"- `{key}`: mean={stats.get('mean'):.6f}, "
                f"min={stats.get('min'):.6f}, max={stats.get('max'):.6f}, "
                f"conditions={stats.get('count')}"
            )
        for condition, info in (summary.get("condition_summaries") or {}).items():
            if not isinstance(info, dict):
                continue
            metrics = info.get("metrics", {}) if isinstance(info.get("metrics"), dict) else {}
            primary_value = metrics.get(primary)
            if isinstance(primary_value, (int, float)):
                lines.append(
                    f"- `{condition}`: {primary}={float(primary_value):.6f}, "
                    f"seeds={int(info.get('n_seeds', 0) or 0)}, "
                    f"95% CI=[{metrics.get(f'{primary}_ci95_low')}, "
                    f"{metrics.get(f'{primary}_ci95_high')}]"
                )
        lines.extend([
            "",
            "## Statistical Analysis",
        ])
        paired = summary.get("paired_comparisons", [])
        if isinstance(paired, list) and paired:
            for comparison in paired:
                if not isinstance(comparison, dict):
                    continue
                lines.append(
                    f"- `{comparison.get('test', 'paired_test')}` for "
                    f"{comparison.get('comparison', 'the compared conditions')}: "
                    f"p={comparison.get('p_value')}."
                )
            lines.append(
                "With only three paired seeds, parametric and non-parametric conclusions may "
                "disagree; these tests are descriptive and do not support a broad significance claim."
            )
        else:
            lines.append("No paired statistical test was present in the executed result artifact.")
        lines.extend(["", "## Source Files"])
        lines.extend(f"- `{path}`" for path in data_files[:50])
        lines.extend([
            "",
            "## Conclusions",
            "The experiment produced parseable numeric metrics and can be consumed by downstream "
            "decision and writing stages. It must not be presented as scientific evidence unless "
            "the provenance explicitly permits scientific claims.",
            "",
        ])
        return "\n".join(lines)

    @staticmethod
    def _resolve_coding_llm(llm: Any, config: RCConfig) -> Any:
        coding_model = getattr(config.llm, "coding_model", "") or ""
        if not coding_model or not llm:
            return llm
        if hasattr(llm, "config") and llm.config.primary_model == coding_model:
            return llm
        try:
            from researchclaw.llm import create_llm_client
            import dataclasses
            new_llm_cfg = dataclasses.replace(config.llm, primary_model=coding_model)
            new_config = dataclasses.replace(config, llm=new_llm_cfg)
            return create_llm_client(new_config)
        except Exception:
            return llm

    @staticmethod
    def _prepare_workspace(
        stage_dir: Path, run_dir: Path, config: RCConfig,
    ) -> Path:
        ws = stage_dir / f"analysis_workspace_{int(time.time())}_{os.getpid()}"
        ws.mkdir(parents=True, exist_ok=True)

        # Only canonical outputs from completed upstream stages are valid
        # inputs.  Versioned stage directories are retained attempt history,
        # while the current analysis stage may itself contain a previous
        # workspace.  Scanning ``stage-*`` used to recursively copy both and
        # could mix stale synthetic metrics into a fresh real-data run.
        stage_match = re.fullmatch(r"stage-(\d+)(?:_v\d+)?", stage_dir.name)
        current_stage = int(stage_match.group(1)) if stage_match else 16
        upstream_stages: list[Path] = []
        for candidate in run_dir.iterdir():
            match = re.fullmatch(r"stage-(\d+)", candidate.name)
            if candidate.is_dir() and match and int(match.group(1)) < current_stage:
                upstream_stages.append(candidate)
        upstream_stages.sort(
            key=lambda path: int(path.name.removeprefix("stage-")), reverse=True,
        )

        # Copy result data from prior stages into the workspace
        for source_name in ("runs", "experiment_final"):
            for stage_d in upstream_stages:
                src = stage_d / source_name
                if src.is_dir():
                    dst = ws / source_name
                    try:
                        shutil.copytree(
                            src, dst,
                            ignore=shutil.ignore_patterns(
                                "__pycache__", "*.pyc", ".git",
                            ),
                            dirs_exist_ok=True,
                        )
                    except OSError:
                        pass
                    break

        # Copy key standalone result files
        _RESULT_FILES = (
            "results.json", "results_v0.json", "refinement_log.json",
            "experiment_summary.json", "sanity_report.json",
        )
        for fname in _RESULT_FILES:
            for stage_d in upstream_stages:
                src = stage_d / fname
                if src.is_file():
                    dst = ws / fname
                    if not dst.exists():
                        try:
                            shutil.copy2(src, dst)
                        except OSError:
                            pass
                    break
                # Also check one level deeper (e.g. runs/results.json, experiment_final/results_v0.json)
                copied_nested = False
                for sub in stage_d.iterdir():
                    if sub.is_dir():
                        nested = sub / fname
                        if nested.is_file():
                            nested_dst = ws / sub.name / fname
                            nested_dst.parent.mkdir(parents=True, exist_ok=True)
                            if not nested_dst.exists():
                                try:
                                    shutil.copy2(nested, nested_dst)
                                    copied_nested = True
                                except OSError:
                                    pass
                if copied_nested:
                    break

        # Copy experiment plan for context
        for plan_name in ("exp_plan.yaml", "EXPERIMENT_PLAN.yaml"):
            for stage_d in upstream_stages:
                src = stage_d / plan_name
                if src.is_file():
                    try:
                        shutil.copy2(src, ws / plan_name)
                    except OSError:
                        pass
                    break

        # Copy analysis.md if it already exists from prior S16 run
        for stage_d in upstream_stages:
            src = stage_d / "analysis.md"
            if src.is_file():
                try:
                    shutil.copy2(src, ws / "prior_analysis.md")
                except OSError:
                    pass
                break

        (ws / "charts").mkdir(exist_ok=True)
        return ws

    @staticmethod
    def _list_data_files(workspace: Path) -> list[str]:
        files: list[str] = []
        for fpath in sorted(workspace.rglob("*")):
            if not fpath.is_file() or fpath.is_symlink():
                continue
            rel = fpath.relative_to(workspace)
            if any(p.startswith(".") or p in _SKIP_DIRS for p in rel.parts):
                continue
            if fpath.suffix.lower() not in _COLLECT_EXTENSIONS:
                continue
            if fpath.stat().st_size > 5 * 1024 * 1024:
                continue
            files.append(str(rel))
        return files

    @staticmethod
    def _build_allowed_reads(config: RCConfig, run_dir: Path) -> list[Path]:
        dirs: list[Path] = [run_dir]
        for attr in ("datasets_dir", "checkpoints_dir", "codebases_dir"):
            d = getattr(config.experiment, attr, "") or ""
            if d and Path(d).is_dir():
                dirs.append(Path(d))
        return dirs
