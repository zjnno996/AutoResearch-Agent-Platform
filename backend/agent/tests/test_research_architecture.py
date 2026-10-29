"""Runtime boundaries, fault recovery and source-attributed method composition."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.cli import main
from researchclaw.config import RCConfig, validate_config
from researchclaw.evaluation.protocol import audit_evaluation_protocol
from researchclaw.llm.client import LLMConfig
from researchclaw.llm.retry import retryable_model_error
from researchclaw.llm.tool_client import ModelResponseError, tool_response
from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.lease import RunAlreadyActive, run_lease
from researchclaw.orchestration.methods import METHOD_SOURCES, method_plan, select_strategy
from researchclaw.orchestration.memory import retained_lessons
from researchclaw.orchestration.repair import begin_repair, complete_repair
from researchclaw.orchestration.rubric import induce_rubric, verify_research
from researchclaw.pipeline.claw_engine.tools.executor import ToolExecutor
from researchclaw.pipeline.claw_engine.turn_loop import AgentTurnLoop
from researchclaw.pipeline.contracts import CONTRACTS
from researchclaw.pipeline.graph import Dependency, StageGraph
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.registry import StageRegistry
from researchclaw.pipeline.runtime import StageBindings, execute_stage
from researchclaw.pipeline.stages import STAGE_SEQUENCE, Stage, StageStatus


@pytest.fixture
def config(tmp_path):
    return RCConfig.from_dict({
        "project": {"name": "architecture-test", "mode": "full-auto"},
        "research": {"topic": "Compare controlled conditions with repeated seeds"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": str(tmp_path / "kb")},
        "llm": {"provider": "openai-compatible", "api_key_env": "UNSET_ARCH_TEST_KEY"},
        "security": {"hitl_required_stages": [9]},
        "research_engine": {"enabled": True, "council_enabled": False, "rubric_enabled": False,
                            "skills_enabled": False, "shared_memory": False},
    }, check_paths=False)


def test_registry_adapts_legacy_signatures_but_never_reexecutes_a_typeerror():
    calls = []
    def legacy(*args, llm=None):
        calls.append(1)
        return StageResult(Stage.TOPIC_INIT, StageStatus.DONE, ())
    registry = StageRegistry({Stage.TOPIC_INIT: legacy})
    assert registry.invoke(Stage.TOPIC_INIT, llm=None, prompts=object()).status == StageStatus.DONE
    def broken(*args, **kwargs):
        calls.append(1)
        raise TypeError("raised inside business implementation")
    registry.register(Stage.TOPIC_INIT, broken, replace=True)
    with pytest.raises(TypeError, match="inside business"):
        registry.invoke(Stage.TOPIC_INIT, prompts=object())
    assert len(calls) == 2
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Stage.TOPIC_INIT, broken)


def test_registry_rejects_wrong_stage_and_describes_contracts():
    registry = StageRegistry({Stage.TOPIC_INIT: lambda **kw: StageResult(Stage.PROBLEM_DECOMPOSE, StageStatus.DONE, ())})
    with pytest.raises(TypeError, match="Invalid stage result"):
        registry.invoke(Stage.TOPIC_INIT)
    assert registry.describe()[0]["outputs"] == list(CONTRACTS[Stage.TOPIC_INIT].output_files)


def test_dependency_graph_preserves_compatible_order_and_reports_resume_dependencies():
    graph = StageGraph()
    assert graph.order == STAGE_SEQUENCE
    assert graph.ready(set()) == (Stage.TOPIC_INIT,)
    assert Stage.CODE_GENERATION in graph.affected_by({Stage.EXPERIMENT_DESIGN})
    assert Stage.CITATION_VERIFY in graph.affected_by({Stage.EXPERIMENT_DESIGN})
    plan = graph.describe(Stage.CODE_GENERATION, Stage.RESULT_ANALYSIS)
    assert plan["order"] == list(range(11, 17))
    assert any(edge["artifact"] == "exp_plan.yaml" for edge in plan["external_dependencies"])
    with pytest.raises(ValueError, match="to_stage"):
        graph.execution_order(Stage.RESULT_ANALYSIS, Stage.TOPIC_INIT)


def test_dependency_graph_rejects_cycles_and_missing_producers():
    with pytest.raises(ValueError, match="cycle"):
        StageGraph(controls=(Dependency(Stage.PROBLEM_DECOMPOSE, Stage.TOPIC_INIT, "control"),))
    with pytest.raises(ValueError, match="No producer"):
        StageGraph({Stage.PROBLEM_DECOMPOSE: CONTRACTS[Stage.PROBLEM_DECOMPOSE]}, controls=())


def test_run_lease_nested_repairs_thread_conflicts_and_release_after_failure(tmp_path):
    def attempt():
        with run_lease(tmp_path, "other-controller"):
            return True
    with pytest.raises(RuntimeError, match="injected failure"):
        with run_lease(tmp_path, "owner"):
            with run_lease(tmp_path, "nested-repair"):
                assert read_json(tmp_path / ".execution.lock")["run_id"] == "owner"
            with ThreadPoolExecutor(max_workers=1) as pool:
                with pytest.raises(RunAlreadyActive):
                    pool.submit(attempt).result()
            with run_lease(tmp_path / "independent-run", "another-run"):
                pass
            raise RuntimeError("injected failure")
    assert read_json(tmp_path / ".execution.lock")["status"] == "released"
    assert attempt()


def test_run_lease_prevents_a_second_process(tmp_path):
    script = ("from pathlib import Path\n"
              "from researchclaw.orchestration.lease import run_lease, RunAlreadyActive\n"
              "import sys\n"
              "try:\n with run_lease(Path(sys.argv[1]), 'second-process'): pass\n"
              "except RunAlreadyActive:\n sys.exit(17)\n")
    with run_lease(tmp_path, "first-process"):
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=10, capture_output=True)
    assert result.returncode == 17, result.stderr.decode()


@pytest.mark.parametrize("topic,expected", [
    ("复现这篇论文并记录原协议的偏差", "reproduction"),
    ("回顾性队列研究与敏感性分析", "observational"),
    ("有限元数值模拟的收敛分析", "numerical"),
    ("形式化证明这个结论", "formal"),
    ("Compare model A and model B", "comparison"),
    ("复现一个数值模拟", "comparison"),  # Ambiguous intent stays conservative.
])
def test_task_routing_is_conservative_and_does_not_waive_requirements(config, topic, expected):
    config = replace(config, research=replace(config.research, topic=topic))
    strategy = select_strategy(config)
    assert strategy.study_type == expected
    plan = method_plan(config)
    assert "cannot waive" in plan["policy"]
    assert len(strategy.roles) == 3
    if expected == "formal":
        assert "not yet implemented" in strategy.validation_limit


def test_explicit_method_overrides_topic_and_sources_disclose_code_availability(config):
    config = replace(config, research_engine=replace(config.research_engine, study_type="observational"))
    assert select_strategy(config).selection == "explicit_configuration"
    sources = {item["agent"]: item for item in METHOD_SOURCES}
    assert len(sources) == 6
    assert sources["AutoSciRub"]["evidence"] == "inspected_public_code"
    assert sources["Ligase"]["evidence"] == "public_product_description"
    assert "proprietary" in sources["Ligase"]["limit"]
    assert not method_plan(config)["benchmark_equivalence"]
    data = config.to_dict()
    data["research_engine"]["skills_dirs"] = []
    data["research_engine"]["study_type"] = "imaginary"
    assert any("study_type" in error for error in validate_config(data, check_paths=False).errors)


def test_method_obligations_become_persistent_criteria_without_deleting_goals(config, tmp_path):
    config = replace(config, research_engine=replace(config.research_engine, study_type="numerical"))
    rubric = induce_rubric(tmp_path, config)
    methods = [row for row in rubric["criteria"] if row["origin"] == "method_strategy"]
    assert len(methods) == 2 and all(row["check"] == "semantic" for row in methods)
    custom = {"criterion_id": "task_1", "requirement": "Preserve the requested dataset", "check": "semantic", "origin": "task_instruction"}
    rubric["criteria"].append(custom)
    write_json(tmp_path / "research_rubric.json", rubric)
    refreshed = induce_rubric(tmp_path, config)
    assert custom in refreshed["criteria"]
    assert len([row for row in refreshed["criteria"] if row["origin"] == "method_strategy"]) == 2


def test_method_recipe_cannot_masquerade_as_successful_task_induction(config, tmp_path):
    config = replace(config, research_engine=replace(config.research_engine, study_type="numerical"))
    model = SimpleNamespace(chat=lambda *a, **kw: SimpleNamespace(content='{"criteria": [], "goals": []}'))
    rubric = induce_rubric(tmp_path, config, model)
    assert rubric["induction_status"] == "unavailable"


def test_completed_review_with_supported_critical_issue_cannot_pass(config, tmp_path):
    config = replace(config, research_engine=replace(config.research_engine, council_enabled=True))
    induce_rubric(tmp_path, config)
    write_json(tmp_path / "stage-09/research_council.json", {"complete": True, "reviews": [
        {"role": "statistician", "issues": [{"supported": True, "severity": "critical", "action": "Remove data leakage"}]}]})
    report = verify_research(tmp_path, 9, config)
    review = next(row for row in report["criteria"] if row["criterion_id"] == "design_review")
    assert not review["satisfied"] and review["evidence_gap"] == "Remove data leakage"


def test_formal_proof_remains_unmet_until_a_real_validator_exists(config, tmp_path):
    config = replace(config, research_engine=replace(config.research_engine, study_type="formal"))
    induce_rubric(tmp_path, config)
    report = verify_research(tmp_path, 17, config)
    proof_checks = [row for row in report["criteria"] if row["origin"] == "method_strategy"]
    assert all(not row["satisfied"] and "validator" in row["evidence_gap"] for row in proof_checks)


def test_repair_cannot_trade_a_satisfied_requirement_for_higher_total_coverage(tmp_path):
    def verification(passed):
        return {"stage": 17, "induction_status": "completed", "summary": {"coverage": len(passed) / 3},
                "criteria": [{"criterion_id": key, "requirement": key, "satisfied": key in passed}
                             for key in ("requested_dataset", "new_condition", "new_metric")]}
    write_json(tmp_path / "research_verification.json", verification({"requested_dataset"}))
    plan = {"round": 1, "checkpoint_stage": 17, "from_stage": 11, "signature": "test",
            "verification_before": {"coverage": 1 / 3},
            "actions": [{"criterion_id": "new_condition", "action": "Add planned condition"}]}
    begin_repair(tmp_path, plan, "run")
    write_json(tmp_path / "research_verification.json", verification({"new_condition", "new_metric"}))
    complete_repair(tmp_path, 17)
    attempt = read_json(tmp_path / "research_repair_history.json")["attempts"][0]
    assert attempt["retained"] == "previous"
    assert attempt["regressed_criteria"] == ["requested_dataset"]
    assert retained_lessons(tmp_path)[0]["retained"] == "previous"


def test_experience_requires_a_comparable_retained_improvement(tmp_path):
    attempt = {"completed": True, "comparable_contract": True, "retained": "current", "coverage_change": 0.2,
               "actions": [{"criterion_id": "coverage", "requirement": "All conditions", "action": "Run missing control"}]}
    write_json(tmp_path / "research_repair_history.json", {"attempts": [attempt]})
    lesson = retained_lessons(tmp_path)[0]
    assert lesson["source_sha256"] and "Not causal" in lesson["scope"]
    attempt["comparable_contract"] = False
    write_json(tmp_path / "research_repair_history.json", {"attempts": [attempt]})
    assert retained_lessons(tmp_path) == []


def test_protocol_checks_raw_repetitions_instead_of_trusting_a_claimed_count(tmp_path):
    stage_dir = tmp_path / "stage-16"
    write_json(stage_dir / "experiment_summary.json", {"condition_summaries": {
        "control": {"n_seeds": 100, "metrics": {"loss": 0.2}}}})
    report = audit_evaluation_protocol(tmp_path, stage_dir)
    assert report["status"] == "insufficient_evidence"
    assert report["conditions"][0]["observed_seeds"] == 0
    assert not report["raw_seed_metrics_present"]
    write_json(stage_dir / "experiment_summary.json", {"condition_summaries": {
        "control": {"n_seeds": 3, "seed_metrics": {"11": {"loss": 0.2}, "29": {"loss": 0.3}, "47": {"loss": 0.1}},
                    "metrics": {"loss": 0.2}}}})
    assert audit_evaluation_protocol(tmp_path, stage_dir)["status"] == "passed"


def _bindings(stage, handler, *, missing=False):
    def metadata(directory, stage, run_id, result):
        write_json(directory / "decision.json", {"status": result.status.value})
    return StageBindings(StageRegistry({stage: handler}), lambda *a: None if missing else "input",
                         metadata, lambda *a: "")


def test_handoff_records_final_approval_state(config, tmp_path):
    def handler(stage_dir, *args, **kwargs):
        (stage_dir / "exp_plan.yaml").write_text("conditions: [a, b]\n")
        return StageResult(Stage.EXPERIMENT_DESIGN, StageStatus.DONE, ("exp_plan.yaml",))
    result = execute_stage(Stage.EXPERIMENT_DESIGN, run_dir=tmp_path, run_id="gate", config=config,
                           adapters=AdapterBundle(), bindings=_bindings(Stage.EXPERIMENT_DESIGN, handler))
    assert result.status == StageStatus.BLOCKED_APPROVAL
    assert read_json(tmp_path / "research_state.json")["state"] == "awaiting_approval"
    assert read_json(tmp_path / "research_engine_summary.json")["stages"]["9"]["status"] == "blocked_approval"
    assert read_json(tmp_path / "stage-09/stage_health.json")["status"] == "blocked_approval"
    assert read_json(tmp_path / "research_method_plan.json")["priority"] == "quality_before_efficiency"


def test_missing_input_has_health_handoff_and_does_not_reuse_stale_verification(config, tmp_path):
    write_json(tmp_path / "research_engine_summary.json", {"all_satisfied": True})
    write_json(tmp_path / "stage-09/research_verification.json", {"all_satisfied": True})
    result = execute_stage(Stage.EXPERIMENT_DESIGN, run_dir=tmp_path, run_id="missing", config=config,
                           adapters=AdapterBundle(), bindings=_bindings(Stage.EXPERIMENT_DESIGN, None, missing=True))
    assert result.status == StageStatus.FAILED
    assert not read_json(tmp_path / "research_engine_summary.json")["all_satisfied"]
    assert "Missing input" in read_json(tmp_path / "research_state.json")["next_task"]
    assert read_json(tmp_path / "stage-09/stage_health.json")["status"] == "failed"


def test_tools_report_real_exit_status_and_controller_written_journal(tmp_path):
    executor = ToolExecutor(tmp_path, journal_path=tmp_path / "harness/events.jsonl")
    failed = executor.execute_structured("bash", {"command": "echo diagnostic >&2\nexit 7"})
    assert failed.is_error and failed.exit_code == 7 and failed.error_code == "nonzero_exit"
    assert "diagnostic" in failed.content
    assert executor.execute("bash", {"command": "exit 2"})[1] is True
    passed = executor.execute_structured("bash", {"command": "echo actual-output"})
    assert not passed.is_error and passed.exit_code == 0
    events = [json.loads(line) for line in (tmp_path / "harness/events.jsonl").read_text().splitlines()]
    assert [row["exit_code"] for row in events] == [7, 2, 0]
    assert all(len(row["output_sha256"]) == 64 for row in events)


def test_tool_timeout_retains_partial_output_and_kills_its_descendants(tmp_path):
    executor = ToolExecutor(tmp_path, bash_timeout=1)
    command = "trap '' TERM\nsleep 30 &\nprintf '%s' $! > descendant.pid\nprintf 'partial-output\\n'\nwait"
    outcome = executor.execute_structured("bash", {"command": command, "timeout": 0.3})
    assert outcome.is_error and outcome.timed_out and outcome.error_code == "timeout"
    assert "partial-output" in outcome.content
    pid = int((tmp_path / "descendant.pid").read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
    assert outcome.duration_sec < 5


@pytest.mark.parametrize("name,args", [
    ("bash", {"command": 123}), ("bash", {"command": "echo ok", "timeout": -1}),
    ("bash", []), ("read_file", {"path": "missing", "offset": "bad"}),
    ("grep_search", {"pattern": "["}), ("write_file", {"path": "x"}),
])
def test_malformed_tool_inputs_are_errors(tmp_path, name, args):
    outcome = ToolExecutor(tmp_path).execute_structured(name, args)
    assert outcome.is_error and outcome.error_code == "invalid_input"
    assert not (tmp_path / "x").exists()


def test_failed_edit_is_not_reported_as_success(tmp_path):
    (tmp_path / "code.py").write_text("unchanged")
    outcome = ToolExecutor(tmp_path).execute_structured("edit_file", {
        "path": "code.py", "old_string": "missing text", "new_string": "replacement"})
    assert outcome.is_error
    assert (tmp_path / "code.py").read_text() == "unchanged"


@pytest.mark.parametrize("status,expected", [(400, False), (401, False), (403, False),
                                           (429, True), (502, True), (503, True)])
def test_retry_policy_distinguishes_service_errors_from_auth_or_input(status, expected):
    assert retryable_model_error(urllib.error.HTTPError("https://example.test", status, "test", {}, None)) is expected


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"output": []},
                                     {"choices": [{"message": {"content": "", "tool_calls": []}}]}])
def test_empty_model_responses_cannot_silently_complete_a_stage(payload):
    with pytest.raises(ModelResponseError):
        tool_response(payload)


def _session():
    return SimpleNamespace(llm_calls=0, log=lambda *a: None, log_error=lambda *a: None)


def test_native_loop_fails_auth_once_and_retries_transient_errors(config, tmp_path, monkeypatch):
    def make_loop(name):
        workspace = tmp_path / name / "workspace"
        workspace.mkdir(parents=True)
        return AgentTurnLoop(llm_config=LLMConfig("https://example.test", "test", max_retries=3, retry_base_delay=0),
                             workspace=workspace, system_prompt="test", session=_session())
    unauthorized = make_loop("auth")
    def auth_error():
        raise urllib.error.HTTPError("https://example.test", 401, "unauthorized", {}, None)
    monkeypatch.setattr(unauthorized, "_call_llm", auth_error)
    result = unauthorized.run_turn("test")
    assert unauthorized._session.llm_calls == 1 and result.errors
    retry = make_loop("transient")
    calls = []
    def transient():
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError("https://example.test", 502, "bad gateway", {}, None)
        return tool_response({"choices": [{"message": {"content": "complete"}}]})
    monkeypatch.setattr(retry, "_call_llm", transient)
    assert retry.run_turn("test").final_text == "complete"
    assert retry._session.llm_calls == 2


def test_native_loop_receives_typed_tool_failure_then_can_repair(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = AgentTurnLoop(llm_config=LLMConfig("https://example.test", "test"), workspace=workspace,
                         system_prompt="test", session=_session())
    replies = iter([
        {"choices": [{"message": {"tool_calls": [{"id": "bad", "function": {"name": "bash", "arguments": '{"command":"exit 4"}'}}]}}]},
        {"choices": [{"message": {"tool_calls": [{"id": "fixed", "function": {"name": "bash", "arguments": '{"command":"echo computed > result.txt"}'}}]}}]},
        {"choices": [{"message": {"content": "complete"}}]},
    ])
    monkeypatch.setattr(loop, "_call_llm", lambda: next(replies))
    result = loop.run_turn("compute")
    assert result.tool_calls == 2 and result.tool_errors == 1
    feedback = [item["content"] for item in loop._messages if item["role"] == "tool"]
    assert "nonzero_exit" in feedback[0]
    assert (workspace / "result.txt").read_text().strip() == "computed"


def test_cli_plan_has_methods_and_dependencies_without_model_calls(tmp_path, capsys):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("project:\n  name: plan-test\nresearch:\n  topic: 复现一篇论文\n"
                           "runtime:\n  timezone: UTC\nnotifications:\n  channel: local\n"
                           "knowledge_base:\n  root: kb\nllm:\n  api_key_env: UNSET_PLAN_TEST_KEY\n"
                           "research_engine:\n  enabled: true\n")
    output = tmp_path / "plan.json"
    assert main(["plan", "--config", str(config_file), "--from-stage", "9", "--to-stage", "17", "--output", str(output)]) == 0
    plan = read_json(output)
    assert plan["execution"]["order"] == list(range(9, 18))
    assert plan["methods"]["strategy"]["study_type"] == "reproduction"
    assert "api_key" not in capsys.readouterr().out
