from __future__ import annotations

import sys
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from researchclaw.templates import get_template, markdown_to_latex  # noqa: E402
from researchclaw.templates.converter import _parse_sections, check_paper_completeness  # noqa: E402
from researchclaw.pipeline.executor import (  # noqa: E402
    _build_claim_integrity_report,
    _build_experiment_fact_contract,
    _baseline_only_outline_violations,
    _augment_canonical_citations,
    _collect_experiment_evidence,
    _enforce_engineering_report_boundary,
    _enforce_topic_experiment_constraints,
    _paper_draft_timeout_sec,
    _remove_missing_markdown_figures,
    _repair_markdown_figure_references,
    _remove_citations_from_text,
    _sanitize_fabricated_data,
    _sanitize_bibtex_for_latex,
    _scope_locked_reproduction_can_proceed,
)
from researchclaw.pipeline.codegen.system_prompt import (  # noqa: E402
    build_system_prompt,
    build_user_message,
    _constraints_section,
    _extract_plan_hints,
)
from researchclaw.pipeline.codegen.types import CodegenContext  # noqa: E402
from researchclaw.pipeline.codegen.turn_loop import ClawTurnLoop  # noqa: E402
from researchclaw.pipeline.codegen.runtime import CodegenRuntime  # noqa: E402
from researchclaw.pipeline.iterative_refine.runtime import IterativeRefineRuntime  # noqa: E402
from researchclaw.pipeline.result_analysis.runtime import ResultAnalysisRuntime  # noqa: E402
from services.agent_bridge import (  # noqa: E402
    BridgeState,
    _ensure_full_chain_ieee_config,
    _generate_config_from_template,
)


def test_ieee_template_generates_two_column_numbered_citation_paper() -> None:
    template = get_template("ieee")
    manuscript = """# EvidenceBot: Reproducible Agent Evaluation

## Abstract
We study a reproducible evaluation workflow using executed experiments.

## Index Terms
research agents, reproducibility, experiment automation, evidence verification

## Introduction
This is the introduction [smith2026].

## Methodology
This is the method.

## Experimental Setup
This is the experimental setup.

## Results
This is the result.

## Conclusion
This is the conclusion.
"""

    tex = markdown_to_latex(manuscript, template, bib_entries={"smith2026": "smith2026"})

    assert "\\documentclass[conference]{IEEEtran}" in tex
    assert "\\begin{IEEEkeywords}" in tex
    assert "research agents, reproducibility" in tex
    assert "\\bibliographystyle{IEEEtran}" in tex
    assert "\\section{Index Terms}" not in tex


def test_combined_experiments_and_results_heading_is_complete() -> None:
    paper = """# Audit Title
## Introduction
Text.
## Related Work
Text.
## Methodology
Text.
## Experiments and Results
Text.
## Discussion
Text.
## Conclusion
Text.
## Limitations
Text.
"""
    warnings = check_paper_completeness(_parse_sections(paper))
    assert not any("Missing sections" in warning for warning in warnings)


def test_full_chain_config_enforces_ieee_and_real_experiment_delivery(tmp_path: Path) -> None:
    state = BridgeState(
        runs_base_dir=str(tmp_path / "runs"),
        agent_package_dir=str(AGENT_DIR),
    )

    config_path = _generate_config_from_template(
        state,
        "ieee-full-chain",
        "可信智能体评测",
        target_conference="ieee",
    )
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

    assert config["export"]["target_conference"] == "ieee"
    assert config["export"]["target_pages"] == 8
    assert config["export"]["min_pages"] == 7
    assert config["export"]["max_pages"] == 8
    topic = config["research"]["topic"]
    assert "IEEE 双栏 LaTeX" in topic
    assert "真实运行记录" in topic
    assert "不得编造实验数据" in topic
    assert config["llm"]["primary_model"].lower().startswith("qwen3")
    assert config["llm"]["coding_model"].lower().startswith("qwen3")


