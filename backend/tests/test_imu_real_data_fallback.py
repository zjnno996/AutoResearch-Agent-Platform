from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from researchclaw.agents.benchmark_agent.surveyor import SurveyorAgent  # noqa: E402
from researchclaw.pipeline.codegen.session import CodegenSession  # noqa: E402
from researchclaw.pipeline.codegen.strategies.fallback import FallbackStrategy  # noqa: E402
from researchclaw.pipeline.codegen.types import CodegenContext  # noqa: E402
from researchclaw.pipeline.result_analysis.runtime import ResultAnalysisRuntime  # noqa: E402
from services.agent_bridge import _clear_run_from_stage  # noqa: E402


def test_imu_topic_routes_only_to_inertial_benchmarks() -> None:
    surveyor = SurveyorAgent(llm=None, enable_hf_search=False)

    result = surveyor.execute({
        "topic": "CV role: self-supervised learning, diffusion and Mamba. Research topic: IMU感知",
        "hypothesis": "Use accelerometer and gyroscope signals for activity recognition.",
        "domain_hints": ["ml_vision"],
    })

    assert result.success is True
    assert result.data["matched_domains"] == ["inertial_sensing"]
    names = {item["name"] for item in result.data["benchmarks"]}
    assert "UCI-HAR" in names
    assert "CIFAR-FS" not in names


def test_imu_codegen_failure_falls_back_to_real_uci_data(tmp_path: Path) -> None:
    context = CodegenContext(
        topic="IMU感知",
        exp_plan="datasets: [UCI-HAR]\nmetrics: [accuracy]",
        metric="primary_metric",
        metric_direction="maximize",
        time_budget_sec=600,
        mode="sandbox",
    )
    config = SimpleNamespace(research=SimpleNamespace(topic="IMU感知"))
    session = CodegenSession(stage_dir=tmp_path)

    result = FallbackStrategy().generate(context, config, None, session)

    assert result.strategy_name == "fallback_uci_har_real_dataset"
    assert "archive.ics.uci.edu/static/public/240" in result.files["main.py"]
    assert "EXPECTED_SHA256" in result.files["main.py"]
    assert "subject leakage" in result.files["main.py"]
    assert '"implementation": "uci_har_real_dataset"' in result.files["experiment_metadata.json"]
    compile(result.files["main.py"], "main.py", "exec")


def test_restart_removes_versioned_outputs_from_requested_stage(tmp_path: Path) -> None:
    for name in ("stage-08_v1", "stage-15_v2", "stage-16", "stage-16_v1", "stage-20_v3"):
        (tmp_path / name).mkdir()

    _clear_run_from_stage(tmp_path, 16)

    assert (tmp_path / "stage-08_v1").is_dir()
    assert (tmp_path / "stage-15_v2").is_dir()
    assert not (tmp_path / "stage-16").exists()
    assert not (tmp_path / "stage-16_v1").exists()
    assert not (tmp_path / "stage-20_v3").exists()


def test_result_analysis_workspace_ignores_versioned_and_current_stage(tmp_path: Path) -> None:
    current = tmp_path / "stage-16"
    current.mkdir()
    (tmp_path / "stage-14" / "runs").mkdir(parents=True)
    (tmp_path / "stage-14" / "runs" / "results.json").write_text(
        '{"source": "fresh-real"}', encoding="utf-8",
    )
    stale = tmp_path / "stage-16_v1" / "analysis_workspace_old"
    stale.mkdir(parents=True)
    (stale / "results.json").write_text(
        '{"source": "stale-synthetic"}', encoding="utf-8",
    )

    workspace = ResultAnalysisRuntime._prepare_workspace(current, tmp_path, object())

    copied = list(workspace.rglob("results.json"))
    assert copied
    assert all("stale-synthetic" not in path.read_text(encoding="utf-8") for path in copied)
    assert any("fresh-real" in path.read_text(encoding="utf-8") for path in copied)


def test_result_analysis_excludes_dataset_metadata_from_run_count(tmp_path: Path) -> None:
    rows: list[dict] = []
    ResultAnalysisRuntime._collect_metric_rows(
        {
            "dataset": {"train_samples": 100, "subject_overlap": 0},
            "conditions": {
                "linear": {"model": "linear", "n_seeds": 3, "accuracy_mean": 0.9},
                "forest": {"model": "forest", "n_seeds": 3, "accuracy_mean": 0.8},
            },
            "paired_comparisons": [
                {"metric": "accuracy", "n_pairs": 3, "p_value": 0.1},
            ],
        },
        "runs/results.json",
        rows,
    )

    assert sum(row.get("row_type") == "metadata" for row in rows) == 1
    assert sum(row.get("row_type") == "paired_comparison" for row in rows) == 1
    assert sum(row.get("row_type") is None for row in rows) == 2
