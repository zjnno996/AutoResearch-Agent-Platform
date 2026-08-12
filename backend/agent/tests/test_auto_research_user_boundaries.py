from __future__ import annotations

import json
import subprocess

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.pipeline.codegen.strategies.fallback import FallbackStrategy
from researchclaw.pipeline.codegen.types import CodegenContext
from researchclaw.pipeline.executor import (
    _build_claim_integrity_report,
    _build_context_preamble,
    _build_research_readiness,
    _execute_quality_gate,
    _parse_yaml_dict_from_llm,
)


class _CodegenSession:
    def log(self, *_args: object) -> None:
        pass


def test_stage9_yaml_parser_recovers_mapping_from_long_qwen_output() -> None:
    text = """
我先解释一下设计思路。

```text
this is not yaml
```

下面才是最终实验计划：

```yaml
objectives:
  - Validate latency-aware KV cache scheduling.
datasets:
  - ShareGPT-trace
baselines:
  - vLLM
proposed_methods:
  - name: QwenCacheRoute
metrics:
  - tokens_per_second
risks:
  - external trace may be unavailable
compute_budget:
  max_gpu: 1
```

补充说明不应该影响解析。
"""

    parsed = _parse_yaml_dict_from_llm(text)

    assert parsed is not None
    assert parsed["datasets"] == ["ShareGPT-trace"]
    assert parsed["baselines"] == ["vLLM"]
    assert parsed["compute_budget"]["max_gpu"] == 1


def test_stage9_yaml_parser_recovers_unfenced_mapping_inside_prose() -> None:
    text = """
Here is the plan.

objectives:
  - Evaluate the generated idea.
datasets:
  - local_dataset
baselines:
  - baseline_a
metrics:
  - primary_metric

## Explanation
The rest is prose.
"""

    parsed = _parse_yaml_dict_from_llm(text)

    assert parsed is not None
    assert parsed["objectives"] == ["Evaluate the generated idea."]
    assert parsed["datasets"] == ["local_dataset"]