def test_generated_config_infers_higher_is_better_metric(tmp_path: Path) -> None:
    state = BridgeState(
        runs_base_dir=str(tmp_path / "runs"),
        agent_package_dir=str(AGENT_DIR),
    )

    config_path = _generate_config_from_template(
        state,
        "imu-accuracy",
        "在 UCI-HAR 上报告 Accuracy 与 Macro-F1",
    )
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

    assert config["experiment"]["metric_key"] == "f1_macro"
    assert config["experiment"]["metric_direction"] == "maximize"


def test_generated_config_infers_lower_is_better_metric(tmp_path: Path) -> None:
    state = BridgeState(
        runs_base_dir=str(tmp_path / "runs"),
        agent_package_dir=str(AGENT_DIR),
    )

    config_path = _generate_config_from_template(
        state,
        "latency-study",
        "比较端到端 latency 与吞吐量",
    )
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

    assert config["experiment"]["metric_key"] == "latency"
    assert config["experiment"]["metric_direction"] == "minimize"


def test_experiment_plan_enforces_explicit_baseline_only_scope() -> None:
    plan = {
        "datasets": [{"name": "UCI-HAR raw resplit"}],
        "baselines": [
            {"name": "Linear SGD"},
            {"name": "Random Forest"},
            {"name": "CNN-LSTM"},
        ],
        "proposed_methods": [{"name": "Invented CNN-LSTM"}],
        "ablations": ["invented ablation"],
        "benchmark_suggestions": {"baselines": ["CNN-LSTM"]},
        "risks": [{"risk": "CNN-LSTM convergence failure"}],
        "objectives": ["Prove that the new method wins with five seeds"],
        "evaluation_protocol": {"independent_seeds": [1, 2, 3, 4, 5]},
    }

    constrained = _enforce_topic_experiment_constraints(
        plan,
        topic=(
            "仅使用官方 UCI-HAR 数据集，复现线性 SGD 与随机森林基线，"
            "使用 3 个独立随机种子、Accuracy 与 Macro-F1；禁止把未实现的新方法写入论文。"
        ),
        metric_key="f1_macro",
        metric_direction="maximize",
    )

    baseline_names = [item["name"] for item in constrained["baselines"]]
    assert baseline_names == ["Linear SGD", "Random Forest"]
    assert constrained["proposed_methods"] == []
    assert constrained["ablations"] == []
    assert "benchmark_suggestions" not in constrained
    assert "CNN" not in yaml.safe_dump(constrained["risks"])
    assert constrained["seeds"] == [1, 2, 3]
    assert constrained["evaluation_protocol"]["minimum_seeds_per_condition"] == 3
    assert constrained["metrics"] == ["accuracy", "f1_macro"]
    assert constrained["primary_metric"] == "f1_macro"
    assert constrained["metric_direction"] == "maximize"
    assert constrained["compute_budget"]["total_runs"] == 6
    assert constrained["datasets"][0]["source"] == "Official UCI Machine Learning Repository"
    assert "do not presuppose" in constrained["objectives"][0]


def test_codegen_hints_ignore_rejected_or_risk_only_technologies() -> None:
    plan = yaml.safe_dump({
        "metrics": ["accuracy", "f1_macro"],
        "baselines": ["Linear SGD", "Random Forest"],
        "proposed_methods": [],
        "risks": ["CNN-LSTM may require memory optimization"],
        "benchmark_suggestions": {"metrics": ["FID", "CLIP score"]},
    })

    assert _extract_plan_hints(plan) == []


def test_codegen_constraints_use_plan_seed_contract() -> None:
    context = CodegenContext(
        topic="UCI-HAR baseline reproduction",
        exp_plan=yaml.safe_dump({
            "baselines": [
                {"name": "Linear SGD", "implementation_spec": {"estimator": "sklearn.linear_model.SGDClassifier"}},
                {"name": "Random Forest", "implementation_spec": {"estimator": "sklearn.ensemble.RandomForestClassifier"}},
            ],
            "proposed_methods": [],
            "evaluation_protocol": {"independent_seeds": [11, 29, 47]},
        }),
        metric="f1_macro",
        metric_direction="maximize",
        time_budget_sec=600,
        mode="sandbox",
    )

    constraints = _constraints_section(context)
    assert "exactly 3 random seeds (11, 29, 47)" in constraints
    assert "42, 123, 456" not in constraints

    system_prompt = build_system_prompt(context)
    user_prompt = build_user_message(context)
    assert "do NOT introduce a pretrained neural model" in user_prompt
    assert "Use REAL pretrained models" not in system_prompt
    assert "Use exactly 3 seeds (11, 29, 47)" in user_prompt
    assert "FID via torchmetrics" not in user_prompt
    assert "generate synthetic data" not in system_prompt


