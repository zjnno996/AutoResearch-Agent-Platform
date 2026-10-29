"""Exercise the actual S11 loop as well as the shared research tool loop."""

import json
import urllib.error
from pathlib import Path

import pytest

from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.llm.tool_client import ModelResponseError, call_tool_model
from researchclaw.observability.tracing import StageTrace
from researchclaw.orchestration.context import stage_guidance
from researchclaw.pipeline.codegen.session import CodegenSession
from researchclaw.pipeline.codegen.turn_loop import ClawTurnLoop
from researchclaw.pipeline.claw_engine.turn_loop import AgentTurnLoop
from researchclaw.pipeline.codegen.requirements import requires_pretrained_model
from researchclaw.pipeline.codegen.system_prompt import build_system_prompt, build_user_message
from researchclaw.pipeline.codegen.types import CodegenContext


class Response:
    def __init__(self, raw):
        self.raw = raw
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return json.dumps(self.raw).encode()
    def __iter__(self):
        event = {"model": self.raw.get("model", "deepseek-flash"), "usage": self.raw.get("usage", {}),
                 "choices": [{"delta": {"content": c["message"].get("content")},
                              "finish_reason": c.get("finish_reason", "stop")} for c in self.raw.get("choices", [])]}
        yield ("data: " + json.dumps(event) + "\n").encode()
        yield b"data: [DONE]\n"