def test_fallback_prefers_real_sklearn_builtin_for_simple_classification(tmp_path) -> None:
    cfg = RCConfig.from_dict(
        {
            "project": {"name": "real-simple", "mode": "full-auto"},
            "research": {"topic": "simple sklearn iris classification benchmark"},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "local"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline",
            },
            "experiment": {"metric_key": "accuracy_mean", "metric_direction": "maximize"},
        },
        project_root=tmp_path,
        check_paths=False,
    )
    ctx = CodegenContext(
        topic="simple sklearn iris classification benchmark",
        exp_plan="datasets: [iris, wine]\nmetrics: [accuracy_mean]\n",
        metric="accuracy_mean",
        metric_direction="maximize",
        time_budget_sec=60,
        mode="sandbox",
    )

    result = FallbackStrategy().generate(ctx, cfg, llm=None, session=_CodegenSession())

    assert result.strategy_name == "fallback_sklearn_builtin"
    assert "load_iris" in result.files["main.py"]
    assert '"implementation": "sklearn_builtin_real_dataset"' in result.files["experiment_metadata.json"]

    exp_dir = tmp_path / "exp"
    exp_dir.mkdir()
    for name, content in result.files.items():
        (exp_dir / name).write_text(content, encoding="utf-8")
    completed = subprocess.run(
        ["/opt/conda/envs/clawailab/bin/python", "main.py"],
        cwd=exp_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert (exp_dir / "results.json").exists()


def _write_readiness_inputs(
    run_dir,
    *,
    provenance: dict,
    diagnostics: dict | None = None,
    total_runs: int = 3,
) -> None:
    for stage in ("stage-09", "stage-16"):
        (run_dir / stage).mkdir(parents=True, exist_ok=True)
    (run_dir / "stage-09" / "exp_plan.yaml").write_text(
        "datasets: [iris, wine]\nbaselines: [logistic_regression, random_forest]\n",
        encoding="utf-8",
    )
    (run_dir / "stage-09" / "exp_plan_diagnostics.json").write_text(
        json.dumps(diagnostics or {"status": "normal", "degraded": False, "parse_strategy": "qwen_yaml"}),
        encoding="utf-8",
    )
    (run_dir / "stage-16" / "experiment_provenance.json").write_text(
        json.dumps(provenance), encoding="utf-8",
    )
    (run_dir / "stage-16" / "experiment_summary.json").write_text(
        json.dumps({
            "total_runs": total_runs,
            "metrics_summary": {"accuracy_mean": {"mean": 0.95, "count": total_runs}},
            "condition_summaries": {"a": {}, "b": {}, "c": {}},
        }),
        encoding="utf-8",
    )
    (run_dir / "stage-16" / "analysis.md").write_text("# Analysis\n真实结果分析。", encoding="utf-8")


def test_research_readiness_marks_smoke_as_engineering_report_only(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_readiness_inputs(run_dir, provenance={
        "executed": True,
        "real_code_execution": True,
        "scientific_claims_allowed": False,
        "claim_status": "smoke_only",
        "experiment_scope": "pipeline_smoke_test",
        "command": "python main.py",
        "returncode": 0,
        "implementation": "synthetic_fallback",
        "execution_mode": "direct_run",
    })

    readiness = _build_research_readiness(run_dir, decision="proceed")

    assert readiness["readiness_level"] == "engineering_smoke_only"
    assert readiness["writing_policy"] == "engineering_report_only"
    assert readiness["scientific_claims_allowed"] is False
    assert readiness["should_proceed_to_writing"] is True


def test_research_readiness_limits_small_real_benchmark_claims(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_readiness_inputs(run_dir, provenance={
        "executed": True,
        "real_code_execution": True,
        "scientific_claims_allowed": True,
        "claim_status": "limited_small_benchmark",
        "experiment_scope": "lightweight_real_benchmark",
        "command": "python main.py",
        "returncode": 0,
        "implementation": "sklearn_builtin_real_dataset",
        "execution_mode": "direct_run",
    }, total_runs=6)
    (run_dir / "stage-17").mkdir(parents=True)
    readiness = _build_research_readiness(run_dir, decision="proceed")
    (run_dir / "stage-17" / "research_readiness.json").write_text(
        json.dumps(readiness), encoding="utf-8",
    )
    rc_config = RCConfig.from_dict(
        {
            "project": {"name": "readiness-test", "mode": "full-auto"},
            "research": {"topic": "simple classification benchmark"},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "local"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline",
            },
            "experiment": {},
        },
        project_root=tmp_path,
        check_paths=False,
    )

    preamble = _build_context_preamble(
        rc_config, run_dir, include_analysis=True, include_experiment_data=True,
    )

    assert readiness["readiness_level"] == "limited_evidence"
    assert readiness["readiness_score"] <= 74
    assert readiness["writing_policy"] == "limited_claims_only"
    assert readiness["limited_claims_allowed"] is True
    assert readiness["scientific_claims_allowed"] is False
    assert readiness["evidence"]["min_seeds_per_condition"] == 0
    assert any("随机种子" in action for action in readiness["recommended_actions"])
    assert "NON-NEGOTIABLE EXPERIMENT CLAIM BOUNDARY" in preamble
    assert "limited_claims_only" in preamble


def test_claim_integrity_blocks_unsupported_overclaim_and_missing_limits(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_readiness_inputs(run_dir, provenance={
        "executed": True,
        "real_code_execution": True,
        "scientific_claims_allowed": True,
        "claim_status": "limited_small_benchmark",
        "experiment_scope": "lightweight_real_benchmark",
    })
    (run_dir / "stage-17").mkdir(parents=True)
    readiness = _build_research_readiness(run_dir, decision="proceed")
    (run_dir / "stage-17" / "research_readiness.json").write_text(
        json.dumps(readiness), encoding="utf-8",
    )
    paper = """# Paper

## Results
Our method reaches 99.9% accuracy and is state-of-the-art across all domains.

## Conclusion
The method is ready for deployment.
"""

    report = _build_claim_integrity_report(run_dir, paper)

    assert report["status"] == "blocked"
    assert report["has_limitations_section"] is False
    assert report["unsupported_numeric_claims"][0]["value"] == "99.9%"
    violation_types = {item["type"] for item in report["violations"]}
    assert "unsupported_numeric_claims" in violation_types
    assert "overgeneralized_claims" in violation_types
    assert "missing_limitations" in violation_types


def test_claim_integrity_accepts_scoped_supported_limited_result(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_readiness_inputs(run_dir, provenance={
        "executed": True,
        "real_code_execution": True,
        "scientific_claims_allowed": True,
        "claim_status": "limited_small_benchmark",
        "experiment_scope": "lightweight_real_benchmark",
    })
    (run_dir / "stage-17").mkdir(parents=True)
    readiness = _build_research_readiness(run_dir, decision="proceed")
    (run_dir / "stage-17" / "research_readiness.json").write_text(
        json.dumps(readiness), encoding="utf-8",
    )
    paper = """# Paper

## Results
On the tested Iris/Wine conditions, mean accuracy was 95%.

## Limitations
Each condition used one seed; no paired significance test was conducted.
"""

    report = _build_claim_integrity_report(run_dir, paper)

    assert report["status"] == "passed"
    assert report["unsupported_numeric_claims"] == []
    assert report["has_limitations_section"] is True


def test_claim_integrity_does_not_treat_ci_level_as_performance(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_readiness_inputs(run_dir, provenance={
        "executed": True,
        "real_code_execution": True,
        "scientific_claims_allowed": True,
        "claim_status": "limited_small_benchmark",
        "experiment_scope": "lightweight_real_benchmark",
    })
    (run_dir / "stage-17").mkdir(parents=True)
    readiness = _build_research_readiness(run_dir, decision="proceed")
    (run_dir / "stage-17" / "research_readiness.json").write_text(
        json.dumps(readiness), encoding="utf-8",
    )
    paper = """# Paper

## Results
| Accuracy Mean | Accuracy 95% CI |
|---:|---:|
| 0.9500 | [0.9400, 0.9600] |

## Limitations
The interval is limited to the executed conditions.
"""

    report = _build_claim_integrity_report(run_dir, paper)

    assert all(item["value"] != "95%" for item in report["unsupported_numeric_claims"])


def test_claim_integrity_forbids_performance_claim_for_smoke_only(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_readiness_inputs(run_dir, provenance={
        "executed": True,
        "real_code_execution": True,
        "scientific_claims_allowed": False,
        "claim_status": "smoke_only",
        "experiment_scope": "pipeline_smoke_test",
    })
    (run_dir / "stage-17").mkdir(parents=True)
    readiness = _build_research_readiness(run_dir, decision="proceed")
    (run_dir / "stage-17" / "research_readiness.json").write_text(
        json.dumps(readiness), encoding="utf-8",
    )

    report = _build_claim_integrity_report(
        run_dir,
        "## Results\nAccuracy improves to 95%.\n\n## Limitations\nSmoke test only.",
    )

    assert report["prohibited_empirical_claims"] is True
    assert report["status"] == "blocked"


def test_quality_gate_emits_and_enforces_claim_integrity_report(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_readiness_inputs(run_dir, provenance={
        "executed": True,
        "real_code_execution": True,
        "scientific_claims_allowed": True,
        "claim_status": "limited_small_benchmark",
        "experiment_scope": "lightweight_real_benchmark",
    })
    (run_dir / "stage-17").mkdir(parents=True)
    readiness = _build_research_readiness(run_dir, decision="proceed")
    (run_dir / "stage-17" / "research_readiness.json").write_text(
        json.dumps(readiness), encoding="utf-8",
    )
    (run_dir / "stage-22").mkdir(parents=True)
    (run_dir / "stage-22" / "paper_revised.md").write_text(
        "## Results\nAccuracy reaches 99.9%.\n\n## Conclusion\nReady.",
        encoding="utf-8",
    )
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir()
    config = RCConfig.from_dict(
        {
            "project": {"name": "claim-gate", "mode": "full-auto"},
            "research": {"topic": "classification", "quality_threshold": 8.0},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "local"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline",
            },
            "experiment": {},
        },
        project_root=tmp_path,
        check_paths=False,
    )

    result = _execute_quality_gate(
        stage_dir, run_dir, config, AdapterBundle(), llm=None,
    )

    assert "claim_integrity_report.json" in result.artifacts
    integrity = json.loads((stage_dir / "claim_integrity_report.json").read_text())
    quality = json.loads((stage_dir / "quality_report.json").read_text())
    assert integrity["status"] == "blocked"
    assert quality["claim_integrity_status"] == "blocked"
    assert quality["score_1_to_10"] <= 4.0