def test_codegen_gates_accept_real_classical_ml_implementation(tmp_path: Path) -> None:
    plan = yaml.safe_dump({
        "baselines": [
            {
                "name": "Strict_Linear_SGD_LogLoss",
                "implementation_spec": {
                    "class_name": "StrictLinearSGD",
                    "estimator": "sklearn.linear_model.SGDClassifier",
                    "required_distinct_helpers": ["strict_normalization"],
                    "required_loss_terms": ["log_loss", "l2_regularization"],
                    "required_model_edits": "none",
                    "required_runtime_hooks": "none",
                },
            },
            {
                "name": "Robust_RandomForest_200",
                "implementation_spec": {
                    "class_name": "RobustRandomForest",
                    "estimator": "sklearn.ensemble.RandomForestClassifier",
                    "required_distinct_helpers": ["strict_normalization"],
                    "required_loss_terms": ["gini_impurity"],
                    "required_model_edits": "none",
                    "required_runtime_hooks": "none",
                },
            },
        ],
        "proposed_methods": [],
        "evaluation_protocol": {"independent_seeds": [11, 29, 47]},
    })
    code = """from sklearn.linear_model import SGDClassifier
from sklearn.ensemble import RandomForestClassifier
SMOKE_TEST = True
def compute_normalization_params(x): return x
def normalize_data(x): return x
class StrictLinearSGD:
    def __init__(self): self.model = SGDClassifier(loss='log_loss', penalty='l2')
class RobustRandomForest:
    def __init__(self): self.model = RandomForestClassifier(criterion='gini')
conditions = ["Strict_Linear_SGD_LogLoss", "Robust_RandomForest_200"]
for model in [StrictLinearSGD().model, RobustRandomForest().model]:
    model.fit(X_train, y_train)
    model.predict(X_test)
compute_normalization_params(X_train)
normalize_data(X_train)
"""
    (tmp_path / "main.py").write_text(code, encoding="utf-8")
    loop = object.__new__(ClawTurnLoop)
    loop._workspace = tmp_path
    loop._exp_plan = plan

    assert loop._plan_requires_pretrained_model() is False
    assert loop._run_simulation_check() is None
    assert loop._run_plan_compliance_check() == []


def test_codegen_archive_writes_nested_artifacts_safely(tmp_path: Path) -> None:
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    CodegenRuntime._write_generated_files(
        experiment_dir,
        {
            "main.py": "print('ok')\n",
            "data/UCI HAR Dataset/README.txt": "official data\n",
            "outputs/results.json": "{}\n",
        },
    )

    assert (experiment_dir / "data/UCI HAR Dataset/README.txt").read_text() == "official data\n"
    assert (experiment_dir / "outputs/results.json").exists()

    import pytest
    with pytest.raises(ValueError, match="Unsafe generated file path"):
        CodegenRuntime._write_generated_files(experiment_dir, {"../escape.txt": "bad"})


def test_codegen_resume_recovers_completed_qwen_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "claw_workspace_123"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "main.py").write_text("print('real experiment')\n", encoding="utf-8")
    (workspace / "outputs/results.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "claw_agent_log.json").write_text(
        '{"success": true, "files_produced": ["main.py", "outputs/results.json"]}',
        encoding="utf-8",
    )

    resumed = CodegenRuntime._load_resumable_generation(tmp_path)
    assert resumed is not None
    files, source = resumed
    assert source == workspace
    assert files["main.py"] == "print('real experiment')\n"
    assert files["outputs/results.json"] == "{}\n"


def test_iterative_refine_respects_locked_baseline_reproduction(tmp_path: Path) -> None:
    stage09 = tmp_path / "stage-09"
    stage09.mkdir()
    (stage09 / "exp_plan.yaml").write_text(
        yaml.safe_dump({
            "user_hard_constraints": {
                "applied": ["official_uci_har_only", "requested_baselines_only"],
            },
        }),
        encoding="utf-8",
    )

    assert IterativeRefineRuntime._has_scope_locked_baselines(tmp_path) is True


