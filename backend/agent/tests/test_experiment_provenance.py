from __future__ import annotations

import json

from researchclaw.pipeline.experiment_run.runtime import ExperimentRunRuntime
from researchclaw.pipeline.result_analysis.runtime import ResultAnalysisRuntime


class _Session:
    def log(self, *_args: object) -> None:
        pass


def test_synthetic_fallback_is_explicitly_smoke_only(tmp_path):
    (tmp_path / "main.py").write_text(
        "# Fallback experiment: parameter sweep on a synthetic objective\n",
        encoding="utf-8",
    )

    provenance = ExperimentRunRuntime._build_experiment_provenance(
        tmp_path,
        {
            "success": True,
            "command": "python main.py",
            "returncode": 0,
            "results": {"primary_metric": 0.5, "source": "stdout"},
        },
        execution_mode="direct_run",
    )

    assert provenance["executed"] is True
    assert provenance["experiment_scope"] == "pipeline_smoke_test"
    assert provenance["implementation"] == "synthetic_fallback"
    assert provenance["scientific_claims_allowed"] is False
    assert provenance["claim_status"] == "smoke_only"


def test_sklearn_builtin_real_dataset_has_limited_claim_scope(tmp_path):
    (tmp_path / "main.py").write_text(
        "# Lightweight real experiment: sklearn built-in real datasets.\n",
        encoding="utf-8",
    )
    (tmp_path / "experiment_metadata.json").write_text(
        json.dumps({
            "implementation": "sklearn_builtin_real_dataset",
            "experiment_scope": "lightweight_real_benchmark",
            "scientific_claims_allowed": True,
            "claim_status": "limited_small_benchmark",
        }),
        encoding="utf-8",
    )

    provenance = ExperimentRunRuntime._build_experiment_provenance(
        tmp_path,
        {
            "success": True,
            "command": "python main.py",
            "returncode": 0,
            "results": {"primary_metric": 0.95, "source": "results_file"},
        },
        execution_mode="direct_run",
    )

    assert provenance["executed"] is True
    assert provenance["experiment_scope"] == "lightweight_real_benchmark"
    assert provenance["implementation"] == "sklearn_builtin_real_dataset"
    assert provenance["scientific_claims_allowed"] is True
    assert provenance["claim_status"] == "limited_small_benchmark"


def test_analysis_uses_raw_results_not_administrative_numbers(tmp_path):
    workspace = tmp_path / "workspace"
    stage_dir = tmp_path / "stage-16"
    workspace.mkdir()
    stage_dir.mkdir()
    provenance = {
        "executed": True,
        "experiment_scope": "pipeline_smoke_test",
        "scientific_claims_allowed": False,
        "display_status_zh": "已真实执行基础 Smoke；仅验证流程，不支持科研性能结论",
    }
    (workspace / "results.json").write_text(
        json.dumps({"primary_metric": 0.25}),
        encoding="utf-8",
    )
    (workspace / "run_report.json").write_text(
        json.dumps({"elapsed_sec": 99, "returncode": 0}),
        encoding="utf-8",
    )
    (workspace / "experiment_summary.json").write_text(
        json.dumps({"total_runs": 500}),
        encoding="utf-8",
    )
    (workspace / "experiment_provenance.json").write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )

    result = ResultAnalysisRuntime._write_deterministic_analysis(
        workspace=workspace,
        stage_dir=stage_dir,
        topic="smoke",
        metric_key="primary_metric",
        metric_direction="maximize",
        data_files=[
            "results.json",
            "run_report.json",
            "experiment_summary.json",
            "experiment_provenance.json",
        ],
        session=_Session(),
    )

    assert result["success"] is True
    summary = result["summary"]
    assert summary["total_runs"] == 1
    assert summary["metrics_summary"] == {
        "primary_metric": {
            "min": 0.25,
            "max": 0.25,
            "mean": 0.25,
            "count": 1,
        }
    }
    assert summary["scientific_claims_allowed"] is False


def test_analysis_expands_named_condition_metrics(tmp_path):
    workspace = tmp_path / "workspace"
    stage_dir = tmp_path / "stage-16"
    workspace.mkdir()
    stage_dir.mkdir()
    provenance = {
        "executed": True,
        "experiment_scope": "lightweight_real_benchmark",
        "scientific_claims_allowed": True,
        "display_status_zh": "已真实执行轻量真实基准",
    }
    (workspace / "results.json").write_text(
        json.dumps({
            "conditions": {
                "iris__logistic_regression": {
                    "dataset": "iris",
                    "model": "logistic_regression",
                    "accuracy_mean": 0.95,
                    "f1_macro_mean": 0.94,
                    "folds": 15,
                },
                "iris__random_forest": {
                    "dataset": "iris",
                    "model": "random_forest",
                    "accuracy_mean": 0.97,
                    "f1_macro_mean": 0.96,
                    "folds": 15,
                },
            },
            "accuracy_mean": 0.97,
            "primary_metric": 0.97,
        }),
        encoding="utf-8",
    )
    (workspace / "experiment_provenance.json").write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )

    result = ResultAnalysisRuntime._write_deterministic_analysis(
        workspace=workspace,
        stage_dir=stage_dir,
        topic="condition expansion",
        metric_key="accuracy_mean",
        metric_direction="maximize",
        data_files=["results.json", "experiment_provenance.json"],
        session=_Session(),
    )

    assert result["success"] is True
    summary = result["summary"]
    assert summary["total_runs"] == 2
    assert summary["best_run"]["condition"] == "iris__random_forest"
    assert summary["condition_summaries"]["iris__random_forest"]["model"] == "random_forest"
    assert summary["metrics_summary"]["accuracy_mean"]["count"] == 2