@pytest.mark.parametrize("loop_type", [ClawTurnLoop, AgentTurnLoop])
def test_production_tool_loops_preserve_provider_state_and_count_requests(tmp_path, monkeypatch, loop_type):
    workspace = tmp_path / "stage-11/workspace"
    workspace.mkdir(parents=True)
    session = CodegenSession(workspace.parent)
    cfg = LLMConfig(base_url="https://example.test/v1", api_key="fixture-only", primary_model="deepseek-flash",
                    max_tokens=32768, extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
    requests = []
    def request(req, timeout):
        requests.append(json.loads(req.data))
        message = {"role": "assistant", "content": "Done"}
        if len(requests) == 1:
            message.update(reasoning_content="synthetic-provider-state", tool_calls=[
                {"id": "write", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "main.py", "content": "print('real output')"})}},
                {"id": "bad", "type": "function", "function": {"name": "bash", "arguments": "[]"}},
            ])
        return Response({"model": "deepseek-flash", "choices": [{"message": message, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}})
    monkeypatch.setattr(LLMClient, "_urlopen", request)
    with StageTrace(tmp_path, workspace.parent, 11, "CODE_GENERATION", "test"):
        with stage_guidance("Retain the independent validation conditions."):
            loop = loop_type(llm_config=cfg, workspace=workspace, system_prompt="Generate code", session=session)
            result = loop.run_turn("Implement the plan")
    assert len(requests) == session.llm_calls == 2
    assert result.tool_calls == 2
    assert (workspace / "main.py").read_text() == "print('real output')"
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert requests[0]["reasoning_effort"] == "high"
    assert requests[0]["max_tokens"] == 32768
    assert "independent validation" in requests[0]["messages"][0]["content"]
    assert next(m for m in requests[1]["messages"] if m["role"] == "assistant")["reasoning_content"] == "synthetic-provider-state"
    assert any("invalid_input" in m.get("content", "") for m in requests[1]["messages"] if m["role"] == "tool")
    rows = [json.loads(line) for line in (tmp_path / "traces/llm_calls.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and sum(row["total_tokens"] for row in rows) == 70
    assert all(row["stage"] == "11" and row["finish_reason"] == "stop" for row in rows)
    journals = list((workspace.parent / "harness").glob("*_tool_events.jsonl"))
    events = [json.loads(line) for p in journals for line in p.read_text().splitlines()]
    assert len(events) == 2 and events[-1]["error_code"] == "invalid_input"


@pytest.mark.parametrize("loop_type", [ClawTurnLoop, AgentTurnLoop])
@pytest.mark.parametrize("status,expected", [(401, 1), (400, 1), (503, 2)])
def test_production_tool_loops_retry_transient_failures_only(tmp_path, monkeypatch, loop_type, status, expected):
    workspace = tmp_path / "stage-11/workspace"
    workspace.mkdir(parents=True)
    session = CodegenSession(workspace.parent)
    cfg = LLMConfig(base_url="https://example.test/v1", api_key="fixture-only", primary_model="deepseek-flash",
                    max_retries=2, retry_base_delay=0)
    calls = []
    def request(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, status, "fixture error", {}, None)
    monkeypatch.setattr(LLMClient, "_urlopen", request)
    loop = loop_type(llm_config=cfg, workspace=workspace, system_prompt="Generate code", session=session)
    result = loop.run_turn("Implement the plan")
    assert len(calls) == session.llm_calls == expected
    assert result.errors


def test_invalid_model_output_retains_consumed_usage(tmp_path, monkeypatch):
    raw = {"choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "length"}],
           "usage": {"prompt_tokens": 40, "completion_tokens": 100, "total_tokens": 140}}
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *a, **kw: Response(raw))
    cfg = LLMConfig(base_url="https://example.test/v1", api_key="fixture-only")
    with StageTrace(tmp_path, tmp_path / "stage-11", 11, "CODE_GENERATION", "test"):
        with pytest.raises(ModelResponseError):
            call_tool_model(cfg, "deepseek-flash", [{"role": "user", "content": "Implement"}], [])
    row = json.loads((tmp_path / "traces/llm_calls.jsonl").read_text())
    assert row["status"] == "error" and row["total_tokens"] == 140
    assert row["finish_reason"] == "length"


def test_malformed_message_is_reported_without_masking_the_transport_error(tmp_path, monkeypatch):
    raw = {"choices": [{"message": "invalid", "finish_reason": "stop"}], "usage": []}
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *a, **kw: Response(raw))
    cfg = LLMConfig(base_url="https://example.test/v1", api_key="fixture-only")
    with StageTrace(tmp_path, tmp_path / "stage-11", 11, "CODE_GENERATION", "test"):
        with pytest.raises(ModelResponseError):
            call_tool_model(cfg, "deepseek-flash", [{"role": "user", "content": "Implement"}], [])
    row = json.loads((tmp_path / "traces/llm_calls.jsonl").read_text())
    assert row["status"] == "error" and row["total_tokens"] == 0


@pytest.mark.parametrize("plan,expected", [
    ({"proposed_methods": ["ADMM", "Nesterov acceleration"], "baselines": ["ISTA", "FISTA"]}, False),
    ({"proposed_methods": ["finite element solver for diffusion"], "implementation_spec": {"save": "checkpoint every 50 steps"}}, False),
    ({"baselines": ["SGDClassifier", "RandomForestClassifier"]}, False),
    ({"proposed_methods": ["Train a CNN from scratch"]}, False),
    ({"model": {"name": "ResNet18", "pretrained": False}}, False),
    ({"model": {"name": "ResNet18", "pretrained": True}}, True),
    ({"proposed_methods": ["Load ViT with from_pretrained"]}, True),
    ({"proposed_methods": ["ADMM"], "risks": ["A pretrained transformer is irrelevant"], "rejected_suggestions": ["ResNet"]}, False),
    ({}, False),
    ([], False),
])
def test_model_requirement_follows_executable_plan_and_domain(plan, expected):
    assert requires_pretrained_model(plan) is expected


def test_actual_checkpoint_requirement_is_preserved():
    assert requires_pretrained_model({"model": {"pretrained": False}}, "/configured/weights")


def test_numerical_codegen_prompt_and_runtime_gate_agree(tmp_path):
    import yaml
    plan = yaml.safe_dump({"proposed_methods": ["ADMM", "Nesterov acceleration"], "baselines": ["ISTA"]})
    ctx = CodegenContext(topic="Compare convergence of optimization methods", exp_plan=plan,
        metric="optimality_gap", metric_direction="minimize", time_budget_sec=100, mode="sandbox")
    prompts = build_system_prompt(ctx) + build_user_message(ctx)
    assert "You MUST use REAL pretrained models" not in prompts
    assert "requested scikit-learn estimator APIs" not in prompts
    assert "algorithms, equations, solvers or estimators" in prompts
    assert "Keep numerical evidence separate from mathematical proof" in prompts
    ws = tmp_path / "workspace"
    ws.mkdir()
    loop = ClawTurnLoop(llm_config=LLMConfig(base_url="https://example.test", api_key="fixture-only"),
        workspace=ws, system_prompt="Code", session=CodegenSession(tmp_path))
    loop.set_exp_plan(plan)
    assert not loop._plan_requires_pretrained_model()
    (ws / "main.py").write_text("import numpy as np\nx = np.linalg.solve(np.eye(2), np.ones(2))\nprint(x)\n")
    assert loop._run_simulation_check() is None


def test_classical_estimators_still_require_actual_fitting_and_evaluation(tmp_path):
    loop = ClawTurnLoop(llm_config=LLMConfig(base_url="https://example.test", api_key="fixture-only"),
        workspace=tmp_path, system_prompt="Code", session=CodegenSession(tmp_path))
    loop.set_exp_plan("baselines: [SGDClassifier, RandomForestClassifier]\n")
    (tmp_path / "main.py").write_text("from sklearn.linear_model import SGDClassifier\nfrom sklearn.ensemble import RandomForestClassifier\n")
    assert "not both fitted and evaluated" in loop._run_simulation_check()


def test_rejected_estimators_do_not_change_numerical_implementation_gate(tmp_path):
    loop = ClawTurnLoop(llm_config=LLMConfig(base_url="https://example.test", api_key="fixture-only"),
        workspace=tmp_path, system_prompt="Code", session=CodegenSession(tmp_path))
    loop.set_exp_plan("proposed_methods: [ADMM]\nrejected_suggestions: [SGDClassifier, RandomForestClassifier]\n")
    (tmp_path / "main.py").write_text("import numpy as np\nx = np.linalg.solve(np.eye(2), np.ones(2))\nprint(x)\n")
    assert loop._run_simulation_check() is None


def test_concurrent_codegen_prompts_keep_their_own_workspace():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from researchclaw.pipeline.codegen.system_prompt import set_workspace_path
    barrier = Barrier(2)
    def build(path):
        set_workspace_path(path)
        barrier.wait()
        ctx = CodegenContext(topic="Numerical solver", exp_plan="proposed_methods: [ADMM]", metric="gap",
            metric_direction="minimize", time_budget_sec=100, mode="sandbox")
        return build_system_prompt(ctx)
    with ThreadPoolExecutor(max_workers=2) as pool:
        prompts = list(pool.map(build, ["/project-alpha/workspace", "/project-beta/workspace"]))
    assert "/project-alpha/workspace" in prompts[0] and "/project-beta/workspace" not in prompts[0]
    assert "/project-beta/workspace" in prompts[1] and "/project-alpha/workspace" not in prompts[1]


def test_actual_mutation_branches_keep_guidance_and_record_every_request(tmp_path, monkeypatch):
    from researchclaw.pipeline.ideation.branches import _generate_idea_mutation_branches
    from researchclaw.observability import tracing
    requests = []
    def request(req, timeout):
        requests.append(json.loads(req.data))
        return Response({"model": "deepseek-flash", "choices": [{"message": {"role": "assistant", "content": "Grounded branch"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    monkeypatch.setattr(LLMClient, "_urlopen", request)
    monkeypatch.setattr(tracing.time, "time", lambda: 123.0)
    client = LLMClient(LLMConfig(base_url="https://example.test/v1", api_key="fixture-only", primary_model="deepseek-flash"))
    with StageTrace(tmp_path, tmp_path / "stage-08", 8, "HYPOTHESIS_GEN", "test"):
        with stage_guidance("Keep the task-specific scientific acceptance criteria."):
            branches = _generate_idea_mutation_branches(client, "Numerical solvers", "Evidence", "Ideas", "Sources", tmp_path / "branches", 1)
    assert len(branches) == len(requests) == 3
    assert all("task-specific scientific acceptance criteria" in r["messages"][0]["content"] for r in requests)
    rows = [json.loads(line) for line in (tmp_path / "traces/llm_calls.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and sum(row["total_tokens"] for row in rows) == 45
    assert all(row["stage"] == "8" for row in rows)
    assert len({row["call_id"] for row in rows}) == 3


@pytest.mark.parametrize("purpose", ["background", "reproduction"])
def test_design_distinguishes_related_work_from_an_explicit_reproduction_target(tmp_path, purpose):
    from types import SimpleNamespace
    from researchclaw.config import RCConfig
    from researchclaw.adapters import AdapterBundle
    from researchclaw.pipeline.steps.design import _execute_experiment_design
    cfg = RCConfig.from_dict({
        "project": {"name": "scope-test"},
        "research": {"topic": "Unify accelerated optimization and splitting", "reference_purpose": purpose},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": str(tmp_path / "kb")},
        "llm": {"provider": "openai-compatible", "api_key_env": "TEST_KEY"},
        "experiment": {"benchmark_agent": {"enabled": False}},
    }, check_paths=False)
    (tmp_path / "stage-04").mkdir()
    (tmp_path / "stage-04/reference_paper_text.md").write_text("A related paper about one accelerated algorithm.")
    stage = tmp_path / "stage-09"
    stage.mkdir()
    prompts = []
    class Model:
        def chat(self, messages, **kwargs):
            prompts.append("\n".join(m["content"] for m in messages))
            return type("Reply", (), {"content": "baselines: [ISTA]\nproposed_methods: [ADMM]\ndatasets: [provided]\nmetrics: [optimality_gap]\nobjectives: [unification]\nrisks: [assumptions]\ncompute_budget: [CPU]\n"})()
    _execute_experiment_design(stage, tmp_path, cfg, AdapterBundle(), llm=Model())
    text = "\n".join(prompts)
    if purpose == "background":
        assert "RELATED WORK (BACKGROUND CONTEXT)" in text
        assert "REPRODUCE the following paper" not in text
        assert "Re-read the reference paper text" not in text
    else:
        assert "REPRODUCE the following paper" in text
        assert "Re-read the reference paper text" in text


def test_benchmark_uses_provided_papers_as_background(tmp_path):
    from researchclaw.config import RCConfig
    from researchclaw.benchmarks.rcbench import task_config
    (tmp_path / "INSTRUCTIONS.md").write_text("Establish the requested framework using the supplied data")
    (tmp_path / "related_work").mkdir()
    (tmp_path / "related_work/background.pdf").write_bytes(b"reference fixture")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/problem.csv").write_text("x,y\n1,2\n")
    cfg = RCConfig.from_dict({"project": {"name": "scope"}, "research": {"topic": "original"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": "kb"}, "llm": {"api_key_env": "TEST_KEY"}}, check_paths=False)
    projected = task_config(cfg, tmp_path)
    assert projected.research.reference_purpose == "background"
    assert projected.research.reference_papers == (str(tmp_path / "related_work/background.pdf"),)
    assert not projected.experiment.benchmark_agent.enabled


def test_validated_benchmark_suggestions_do_not_override_local_task_baselines(tmp_path, monkeypatch):
    import yaml
    from types import SimpleNamespace
    from researchclaw.config import RCConfig
    from researchclaw.adapters import AdapterBundle
    from researchclaw.pipeline.steps.design import _execute_experiment_design
    from researchclaw.agents import benchmark_agent
    from researchclaw.domains import detector
    data = tmp_path / "data"
    data.mkdir()
    (data / "problem.csv").write_text("x,y\n1,2\n")
    cfg = RCConfig.from_dict({"project": {"name": "local"}, "research": {"topic": "Compare optimization solvers", "reference_purpose": "background"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"}, "knowledge_base": {"root": "kb"},
        "llm": {"api_key_env": "TEST_KEY"}, "experiment": {"mode": "sandbox", "datasets_dir": str(data), "benchmark_agent": {"enabled": True}}}, check_paths=False)
    suggestion = SimpleNamespace(validation_passed=True, selected_benchmarks=[{"name": "unrelated dataset"}],
        selected_baselines=[{"name": "unrelated baseline"}], validation_errors=[], validation_warnings=[], llm_calls=0, elapsed_sec=0,
        to_dict=lambda: {"validation_passed": True})
    monkeypatch.setattr(detector, "is_ml_domain", lambda *a: True)
    monkeypatch.setattr(benchmark_agent, "BenchmarkOrchestrator", lambda *a, **kw: SimpleNamespace(orchestrate=lambda request: suggestion))
    class Model:
        def chat(self, *args, **kwargs):
            return SimpleNamespace(content="baselines: [ISTA]\nproposed_methods: [ADMM]\ndatasets: [provided]\nmetrics: [gap]\nobjectives: [compare]\nrisks: [conditioning]\ncompute_budget: [CPU]\n")
    stage = tmp_path / "stage-09"
    stage.mkdir()
    _execute_experiment_design(stage, tmp_path, cfg, AdapterBundle(), llm=Model())
    plan = yaml.safe_load((stage / "exp_plan.yaml").read_text())
    assert plan["baselines"] == ["ISTA"]
    assert plan["datasets"] == ["provided"]
    assert plan["benchmark_suggestions"]["baselines"] == ["unrelated baseline"]
    assert plan["benchmark_suggestions"]["validation_passed"] is True


@pytest.mark.parametrize("topic", ["Compute the minimum objective", "Run a numerical simulation", "Find the maximum convergence rate"])
def test_ordinary_mathematical_words_cannot_select_an_imu_dataset(tmp_path, topic):
    from types import SimpleNamespace
    from researchclaw.pipeline.codegen.strategies.fallback import FallbackStrategy
    cfg = SimpleNamespace(research=SimpleNamespace(topic=topic))
    ctx = CodegenContext(topic=topic, exp_plan="risks: [UCI-HAR is unrelated]", metric="gap",
        metric_direction="minimize", time_budget_sec=30, mode="sandbox")
    assert not FallbackStrategy._should_use_uci_har(ctx, cfg)
    result = FallbackStrategy().generate(ctx, cfg, None, CodegenSession(tmp_path))
    assert not result.files and result.error


def test_supplied_task_data_cannot_be_replaced_by_a_fallback(tmp_path):
    from types import SimpleNamespace
    from researchclaw.pipeline.codegen.strategies.fallback import FallbackStrategy
    cfg = SimpleNamespace(research=SimpleNamespace(topic="IMU analysis on the supplied measurements"))
    ctx = CodegenContext(topic=cfg.research.topic, exp_plan="datasets: [provided]", metric="accuracy",
        metric_direction="maximize", time_budget_sec=30, mode="sandbox", datasets_dir=str(tmp_path / "measurements"))
    result = FallbackStrategy().generate(ctx, cfg, None, CodegenSession(tmp_path))
    assert not result.files and "task-compatible" in result.error


def test_explicit_imu_task_retains_its_real_dataset_generator(tmp_path):
    from types import SimpleNamespace
    from researchclaw.pipeline.codegen.strategies.fallback import FallbackStrategy
    cfg = SimpleNamespace(research=SimpleNamespace(topic="IMU感知"))
    ctx = CodegenContext(topic=cfg.research.topic, exp_plan="datasets: [UCI-HAR]", metric="accuracy",
        metric_direction="maximize", time_budget_sec=30, mode="sandbox")
    result = FallbackStrategy().generate(ctx, cfg, None, CodegenSession(tmp_path))
    assert result.strategy_name == "fallback_uci_har_real_dataset"
    compile(result.files["main.py"], "main.py", "exec")


def test_premature_final_answer_gets_a_bounded_request_to_write_main(tmp_path, monkeypatch):
    cfg = LLMConfig(base_url="https://example.test/v1", api_key="fixture-only", primary_model="deepseek-flash")
    session = CodegenSession(tmp_path)
    requests = []
    def request(req, timeout):
        requests.append(json.loads(req.data))
        message = {"role": "assistant", "content": "Finished"}
        if len(requests) == 2:
            message["tool_calls"] = [{"id": "write", "type": "function", "function": {"name": "write_file",
                "arguments": json.dumps({"path": "main.py", "content": "print('implemented')"})}}]
        return Response({"choices": [{"message": message, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    monkeypatch.setattr(LLMClient, "_urlopen", request)
    loop = ClawTurnLoop(llm_config=cfg, workspace=tmp_path, system_prompt="Generate code", session=session, max_iterations=5)
    result = loop.run_turn("Implement the supplied plan")
    assert session.llm_calls == 3 and result.tool_calls == 1
    assert (tmp_path / "main.py").read_text() == "print('implemented')"
    assert any("have not created main.py" in m.get("content", "") for m in requests[1]["messages"])


def test_codegen_runtime_reports_failure_without_substituting_another_task(tmp_path, monkeypatch):
    from researchclaw.config import RCConfig
    from researchclaw.adapters import AdapterBundle
    from researchclaw.pipeline.codegen.runtime import CodegenRuntime
    from researchclaw.pipeline.codegen.strategies.claw_agent import ClawAgentStrategy
    from researchclaw.pipeline.codegen.types import CodegenResult
    from researchclaw.pipeline.stages import StageStatus
    data = tmp_path / "data"
    data.mkdir()
    source = data / "problem.npy"
    source.write_bytes(b"immutable-input-fixture")
    cfg = RCConfig.from_dict({"project": {"name": "scope"}, "research": {"topic": "Minimize the supplied objective"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"}, "knowledge_base": {"root": "kb"},
        "llm": {"api_key_env": "TEST_KEY"}, "experiment": {"mode": "sandbox", "datasets_dir": str(data)}}, check_paths=False)
    monkeypatch.setattr(ClawAgentStrategy, "generate", lambda *a, **kw: CodegenResult(strategy_name="claw_agent", error="no executable generated"))
    stage = tmp_path / "stage-11"
    stage.mkdir()
    result = CodegenRuntime().execute(stage, tmp_path, cfg, AdapterBundle())
    assert result.status == StageStatus.FAILED
    assert not (stage / "experiment/main.py").exists()
    assert source.read_bytes() == b"immutable-input-fixture"
    assert "task-compatible" in result.error


def test_repeated_empty_deliverables_cannot_exhaust_forty_model_turns(tmp_path, monkeypatch):
    cfg = LLMConfig(base_url="https://example.test/v1", api_key="fixture-only", primary_model="deepseek-flash")
    session = CodegenSession(tmp_path)
    raw = {"choices": [{"message": {"role": "assistant", "content": "Finished"}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *a, **kw: Response(raw))
    loop = ClawTurnLoop(llm_config=cfg, workspace=tmp_path, system_prompt="Generate code", session=session, max_iterations=40)
    result = loop.run_turn("Implement the supplied plan")
    assert session.llm_calls == 3
    assert result.errors and not (tmp_path / "main.py").exists()


def test_generic_classification_does_not_authorize_a_different_builtin_dataset(tmp_path):
    from types import SimpleNamespace
    from researchclaw.pipeline.codegen.strategies.fallback import FallbackStrategy
    cfg = SimpleNamespace(research=SimpleNamespace(topic="Image classification on CIFAR-10"))
    ctx = CodegenContext(topic=cfg.research.topic, exp_plan="datasets: [CIFAR-10]", metric="accuracy",
        metric_direction="maximize", time_budget_sec=30, mode="sandbox")
    assert not FallbackStrategy._should_use_sklearn_builtin(ctx, cfg)
    assert not FallbackStrategy().generate(ctx, cfg, None, CodegenSession(tmp_path)).files