def test_locked_real_reproduction_proceeds_to_evidence_limited_writing(tmp_path: Path) -> None:
    stage09 = tmp_path / "stage-09"
    stage09.mkdir()
    (stage09 / "exp_plan.yaml").write_text(
        yaml.safe_dump({
            "user_hard_constraints": {
                "applied": ["official_uci_har_only", "requested_baselines_only"],
            },
        }),
        encoding="utf-8",
    )
    readiness = {
        "should_proceed_to_writing": True,
        "writing_policy": "engineering_report_only",
        "scientific_claims_allowed": False,
        "evidence": {
            "executed": True,
            "real_code_execution": True,
            "has_metrics": True,
        },
    }

    assert _scope_locked_reproduction_can_proceed(tmp_path, readiness) is True
    readiness["evidence"]["real_code_execution"] = False
    assert _scope_locked_reproduction_can_proceed(tmp_path, readiness) is False


def test_result_parser_accepts_condition_summary_seed_schema() -> None:
    rows = []
    ResultAnalysisRuntime._collect_metric_rows(
        {
            "conditions": {
                "Linear_SGD": {
                    "seeds": {
                        "11": {"accuracy": 0.91, "f1_macro": 0.90},
                        "29": {"accuracy": 0.93, "f1_macro": 0.92},
                        "47": {"accuracy": 0.92, "f1_macro": 0.91},
                    },
                    "summary": {
                        "f1_macro": {
                            "mean": 0.91,
                            "std": 0.01,
                            "ci_95": [0.88, 0.94],
                        },
                    },
                },
            },
            "statistical_tests": {
                "paired_t_test": {
                    "comparison": "Linear_SGD vs RF",
                    "p_value": 0.04,
                },
            },
        },
        "results.json",
        rows,
    )

    condition = next(row for row in rows if row.get("condition") == "Linear_SGD")
    assert condition["metrics"]["f1_macro"] == 0.91
    assert condition["metrics"]["f1_macro_ci95_low"] == 0.88
    assert len(condition["seed_metrics"]) == 3
    test = next(row for row in rows if row.get("row_type") == "paired_comparison")
    assert test["comparison"]["test"] == "paired_t_test"


def test_analysis_text_distinguishes_conditions_from_seed_runs() -> None:
    summary = {
        "condition_count": 2,
        "total_runs": 6,
        "metric_key": "f1_macro",
        "metrics_summary": {},
        "condition_summaries": {
            "Linear": {
                "n_seeds": 3,
                "metrics": {
                    "f1_macro": 0.91,
                    "f1_macro_ci95_low": 0.88,
                    "f1_macro_ci95_high": 0.94,
                },
            },
        },
        "paired_comparisons": [{
            "test": "wilcoxon_signed_rank",
            "comparison": "Linear vs RF",
            "p_value": 0.25,
        }],
        "experiment_provenance": {"display_status_zh": "真实执行"},
    }
    text = ResultAnalysisRuntime._render_analysis_markdown("UCI-HAR", summary, ["results.json"])

    assert "2 experimental condition(s) and 6 per-seed run(s)" in text
    assert "seeds=3" in text
    assert "wilcoxon_signed_rank" in text
    assert "No paired statistical test was run" not in text


