"""Evidence, isolation, repair and transport regressions for the research engine."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig, validate_config
from researchclaw.llm.client import LLMConfig
from researchclaw.llm.tool_client import tool_request, tool_response
from researchclaw.observability.tracing import StageTrace, trace_llm_call
from researchclaw.orchestration.artifacts import Artifacts, read_json, write_json
from researchclaw.orchestration.assessment import compare_runs, validate_efficiency_approval
from researchclaw.orchestration.context import augment_system, stage_guidance
from researchclaw.orchestration.council import review_council
from researchclaw.orchestration.engine import prepare_stage
from researchclaw.orchestration.memory import memory_path, record_handoff
from researchclaw.orchestration.repair import begin_repair, complete_repair, propose_repair
from researchclaw.orchestration.rubric import core_criteria, planned_coverage, verify_research
from researchclaw.pipeline import executor, runner
from researchclaw.pipeline.contracts import CONTRACTS
from researchclaw.pipeline.executor import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.skills import SkillRegistry
from researchclaw.skills.registry import BUILTIN_ROOT


@pytest.fixture
def config(tmp_path):
    return RCConfig.from_dict({
        "project": {"name": "engine-test", "mode": "full-auto"},
        "research": {"topic": "Compare a fitted linear model, mean baseline and no-intercept ablation using three seeds"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": str(tmp_path / "kb")},
        "llm": {"provider": "openai-compatible", "api_key_env": "TEST_KEY"},
        "security": {"hitl_required_stages": []},
        "research_engine": {"enabled": True, "council_enabled": False, "max_repair_rounds": 2},
    }, check_paths=False)


def _rubric(root: Path, config):
    write_json(root / "research_rubric.json", {
        "topic": config.research.topic, "topic_hash": hashlib.sha256(config.research.topic.encode()).hexdigest(),
        "induction_status": "completed", "criteria": core_criteria(),
    })


@pytest.mark.parametrize("field,value", [("enabled", "false"), ("max_repair_rounds", True),
                                         ("skill_context_chars", -1), ("skills_dirs", "bad"),
                                         ("profile", "fast"), ("max_skills_per_stage", 0)])
def test_invalid_engine_settings_fail_before_execution(config, field, value):
    data = config.to_dict()
    data["security"]["hitl_required_stages"] = []
    data["research_engine"]["skills_dirs"] = []
    data["research_engine"][field] = value
    assert not validate_config(data, check_paths=False).ok


def test_archives_future_stages_and_external_symlinks_are_not_evidence(tmp_path):
    write_json(tmp_path / "research_attempts/round-001/stage-16/results.json", {"metric": 1})
    write_json(tmp_path / "stage-23/results.json", {"metric": 2})
    (tmp_path / "stage-16").mkdir()
    (tmp_path / "stage-16/results.json").symlink_to(tmp_path / "stage-23/results.json")
    artifacts = Artifacts(tmp_path, 17)
    assert not artifacts.exists("stage-16/results.json")
    assert not artifacts.exists("stage-23/results.json")
    assert not artifacts.exists("stage-16/../../etc/passwd")
    assert not artifacts.latest("results.json")


def test_zero_and_negative_metrics_are_valid_but_nan_is_not(tmp_path, config):
    _rubric(tmp_path, config)
    write_json(tmp_path / "stage-16/experiment_provenance.json", {
        "executed": True, "real_code_execution": True, "returncode": 0,
        "command": "python main.py", "execution_mode": "sandbox"})
    write_json(tmp_path / "stage-16/experiment_summary.json", {"metrics_summary": {"effect": 0, "loss_delta": -0.1}})
    report = verify_research(tmp_path, 17, config)
    assert next(row for row in report["criteria"] if row["criterion_id"] == "measurements")["satisfied"]
    (tmp_path / "stage-16/experiment_summary.json").write_text('{"metrics_summary":{"effect":NaN}}')
    report = verify_research(tmp_path, 17, config)
    assert not next(row for row in report["criteria"] if row["criterion_id"] == "measurements")["satisfied"]


def test_declaring_a_baseline_in_the_plan_does_not_count_as_running_it(tmp_path):
    (tmp_path / "stage-09").mkdir()
    (tmp_path / "stage-09/exp_plan.yaml").write_text("baselines: [control, reference]\nproposed_methods: [new_method]\n")
    write_json(tmp_path / "stage-16/experiment_summary.json", {
        "condition_summaries": {"new_method": {"metrics": {"accuracy": 0.8}}}})
    passed, message = planned_coverage(Artifacts(tmp_path))
    assert not passed and "control" in message and "reference" in message


def test_skills_are_versioned_and_routed_by_stage():
    registry = SkillRegistry()
    assert len(registry.skills) == 210
    selected = registry.select(9, "randomized statistical experiment")
    assert {skill.name for skill in selected} == {"experimental-design", "criterion-synthesis", "statistical-analysis"}
    text, usage = registry.prompt(21, "scientific paper", max_chars=12000)
    assert "peer-review" in text and len(text) <= 12000
    assert usage and all(item["sha256"] for item in usage)
    quantum_skills = {skill.name for skill in registry.select(11, "quantum qubit Hamiltonian circuit simulation")}
    assert quantum_skills.intersection({"qutip", "qiskit", "cirq", "pennylane"})
    assert not quantum_skills.intersection({"scikit-learn", "pytorch-lightning"})
    lock = read_json(BUILTIN_ROOT.parent / "sources.lock.json")
    assert all(hashlib.sha256((BUILTIN_ROOT / name).read_bytes()).hexdigest() == expected
               for name, expected in lock["files"].items())


def test_stage_context_and_usage_are_isolated_between_concurrent_projects(tmp_path):
    barrier = Barrier(2)

    def project(number):
        root = tmp_path / str(number)
        with StageTrace(root, root / "stage", number, "TEST", str(number)):
            with stage_guidance(f"project-{number}"):
                barrier.wait()
                assert f"project-{number}" in augment_system("system")
                trace_llm_call(model="test", messages=[], max_tokens=10, temperature=0,
                               json_mode=False, status="ok", latency_sec=0.1,
                               prompt_tokens=number, completion_tokens=1)
        assert augment_system("system") == "system"

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(project, (1, 2)))
    for number in (1, 2):
        row = json.loads((tmp_path / str(number) / "traces/llm_calls.jsonl").read_text())
        assert row["stage"] == str(number) and row["total_tokens"] == number + 1


def test_cross_session_memory_is_scoped_by_project_and_topic(tmp_path, config):
    run = tmp_path / "run"
    run.mkdir()
    result = StageResult(Stage.RESEARCH_DECISION, StageStatus.DONE, ())
    record_handoff(run, config, 17, result)
    assert memory_path(config).is_file()
    other = replace(config, project=replace(config.project, name="other-owner"))
    assert memory_path(config) != memory_path(other)


def test_specialists_receive_separate_perspectives_and_unsupported_issues_are_not_actions(tmp_path, config):
    (tmp_path / "stage-09").mkdir()
    (tmp_path / "stage-09/exp_plan.yaml").write_text("baselines: [control]")
    calls = []

    class Reviewer:
        def chat(self, messages, **kwargs):
            calls.append(kwargs["system"])
            return SimpleNamespace(content=json.dumps({"issues": [{"severity": "critical", "requirement": "X",
                "action": "fix", "evidence_refs": ["stage-09/nonexistent.json"]}], "verdict": "revise"}))

    report = review_council(tmp_path, 9, config, Reviewer())
    assert len(set(calls)) == 3 and report["complete"]
    assert all(not review["issues"][0]["supported"] for review in report["reviews"])


def test_repair_invalidates_entire_suffix_and_never_repeats_the_same_gap(tmp_path, config):
    _rubric(tmp_path, config)
    for stage in (11, 17, 23, 26):
        write_json(tmp_path / f"stage-{stage:02d}/old.json", {"stale": True})
    write_json(tmp_path / "stage-16_v1/old.json", {"stale": True})
    report = verify_research(tmp_path, 17, config)
    plan = propose_repair(tmp_path, 17, config)
    assert plan and plan["from_stage"] == 1
    begin_repair(tmp_path, plan, "test")
    assert not list(tmp_path.glob("stage-*"))
    assert (tmp_path / plan["archive"] / "stage-26/old.json").is_file()
    write_json(tmp_path / "research_verification.json", report)
    assert propose_repair(tmp_path, 17, config) is None


def test_regressed_repair_retains_previous_evidence(tmp_path):
    write_json(tmp_path / "stage-11/result.json", {"result": "before"})
    write_json(tmp_path / "research_verification.json", {"summary": {"coverage": 0.8}, "induction_status": "completed",
                                                       "criteria": [{"criterion_id": "C1", "requirement": "same requirement"}]})
    plan = {"from_stage": 11, "round": 1, "checkpoint_stage": 17,
            "verification_before": {"coverage": 0.8}, "actions": []}
    begin_repair(tmp_path, plan, "test")
    write_json(tmp_path / "stage-11/result.json", {"result": "worse"})
    write_json(tmp_path / "research_verification.json", {"summary": {"coverage": 0.5}, "induction_status": "completed",
                                                       "criteria": [{"criterion_id": "C1", "requirement": "same requirement"}]})
    complete_repair(tmp_path, 17)
    assert read_json(tmp_path / "stage-11/result.json")["result"] == "before"
    assert read_json(tmp_path / "research_repair_history.json")["attempts"][0]["retained"] == "previous"


def test_failed_task_contract_induction_returns_to_scoping(tmp_path, config):
    write_json(tmp_path / "research_verification.json", {"stage": 9, "induction_status": "unavailable",
                                                       "criteria": [], "summary": {"coverage": 1}})
    plan = propose_repair(tmp_path, 9, config)
    assert plan and plan["from_stage"] == 2 and plan["actions"][0]["criterion_id"] == "task_contract"


def test_improving_quality_gets_another_iteration_even_if_same_criterion_is_unmet(tmp_path, config):
    def score(value):
        path = "stage-23/quality_report.json"
        write_json(tmp_path / path, {"score_1_to_10": value})
        write_json(tmp_path / "research_verification.json", {"stage": 23, "induction_status": "completed",
            "criteria": [{"criterion_id": "paper_quality", "requirement": "Meet 7/10", "owner_stage": 22,
                          "status": "unmet", "critical": True, "evidence_gap": "Below target",
                          "evidence_found": [Artifacts(tmp_path).reference(path)]}], "summary": {"coverage": 0.9}})
    score(5)
    first = propose_repair(tmp_path, 23, config)
    write_json(tmp_path / "research_repair_history.json", {"attempts": [first]})
    assert propose_repair(tmp_path, 23, config) is None
    score(6)
    assert propose_repair(tmp_path, 23, config) is not None


@pytest.mark.parametrize("model,endpoint,token_key", [
    ("Qwen3.5-122B-A10B-FP8", "/chat/completions", "max_tokens"),
    ("gpt-5.4", "/chat/completions", "max_completion_tokens"),
    ("gpt-5.5", "/responses", "max_output_tokens"),
])
def test_tool_wire_format_preserves_history_and_uses_correct_endpoint(model, endpoint, token_key):
    cfg = LLMConfig(base_url="https://example.test/v1", api_key="test")
    calls = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object"}}}]
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call1", "content": "evidence"}]
    url, body = tool_request(cfg, model, messages, calls)
    assert url.endswith(endpoint) and token_key in body
    if endpoint == "/responses":
        assert body["tools"][0]["name"] == "read_file"
        assert any(item.get("type") == "function_call_output" and item["call_id"] == "call1" for item in body["input"])


def test_responses_tool_calls_and_reasoning_items_survive_normalization():
    raw = {"output": [{"type": "reasoning", "id": "r1", "encrypted_content": "opaque"},
                      {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"}],
           "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}
    value = tool_response(raw)
    from researchclaw.pipeline.claw_engine.turn_loop import AgentTurnLoop
    message = AgentTurnLoop._build_assistant_message(value)
    assert message["tool_calls"][0]["id"] == "c1" and message["_response_items"][0]["id"] == "r1"


def test_faster_run_without_external_quality_evidence_cannot_enable_efficiency(tmp_path, config):
    before, after = tmp_path / "before", tmp_path / "after"
    for root, tokens in ((before, 100), (after, 50)):
        _rubric(root, config)
        write_json(root / "research_verification.json", {"stage": 26, "all_satisfied": True,
            "criteria": [{"criterion_id": "C1", "satisfied": True}], "summary": {"coverage": 1}})
        write_json(root / "traces/llm_trace_summary.json", {"total_tokens": tokens, "total_latency_sec": tokens / 10, "usage_complete": True})
        (root / "traces/workflow_trace.jsonl").write_text(json.dumps({"event": "stage_finished", "duration_sec": tokens}) + "\n")
    proof = tmp_path / "comparison.json"
    report = compare_runs(before, after, proof)
    assert report["efficiency_improved"] and not report["approve_efficiency_profile"]
    balanced = replace(config, research_engine=replace(config.research_engine, profile="balanced", efficiency_approval=str(proof)))
    with pytest.raises(ValueError):
        validate_efficiency_approval(balanced)
    for root, score in ((before, 40), (after, 42)):
        write_json(root / "external_evaluation.json", {"benchmark": "test", "task_id": "T1", "evaluator_version": "v1",
            "judge_model": "independent", "data_revision": "fixed", "score": score})
    assert compare_runs(before, after, proof)["approve_efficiency_profile"]
    validate_efficiency_approval(balanced)


def test_end_to_end_repairs_missing_condition_using_real_subprocess(tmp_path, config, monkeypatch):
    """Run a real seeded numerical comparison, detect an omitted control, repair,
    then re-run and verify. Model generation is stubbed; computation is real.
    """
    root = tmp_path / "e2e"
    root.mkdir()
    _rubric(root, config)
    for stage in range(1, 11):
        for name in CONTRACTS[Stage(stage)].output_files:
            target = root / f"stage-{stage:02d}" / name
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                (target / "card.md").write_text("Source-backed fixture")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture")
    (root / "stage-09/exp_plan.yaml").write_text(
        "conditions:\n  - name: control\n  - name: linear\n  - name: ablation\n"
        "evaluation_protocol:\n  independent_seeds: [1, 2, 3]\n  minimum_seeds_per_condition: 3\n"
        "  report: [mean, standard_deviation, 95%_confidence_interval]\n")
    code = '''import json, random, statistics, sys
conditions = ['control', 'linear'] + (['ablation'] if sys.argv[1] == 'repair' else [])
rows = {}
for method in conditions:
    errors = []
    for seed in [1,2,3]:
        rng = random.Random(seed)
        x = [rng.uniform(-2,2) for _ in range(60)]
        y = [2*v + 1 + rng.gauss(0,0.1) for v in x]
        xm, ym = statistics.mean(x), statistics.mean(y)
        slope = sum((a-xm)*(b-ym) for a,b in zip(x,y))/sum((a-xm)**2 for a in x)
        intercept = ym-slope*xm
        pred = [ym if method=='control' else slope*a+(intercept if method=='linear' else 0) for a in x]
        errors.append(statistics.mean((a-b)**2 for a,b in zip(pred,y)))
    mean, std = statistics.mean(errors), statistics.stdev(errors)
    rows[method] = {'n_seeds':3,'seed_metrics':errors,'metrics':{'mse':mean,'mse_std':std,'mse_ci95_low':mean-1.96*std/(3**0.5)}}
print(json.dumps({'condition_summaries':rows,'metrics_summary':{'mse':{'mean':rows['linear']['metrics']['mse'],'count':3}},'total_runs':len(rows)*3}))
'''
    executions = []

    def step(stage_dir, run_dir, config, adapters, **kwargs):
        stage = Stage(int(stage_dir.name[-2:]))
        for name in CONTRACTS[stage].output_files:
            target = stage_dir / name
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                (target / "fixture.txt").write_text("fixture")
            else:
                target.write_text("fixture")
        if stage == Stage.CODE_GENERATION:
            (stage_dir / "experiment/main.py").write_text(code)
        elif stage == Stage.EXPERIMENT_RUN:
            repaired = (root / "research_revision_plan.json").exists()
            command = [sys.executable, str(root / "stage-11/experiment/main.py"), "repair" if repaired else "initial"]
            completed = subprocess.run(command, text=True, capture_output=True, check=True)
            executions.append(json.loads(completed.stdout))
            write_json(stage_dir / "runs/results.json", executions[-1])
        elif stage == Stage.RESULT_ANALYSIS:
            write_json(stage_dir / "experiment_summary.json", executions[-1])
            write_json(stage_dir / "experiment_provenance.json", {"executed": True, "real_code_execution": True,
                "returncode": 0, "command": f"{sys.executable} main.py", "execution_mode": "sandbox", "scientific_claims_allowed": True})
        elif stage == Stage.RESEARCH_DECISION:
            write_json(stage_dir / "research_readiness.json", {"readiness_level": "limited_evidence", "writing_policy": "limited_claims_only"})
        elif stage == Stage.EXPORT_PUBLISH:
            write_json(stage_dir / "final_claim_integrity_report.json", {"status": "passed"})
        elif stage == Stage.QUALITY_GATE:
            write_json(stage_dir / "quality_report.json", {"score_1_to_10": 8.0, "verdict": "proceed"})
        elif stage == Stage.CITATION_VERIFY:
            write_json(stage_dir / "verification_report.json", {"summary": {"total": 2, "verified": 2}})
        return StageResult(stage, StageStatus.DONE, CONTRACTS[stage].output_files)

    for stage in Stage:
        monkeypatch.setitem(executor._STAGE_EXECUTORS, stage, step)
    monkeypatch.setattr(runner, "_package_deliverables", lambda *args: None)
    results = runner.execute_pipeline(run_dir=root, run_id="real-computation", config=config,
                                     adapters=AdapterBundle(), from_stage=Stage.CODE_GENERATION,
                                     auto_approve_gates=True)
    assert results[-1].stage == Stage.CITATION_VERIFY and len(executions) == 2
    assert "ablation" not in executions[0]["condition_summaries"]
    assert "ablation" in executions[1]["condition_summaries"]
    report = read_json(root / "research_verification.json")
    assert report["all_satisfied"] and report["quality_validated"] is False
    history = read_json(root / "research_repair_history.json")["attempts"]
    assert len(history) == 1 and history[0]["from_stage"] == 11 and history[0]["coverage_change"] > 0
    assert (root / "research_attempts/round-001/stage-14/runs/results.json").exists()


def test_failure_is_not_overwritten_by_an_approval_gate(tmp_path, config, monkeypatch):
    config = replace(config, security=replace(config.security, hitl_required_stages=(5,)),
                     research_engine=replace(config.research_engine, enabled=False))
    root = tmp_path / "run"
    (root / "stage-04").mkdir(parents=True)
    (root / "stage-04/candidates.jsonl").write_text("{}")
    monkeypatch.setitem(executor._STAGE_EXECUTORS, Stage.LITERATURE_SCREEN,
        lambda *a, **k: StageResult(Stage.LITERATURE_SCREEN, StageStatus.FAILED, (), error="actual failure"))
    result = executor.execute_stage(Stage.LITERATURE_SCREEN, run_dir=root, run_id="gate", config=config, adapters=AdapterBundle())
    assert result.status == StageStatus.FAILED and result.error == "actual failure"


def test_prior_artifact_lookup_does_not_consume_outputs_from_future_stages(tmp_path):
    for stage, text in ((9, "current design"), (25, "obsolete exported design")):
        directory = tmp_path / f"stage-{stage:02d}"
        directory.mkdir()
        (directory / "exp_plan.yaml").write_text(text)
    with StageTrace(tmp_path, tmp_path / "stage-11", 11, "CODE_GENERATION", "test"):
        assert executor._read_prior_artifact(tmp_path, "exp_plan.yaml") == "current design"
        assert executor._find_prior_file(tmp_path, "exp_plan.yaml") == tmp_path / "stage-09/exp_plan.yaml"


def test_native_tool_calls_receive_skills_and_contribute_usage(tmp_path, monkeypatch):
    from researchclaw.pipeline.claw_engine.turn_loop import AgentTurnLoop
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = LLMConfig(base_url="https://example.test/v1", api_key="test", primary_model="qwen-test")
    session = SimpleNamespace(llm_calls=0, log=lambda *a: None, log_error=lambda *a: None)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"choices": [{"message": {"role": "assistant", "content": "done"}}],
                               "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}}).encode()

    def request(req, timeout):
        captured.update(json.loads(req.data))
        return Response()

    from researchclaw.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, "_urlopen", request)
    with StageTrace(tmp_path, tmp_path / "stage-11", 11, "CODE_GENERATION", "test"):
        with stage_guidance("Use the required independent seeds."):
            loop = AgentTurnLoop(llm_config=config, workspace=workspace, system_prompt="Generate code", session=session)
            loop._messages = [{"role": "user", "content": "Build the comparison"}]
            loop._call_llm()
    assert "independent seeds" in captured["messages"][0]["content"]
    usage = json.loads((tmp_path / "traces/llm_calls.jsonl").read_text())
    assert usage["total_tokens"] == 35 and usage["stage"] == "11"


def test_configured_output_budget_reaches_client_and_native_tool_requests(config):
    from researchclaw.llm.client import LLMClient
    config = replace(config, llm=replace(config.llm, max_tokens=32768))
    client = LLMClient.from_rc_config(config)
    assert client.config.max_tokens == 32768
    _, request = tool_request(client.config, "deepseek-flash", [{"role": "user", "content": "Implement the task"}], [])
    assert request["max_tokens"] == 32768


def test_explicit_judge_model_uses_selected_provider_options(config, monkeypatch):
    from researchclaw.pipeline.support import settings
    monkeypatch.setenv("RESEARCHCLAW_IDEA_JUDGE_MODEL", "deepseek-flash")
    monkeypatch.setenv("RESEARCHCLAW_IDEA_JUDGE_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("RESEARCHCLAW_IDEA_JUDGE_API_KEY", "fixture-only")
    monkeypatch.setattr(settings, "_load_root_arc_section", lambda *a: {
        "primary_model": "old-provider-model", "extra_body": {"provider_specific_option": True}})
    config = replace(config, llm=replace(config.llm, max_tokens=32768, reasoning_token_reserve=8192,
                                        extra_body={"thinking": {"type": "enabled"}}))
    client, model = settings._create_idea_judge_llm(config)
    assert model == "deepseek-flash" and client.config.max_tokens == 32768
    assert client.config.extra_body == {"thinking": {"type": "enabled"}}
    assert client.config.reasoning_token_reserve == 8192


@pytest.mark.parametrize("visible,expected", [(None, 32768), (2500, 10692), (32000, 32768), (40000, 40000)])
def test_reasoning_reserve_prevents_stage_answer_starvation_with_bounded_total(config, monkeypatch, visible, expected):
    from researchclaw.llm.client import LLMClient, LLMResponse
    config = replace(config, llm=replace(config.llm, max_tokens=32768, reasoning_token_reserve=8192))
    client = LLMClient.from_rc_config(config)
    captured = []
    def respond(model, messages, max_tokens, temperature, json_mode):
        captured.append(max_tokens)
        return LLMResponse(content='{"verdict":"pass","issues":[]}', model=model)
    monkeypatch.setattr(client, "_call_with_retry", respond)
    client.chat([{"role": "user", "content": "Review the design"}], max_tokens=visible, json_mode=True)
    assert captured == [expected]


def test_stage_token_limit_remains_unchanged_without_reasoning_reserve(config, monkeypatch):
    from researchclaw.llm.client import LLMClient, LLMResponse
    client = LLMClient.from_rc_config(config)
    captured = []
    def respond(model, messages, max_tokens, temperature, json_mode):
        captured.append(max_tokens)
        return LLMResponse(content="ok", model=model)
    monkeypatch.setattr(client, "_call_with_retry", respond)
    client.chat([{"role": "user", "content": "Review"}], max_tokens=500)
    assert captured == [500]


def test_internal_quality_target_rejects_a_low_scoring_manuscript(tmp_path, config):
    _rubric(tmp_path, config)
    write_json(tmp_path / "stage-23/quality_report.json", {"score_1_to_10": 5.0, "verdict": "proceed"})
    report = verify_research(tmp_path, 23, config)
    row = next(row for row in report["criteria"] if row["criterion_id"] == "paper_quality")
    assert row["status"] == "unmet" and "7.0" in row["evidence_gap"]


def test_semantic_judge_cannot_pass_experiments_using_only_report_text(tmp_path, config):
    _rubric(tmp_path, config)
    rubric = read_json(tmp_path / "research_rubric.json")
    rubric["criteria"].append({"criterion_id": "task_1", "requirement": "Implement the proposed method",
        "check": "semantic", "due_stage": 17, "owner_stage": 11, "evidence_paths": [], "critical": True})
    write_json(tmp_path / "research_rubric.json", rubric)
    (tmp_path / "stage-01").mkdir()
    (tmp_path / "stage-01/goal.md").write_text("We implemented the method")
    write_json(tmp_path / "stage-16/experiment_provenance.json", {"executed": True, "real_code_execution": True,
        "returncode": 0, "command": "python main.py", "execution_mode": "sandbox"})

    class Judge:
        def chat(self, *a, **k):
            return SimpleNamespace(content=json.dumps({"criteria": [{"criterion_id": "task_1", "satisfied": True,
                "reason": "The report says so", "artifact_references": ["stage-01/goal.md"]}]}))

    report = verify_research(tmp_path, 17, config, Judge())
    row = next(row for row in report["criteria"] if row["criterion_id"] == "task_1")
    assert row["status"] == "unmet" and "source-code" in row["evidence_gap"]


def test_resolved_failures_do_not_mark_a_repaired_run_failed(tmp_path):
    failed = StageResult(Stage.QUALITY_GATE, StageStatus.FAILED, (), error="old failure")
    done = StageResult(Stage.QUALITY_GATE, StageStatus.DONE, ())
    final = StageResult(Stage.CITATION_VERIFY, StageStatus.DONE, ())
    summary = runner._build_pipeline_summary(run_id="repair", results=[failed, done, final],
                                              from_stage=Stage.QUALITY_GATE, run_dir=tmp_path)
    assert summary["stages_failed"] == 0 and summary["final_status"] == "done"