def test_writing_fact_contract_uses_observed_official_split(tmp_path: Path) -> None:
    stage09 = tmp_path / "stage-09"
    stage11_data = tmp_path / "stage-11" / "experiment" / "data" / "train"
    stage11_test = tmp_path / "stage-11" / "experiment" / "data" / "test"
    stage16 = tmp_path / "stage-16"
    stage09.mkdir(); stage11_data.mkdir(parents=True); stage11_test.mkdir(parents=True); stage16.mkdir()
    (stage09 / "exp_plan.yaml").write_text(yaml.safe_dump({
        "datasets": [{
            "name": "UCI-HAR",
            "split_strategy": "official split without reshuffling",
            "preprocessing": "official precomputed feature matrices",
        }],
        "baselines": [{"name": "Linear SGD"}, {"name": "Random Forest"}],
        "proposed_methods": [],
        "seeds": [11, 29, 47],
        "metrics": ["accuracy", "f1_macro"],
    }), encoding="utf-8")
    (stage16 / "experiment_summary.json").write_text(
        '{"condition_count": 2, "total_runs": 6, "condition_summaries": '
        '{"Linear SGD": {"metrics": {"accuracy": 0.937}, "seed_metrics": '
        '{"11": {"accuracy": 0.932}}}}}', encoding="utf-8"
    )
    (stage11_data / "subject_train.txt").write_text("1\n3\n3\n", encoding="utf-8")
    (stage11_test / "subject_test.txt").write_text("2\n4\n", encoding="utf-8")
    (stage11_data / "X_train.txt").write_text("0 1 2\n", encoding="utf-8")

    contract = _build_experiment_fact_contract(tmp_path)
    assert "Official train subjects (2): [1, 3]" in contract
    assert "Official test subjects (2): [2, 4]" in contract
    assert "3 precomputed features" in contract
    assert "Do not invent or brand a new model" in contract
    assert "2 conditions and 6 per-seed runs" in contract
    assert "Exact observed aggregate for Linear SGD" in contract
    assert '"accuracy": 0.937' in contract
    assert "No per-activity, per-class" in contract


def test_peer_review_evidence_uses_domain_run_count_not_json_file_count(tmp_path: Path) -> None:
    stage14 = tmp_path / "stage-14" / "runs"
    stage16 = tmp_path / "stage-16"
    stage14.mkdir(parents=True); stage16.mkdir()
    (stage14 / "results.json").write_text(
        '{"seeds":[11,29,47],"conditions":{"SGD":{"seeds":{}}},'
        '"statistical_tests":{"paired_t_test":{"p_value":0.0075}}}',
        encoding="utf-8",
    )
    (stage16 / "experiment_summary.json").write_text(
        '{"total_runs":6,"condition_count":2,"condition_summaries":'
        '{"SGD":{"n_seeds":3}},"paired_comparisons":[]}',
        encoding="utf-8",
    )

    evidence = _collect_experiment_evidence(tmp_path)

    assert "6 executed per-seed condition runs" in evidence
    assert '"total_runs": 6' in evidence
    assert '"metrics": null' not in evidence


def test_canonical_baseline_bibliography_is_added_only_when_cited() -> None:
    paper = (
        "UCI-HAR [anguita2013uci], implemented with "
        "[pedregosa2011scikit, breiman2001random]."
    )
    bib = _augment_canonical_citations("", paper)
    assert "@misc{anguita2013uci" in bib
    assert "@article{pedregosa2011scikit" in bib
    assert "@article{breiman2001random" in bib


def test_verified_bibtex_metadata_is_pdf_latex_safe() -> None:
    bib = "@article{x, title={L0 via ℓ0 with A & B}, author={A and B}}"
    cleaned = _sanitize_bibtex_for_latex(bib)
    assert "ℓ" not in cleaned
    assert r"$\ell$" in cleaned
    assert r"A \& B" in cleaned


def test_low_relevance_filter_handles_multi_key_markdown_citation() -> None:
    text = "Claim [keep2020paper, drop2021paper] and [drop2021paper]."
    cleaned = _remove_citations_from_text(text, {"drop2021paper"})
    assert "[keep2020paper]" in cleaned
    assert "drop2021paper" not in cleaned


def test_baseline_only_outline_rejects_invented_protocol_brand(tmp_path: Path) -> None:
    stage09 = tmp_path / "stage-09"
    stage09.mkdir()
    (stage09 / "exp_plan.yaml").write_text(
        yaml.safe_dump({"proposed_methods": [], "baselines": ["SGD", "RF"]}),
        encoding="utf-8",
    )
    violations = _baseline_only_outline_violations(
        tmp_path,
        "## Method Name Proposal\nWe introduce **L-HAR**, a reproduction protocol.\nAcronym rationale.",
    )
    assert len(violations) == 3
    assert _baseline_only_outline_violations(
        tmp_path,
        "# A Reproducibility Audit of Linear SGD and Random Forest on UCI-HAR",
    ) == []


def test_resumed_full_chain_config_is_migrated_to_ieee(tmp_path: Path) -> None:
    config_path = tmp_path / "existing.yaml"
    config_path.write_text(
        yaml.safe_dump({"research": {"topic": "已有项目"}, "export": {"target_conference": "neurips_2025"}}, allow_unicode=True),
        encoding="utf-8",
    )

    assert _ensure_full_chain_ieee_config(str(config_path)) is True
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["export"]["target_conference"] == "ieee"
    assert config["export"]["target_pages"] == 8
    assert "不得编造实验数据" in config["research"]["topic"]
    assert _ensure_full_chain_ieee_config(str(config_path)) is False


def test_full_paper_timeout_covers_multiple_qwen_calls(monkeypatch) -> None:
    class Llm:
        timeout_sec = 120

    class Config:
        llm = Llm()

    monkeypatch.delenv("RESEARCHCLAW_PAPER_DRAFT_TIMEOUT_SEC", raising=False)
    assert _paper_draft_timeout_sec(Config()) == 3600
    monkeypatch.setenv("RESEARCHCLAW_PAPER_DRAFT_TIMEOUT_SEC", "900")
    assert _paper_draft_timeout_sec(Config()) == 900


def test_engineering_smoke_report_removes_unsupported_performance_claims(tmp_path: Path) -> None:
    stage16 = tmp_path / "stage-16"
    stage17 = tmp_path / "stage-17"
    stage16.mkdir()
    stage17.mkdir()
    (stage16 / "experiment_provenance.json").write_text(
        '{"real_code_execution": true, "execution_mode": "direct_run", '
        '"implementation": "synthetic_fallback"}',
        encoding="utf-8",
    )
    (stage17 / "research_readiness.json").write_text(
        '{"writing_policy": "engineering_report_only"}',
        encoding="utf-8",
    )
    paper = """# IMU Method

## Abstract
Our method reduces error by 68.6% and significantly outperforms all baselines.

## Experimental Setup
We evaluate the model.

## Results
The improvement is 68.6% with p < 0.01.

## Discussion
The method is state-of-the-art.

## Conclusion
Our method is superior.
"""

    bounded, applied = _enforce_engineering_report_boundary(paper, tmp_path)

    assert applied is True
    assert "68.6" not in bounded
    assert "p < 0.01" not in bounded
    assert "does not establish accuracy" in bounded
    assert "synthetic_fallback" in bounded
    assert "state-of-the-art" not in bounded.lower()
    assert "cite_key:" not in bounded

    report = _build_claim_integrity_report(tmp_path, bounded)
    assert report["status"] == "passed"
    assert report["has_empirical_performance_claims"] is False


def test_real_engineering_report_may_describe_bounded_metrics_and_test_conflict(tmp_path: Path) -> None:
    stage09 = tmp_path / "stage-09"
    stage16 = tmp_path / "stage-16"
    stage17 = tmp_path / "stage-17"
    stage09.mkdir(); stage16.mkdir(); stage17.mkdir()
    (stage09 / "exp_plan.yaml").write_text(
        yaml.safe_dump({"proposed_methods": [], "baselines": ["Linear SGD", "Random Forest"]}),
        encoding="utf-8",
    )
    (stage16 / "experiment_provenance.json").write_text(
        '{"executed": true, "real_code_execution": true, '
        '"implementation": "generated_domain_code", "execution_mode": "agentic_run"}',
        encoding="utf-8",
    )
    (stage16 / "experiment_summary.json").write_text(
        '{"metrics_summary": {"f1_macro": {"mean": 0.932384}}, '
        '"condition_summaries": {"Linear SGD": {"metrics": {"f1_macro": 0.936842}}, '
        '"Random Forest": {"metrics": {"f1_macro": 0.927926}}}}',
        encoding="utf-8",
    )
    (stage17 / "research_readiness.json").write_text(
        '{"writing_policy": "engineering_report_only", "evidence": '
        '{"executed": true, "real_code_execution": true, "has_metrics": true}}',
        encoding="utf-8",
    )
    paper = """# A Reproducibility Audit of Linear SGD and Random Forest

## Results
Linear SGD obtained Macro-F1 0.936842 and Random Forest obtained 0.927926.
The paired t-test was statistically significant; however, Wilcoxon was not, so the tests conflict.

## Limitations
Only three paired seeds were observed; no superiority conclusion is supported.
"""

    bounded, applied = _enforce_engineering_report_boundary(paper, tmp_path)
    assert applied is True
    assert "Visual-Inertial" not in bounded
    assert "0.936842" in bounded
    report = _build_claim_integrity_report(tmp_path, bounded)
    assert report["status"] == "passed"
    assert report["prohibited_empirical_claims"] is False


def test_claim_gate_blocks_invented_contiguous_subject_split(tmp_path: Path) -> None:
    stage09 = tmp_path / "stage-09"; stage16 = tmp_path / "stage-16"; stage17 = tmp_path / "stage-17"
    stage09.mkdir(); stage16.mkdir(); stage17.mkdir()
    (stage09 / "exp_plan.yaml").write_text(
        yaml.safe_dump({"proposed_methods": [], "baselines": ["SGD", "RF"]}), encoding="utf-8"
    )
    (stage16 / "experiment_summary.json").write_text('{"metrics_summary": {}}', encoding="utf-8")
    (stage17 / "research_readiness.json").write_text(
        '{"writing_policy": "engineering_report_only", "evidence": {}}', encoding="utf-8"
    )
    paper = """# Baseline Audit
## Method
Training Subjects (1-21) and Testing Subjects (22-30) are disjoint.
## Limitations
Evidence is limited.
"""
    report = _build_claim_integrity_report(tmp_path, paper)
    assert report["status"] == "blocked"
    assert len(report["experiment_fact_contradictions"]) == 2


def test_claim_gate_blocks_unexecuted_per_activity_analysis(tmp_path: Path) -> None:
    stage16 = tmp_path / "stage-16"; stage17 = tmp_path / "stage-17"
    stage16.mkdir(); stage17.mkdir()
    (stage16 / "experiment_summary.json").write_text(
        '{"condition_summaries":{"SGD":{"metrics":{"f1_macro":0.9368}}}}',
        encoding="utf-8",
    )
    (stage17 / "research_readiness.json").write_text(
        '{"writing_policy":"engineering_report_only","evidence":{}}', encoding="utf-8"
    )
    paper = """# Baseline Audit
## Results
### Per-Activity Performance Breakdown
We analyzed individual activity classes and found that sitting was easiest.
## Limitations
Only aggregate results were retained.
"""

    report = _build_claim_integrity_report(tmp_path, paper)

    assert report["status"] == "blocked"
    assert "claimed per-activity/class results" in report["experiment_fact_contradictions"][0]


def test_missing_figure_is_omitted_instead_of_breaking_pdf(tmp_path: Path) -> None:
    paper = "See the setup below.\n\n![Setup](charts/not-generated.png)\n"

    cleaned, missing = _remove_missing_markdown_figures(paper, tmp_path)

    assert missing == ["charts/not-generated.png"]
    assert "![Setup]" not in cleaned
    assert "Figure omitted" in cleaned


def test_stale_figure_number_is_replaced_with_stable_reference() -> None:
    paper = """As shown in Figure 6, the observations are paired.

![Paired Comparison](charts/paired.png)
*Figure 6: Paired observations.*
"""
    repaired = _repair_markdown_figure_references(paper)
    assert r"Figure \ref{fig:paired_comparison}" in repaired
    assert "*Figure 6: Paired observations.*" in repaired


def test_sanitizer_reads_result_analysis_stage_16(tmp_path: Path) -> None:
    stage16 = tmp_path / "stage-16"
    stage16.mkdir()
    (stage16 / "experiment_summary.json").write_text(
        '{"metrics_summary": {"verified": 0.42}}', encoding="utf-8"
    )
    paper = """## Results

| Method | Score |
| --- | ---: |
| verified | 0.42 |
| invented | 68.6 |
"""

    cleaned, report = _sanitize_fabricated_data(paper, tmp_path)

    assert "| verified | 0.42 |" in cleaned
    assert "| invented | --- |" in cleaned
    assert report["verified_values_count"] == 1
    assert report["numbers_replaced"] == 1
