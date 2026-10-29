"""ResearchClawBench boundaries, output contract and official-scorer delegation."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.benchmarks import rcbench
from researchclaw.cli import main
from researchclaw.config import RCConfig
from researchclaw.orchestration.artifacts import read_json, write_json


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text('''project:
  name: adapter-test
research:
  topic: original topic
runtime:
  timezone: UTC
notifications:
  channel: local
knowledge_base:
  root: kb
llm:
  api_key_env: NO_BENCH_TEST_KEY
  api_key: secret-test-value
  base_url: https://example.test/v1
research_engine:
  enabled: true
''')
    return path


@pytest.fixture
def official_repo(tmp_path, monkeypatch):
    repo = tmp_path / "official"
    task = repo / "tasks/Math_001"
    write_json(task / "task_info.json", {"task": "Compute the requested result", "data": []})
    (task / "data").mkdir()
    (task / "data/input.csv").write_text("x,y\n1,2\n")
    (task / "related_work").mkdir()
    (task / "related_work/paper_000.pdf").write_bytes(b"%PDF reference fixture")
    write_json(task / "target_study/checklist.json", {"hidden": "must not enter agent workspace"})
    write_json(repo / "evaluation/agents.json", {"other_agent": {"cmd": "keep-original"}})
    (repo / "evaluation/run_task.py").write_text("# official fixture")
    monkeypatch.setattr(rcbench, "revision", lambda root: "fixed-revision")
    class Runner:
        def __init__(self, task_id, agent_cmd, agent_name):
            self.agent_name, self.task_id = agent_name, task_id
        def setup_workspace(self):
            self.workspace.mkdir(parents=True)
            for directory in ("data", "related_work"):
                shutil.copytree(task / directory, self.workspace / directory)
            for directory in ("report/images", "outputs", "code"):
                (self.workspace / directory).mkdir(parents=True, exist_ok=True)
            self.instructions_path.write_text("Compute the requested result from data/input.csv")
        def _write_meta(self, status, extra):
            write_json(self.meta_path, {"task_id": self.task_id, "run_id": self.run_id,
                                       "status": status, "agent_name": self.agent_name, **extra})
    monkeypatch.setattr(rcbench, "_official", lambda root, module: SimpleNamespace(TaskRunner=Runner))
    return repo


def test_prepare_creates_isolated_fresh_attempts_and_retains_input_hashes(official_repo, config_file):
    first = rcbench.prepare(official_repo, "Math_001", config_file)
    second = rcbench.prepare(official_repo, "Math_001", config_file)
    assert first != second
    assert not (first / "target_study").exists()
    assert read_json(first / "_meta.json")["status"] == "prepared"
    manifest = read_json(first / "_claw_benchmark.json")
    assert manifest["data_manifest"]["input.csv"]
    assert manifest["related_work_manifest"]["paper_000.pdf"]
    assert manifest["agent_source_sha256"] == rcbench.source_identity()
    assert "secret-test-value" not in (first / "_claw_benchmark.json").read_text()
    assert manifest["official_submission"] is False


def test_task_config_preserves_official_prompt_and_uses_only_current_inputs(official_repo, config_file):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    config = rcbench.task_config(RCConfig.load(config_file, check_paths=False), workspace)
    assert config.research.topic == (workspace / "INSTRUCTIONS.md").read_text()
    assert config.research.domains == ()
    assert config.experiment.datasets_dir == str(workspace / "data")
    assert config.experiment.codebases_dir == config.experiment.checkpoints_dir == ""
    assert config.research.reference_papers == (str(workspace / "related_work/paper_000.pdf"),)
    assert not config.research_engine.shared_memory
    assert config.knowledge_base.root.startswith(str(workspace))


def test_official_react_protocol_does_not_reach_scoped_planning_and_coding_calls(official_repo, config_file):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    instructions = ("## Role\nYou are an autonomous research agent.\n\n"
        "## Research Task\n### Task Description\nProve convergence under explicit assumptions.\n"
        "### Available Data Files\n- data/measurements.npy\n\n---\n\n"
        "## Execution Protocol\nEvery response must include at least one tool call.\n"
        "Never finish before report/report.md exists.\n\n"
        f"## Workspace\nYour workspace is `{workspace}`.\n"
        "data/ and related_work/ are read-only.\n"
        "### Deliverables\nAnalysis code, report/report.md and mandatory PNG validation plots.\n"
        "### Technical Notes\nEnsure reproducibility.\n")
    (workspace / "INSTRUCTIONS.md").write_text(instructions)
    projected = rcbench.task_config(RCConfig.load(config_file, check_paths=False), workspace)
    assert "Prove convergence under explicit assumptions." in projected.research.topic
    assert "data/measurements.npy" in projected.research.topic
    assert "mandatory PNG validation plots" in projected.research.topic
    assert "export destinations" in projected.research.topic
    assert "Every response must include" not in projected.research.topic
    assert "Never finish before" not in projected.research.topic
    assert str(workspace) not in projected.research.topic
    assert (workspace / "INSTRUCTIONS.md").read_text() == instructions
    assert "read-only" in rcbench.task_contract(instructions)["workspace_contract"]


def test_changed_official_layout_fails_instead_of_forwarding_session_policy():
    with pytest.raises(ValueError, match="cannot isolate scientific task"):
        rcbench.task_contract("## Role\nResearch agent\n## Execution Protocol\nAlways call tools")


@pytest.mark.parametrize("relative", ["INSTRUCTIONS.md", "data/input.csv", "related_work/paper_000.pdf"])
def test_changed_prepared_inputs_are_rejected_before_model_calls(official_repo, config_file, relative):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    with (workspace / relative).open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="changed since preparation"):
        rcbench.run_agent(workspace, config_file)


def test_registration_preserves_other_agents_and_quotes_paths(official_repo, config_file):
    rcbench.register(official_repo, config_file)
    presets = read_json(official_repo / "evaluation/agents.json")
    assert presets["other_agent"]["cmd"] == "keep-original"
    native = presets[rcbench.AGENT_ID]
    assert "rcbench agent" in native["cmd"] and "<WORKSPACE>" in native["cmd"] and "<PROMPT>" in native["cmd"]
    assert rcbench.source_identity()[:10] in native["label"]
    assert "secret-test-value" not in native["cmd"]


def _final_report(workspace):
    run = workspace / ".claw/run"
    (run / "stage-25").mkdir(parents=True)
    (run / "stage-16/charts").mkdir(parents=True)
    (run / "stage-11/experiment").mkdir(parents=True)
    (run / "stage-11/experiment/main.py").write_text("print('real computations belong here')")
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    (run / "stage-16/charts/figure.png").write_bytes(png)
    (run / "stage-25/paper_final.md").write_text("# Report\n## Method\nDescription\n![Observed values](charts/figure.png)\n## Discussion\nLimitations")
    write_json(run / "stage-16/experiment_summary.json", {"metrics": {"loss": 0.2}})
    return run


def test_report_export_rewrites_local_png_paths_and_preserves_evidence(official_repo, config_file):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    run = _final_report(workspace)
    result = rcbench.export_report(run, workspace)
    assert result["deliverables_complete"] and result["images"]
    assert "images/figure_" in (workspace / "report/report.md").read_text()
    assert (workspace / "report/images" / result["images"][0]).is_file()
    assert (workspace / "code/main.py").is_file()
    assert (workspace / "outputs/experiment_summary.json").is_file()


def test_missing_figures_are_not_fabricated_or_marked_complete(official_repo, config_file):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    run = _final_report(workspace)
    (run / "stage-16/charts/figure.png").unlink()
    result = rcbench.export_report(run, workspace)
    assert not result["deliverables_complete"]
    assert result["missing_images"] == ["charts/figure.png"]


def test_manifest_export_keeps_authored_code_and_excludes_derived_report_files(official_repo, config_file):
    from researchclaw.orchestration.compact_generation import record_sources
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    run = _final_report(workspace)
    code = run / 'stage-11/experiment'
    record_sources(code, ['main.py'])
    (code / 'report/images').mkdir(parents=True)
    (code / 'report/images/generated.png').write_bytes(b'generated binary')
    (code / 'derived.json').write_text('{"result":1}')
    assert rcbench.export_report(run, workspace)['deliverables_complete']
    assert (workspace / 'code/main.py').is_file()
    assert not (workspace / 'code/report').exists() and not (workspace / 'code/derived.json').exists()


def test_scoring_requires_independent_judge_configuration(official_repo, config_file, monkeypatch):
    for key in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    with pytest.raises(ValueError, match="independent judge"):
        rcbench.score(official_repo, workspace)
    assert not (workspace / "_score.json").exists()


@pytest.mark.parametrize("status", ["completed", "completed_with_gaps"])
def test_scoring_delegates_to_official_scorer_and_records_revision(official_repo, config_file, monkeypatch, status):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    write_json(workspace / "_meta.json", {"status": status, "task_id": "Math_001", "scientific_status": "gaps" if status.endswith("gaps") else "internally_accepted"})
    (official_repo / "evaluation/score.py").write_text("# unchanged official scoring implementation fixture")
    for key in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME"):
        monkeypatch.setenv(key, "separate-judge")
    monkeypatch.setattr(rcbench.importlib.util, "find_spec", lambda name: object())
    calls = []
    scorer = SimpleNamespace(score_workspace=lambda path: calls.append(path) or {
        "task_id": "Math_001", "total_score": 37.5,
        "items": [{"score": 37.5, "reasoning": "Concrete executed evidence", "weight": 1.0}]})
    monkeypatch.setattr(rcbench, "_official", lambda repo, module: scorer)
    result = rcbench.score(official_repo, workspace)
    assert calls == [workspace]
    assert result["score"] == 37.5 and result["evaluator_version"] == "fixed-revision"
    assert result["data_revision"] and result["agent_source_sha256"]
    assert result["scientific_status"] == ("gaps" if status.endswith("gaps") else "internally_accepted")
    assert not result["official_submission"]


def test_changed_agent_source_requires_a_fresh_attempt(official_repo, config_file, monkeypatch):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    monkeypatch.setattr(rcbench, "source_identity", lambda: "changed-source")
    with pytest.raises(ValueError, match="source changed"):
        rcbench.run_agent(workspace, config_file)


def test_source_changes_during_research_invalidate_the_attempt(official_repo, config_file, monkeypatch):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    original_identity = rcbench.source_identity()
    changed = []
    monkeypatch.setattr(rcbench, "source_identity", lambda: "different-version" if changed else original_identity)
    monkeypatch.setattr("researchclaw.llm.create_llm_client", lambda config: SimpleNamespace(preflight=lambda: (True, "OK")))
    def pipeline(**kwargs):
        changed.append(True)
        return []
    monkeypatch.setattr("researchclaw.pipeline.runner.execute_pipeline", pipeline)
    with pytest.raises(ValueError, match="source changed during research"):
        rcbench.run_agent(workspace, config_file)
    assert read_json(workspace / "_meta.json")["status"] == "failed"
    assert not (workspace / "external_evaluation.json").exists()


@pytest.mark.parametrize("mutate_inputs", [False, True])
def test_native_agent_updates_live_state_and_preserves_input_contract(official_repo, config_file, monkeypatch, mutate_inputs):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    monkeypatch.setattr("researchclaw.llm.create_llm_client", lambda config: SimpleNamespace(preflight=lambda: (True, "OK")))
    from researchclaw.pipeline.models import StageResult
    from researchclaw.pipeline.stages import Stage, StageStatus
    def pipeline(**kwargs):
        assert read_json(workspace / "_meta.json")["status"] == "running"
        assert "hidden" not in kwargs["config"].research.topic
        _final_report(workspace)
        if mutate_inputs:
            (workspace / "data/input.csv").write_text("changed input")
        return [StageResult(Stage.CITATION_VERIFY, StageStatus.DONE, ())]
    monkeypatch.setattr("researchclaw.pipeline.runner.execute_pipeline", pipeline)
    code = rcbench.run_agent(workspace, config_file)
    meta = read_json(workspace / "_meta.json")
    assert code == (1 if mutate_inputs else 0)
    assert meta["status"] == ("failed" if mutate_inputs else "completed")
    assert meta["inputs_unchanged"] is not mutate_inputs


def test_native_agent_retains_phase_error_before_attempting_export(official_repo, config_file, monkeypatch):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    monkeypatch.setattr("researchclaw.llm.create_llm_client", lambda config: SimpleNamespace(preflight=lambda: (True, "OK")))
    from researchclaw.pipeline.models import StageResult
    from researchclaw.pipeline.stages import Stage, StageStatus
    def pipeline(**kwargs):
        write_json(kwargs["run_dir"] / "compact_state.json", {
            "phases": {"verify": {"status": "failed", "failure_kind": "transport"}}})
        return [StageResult(Stage.CITATION_VERIFY, StageStatus.FAILED, (), error="provider stream interrupted")]
    monkeypatch.setattr("researchclaw.pipeline.runner.execute_pipeline", pipeline)
    assert rcbench.run_agent(workspace, config_file) == 1
    meta = read_json(workspace / "_meta.json")
    assert meta["status"] == "failed" and meta["failed_phase"] == "verify"
    assert meta["failure_kind"] == "transport" and meta["error"] == "provider stream interrupted"
    assert not (workspace / "report/report.md").exists()


@pytest.mark.parametrize("audit_passed,mutate_inputs", [(True, False), (False, False), (True, True)])
def test_scientific_gaps_can_be_graded_only_with_valid_unchanged_deliverables(
        official_repo, config_file, monkeypatch, audit_passed, mutate_inputs):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    monkeypatch.setattr("researchclaw.llm.create_llm_client", lambda config: SimpleNamespace(preflight=lambda: (True, "OK")))
    monkeypatch.setattr("researchclaw.benchmarks.partial_recovery.retained_review_audit",
                        lambda *args: {"eligible": audit_passed})
    from researchclaw.pipeline.models import StageResult
    from researchclaw.pipeline.stages import Stage, StageStatus
    def pipeline(**kwargs):
        run = _final_report(workspace)
        write_json(run / "compact_state.json", {"phases": {"verify": {"status": "failed", "failure_kind": "contract_or_execution"}}})
        write_json(run / "compact_review.json", {"criteria": [{"id": "R1", "status": "unmet"}]})
        write_json(run / "research_verification.json", {"all_satisfied": False})
        if mutate_inputs:
            (workspace / "data/input.csv").write_text("modified")
        return [StageResult(Stage.CITATION_VERIFY, StageStatus.FAILED, (),
                            error="Scientific review has unresolved requirements; see compact_review.json")]
    monkeypatch.setattr("researchclaw.pipeline.runner.execute_pipeline", pipeline)
    allowed = audit_passed and not mutate_inputs
    assert rcbench.run_agent(workspace, config_file) == (0 if allowed else 1)
    meta = read_json(workspace / "_meta.json")
    assert meta["status"] == ("completed_with_gaps" if allowed else "failed")
    assert meta.get("internal_verification") is not True
    assert read_json(workspace / ".claw/run/compact_state.json")["phases"]["verify"]["status"] == "failed"
    if allowed:
        assert meta["scientific_status"] == "gaps" and meta["pipeline_status"] == "failed"


def test_exported_code_runs_with_relative_input_and_output_bindings(official_repo, config_file):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    run = _final_report(workspace)
    source = run / "stage-11/experiment/main.py"
    source.write_text("from pathlib import Path\nr=Path(__file__).resolve().parent\n"
                      "(r/'outputs/result.txt').write_text((r/'data/input.csv').read_text())\n")
    assert rcbench.export_report(run, workspace)["deliverables_complete"]
    result = subprocess.run([sys.executable, str(workspace / "code/main.py")], cwd=workspace, capture_output=True)
    assert result.returncode == 0
    assert (workspace / "outputs/result.txt").read_text() == (workspace / "data/input.csv").read_text()
    assert (workspace / "code/data").is_symlink() and (workspace / "code/outputs").is_symlink()


def test_judge_parse_failure_is_not_saved_as_a_real_zero_score(official_repo, config_file, monkeypatch):
    workspace = rcbench.prepare(official_repo, "Math_001", config_file)
    write_json(workspace / "_meta.json", {"status": "completed", "task_id": "Math_001"})
    for key in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME"):
        monkeypatch.setenv(key, "separate-judge")
    monkeypatch.setattr(rcbench.importlib.util, "find_spec", lambda name: object())
    scorer = SimpleNamespace(score_workspace=lambda path: {
        "task_id": "Math_001", "total_score": 0,
        "items": [{"score": 0, "reasoning": "Failed to parse scoring response."}]})
    monkeypatch.setattr(rcbench, "_official", lambda *a: scorer)
    with pytest.raises(ValueError, match="retry scoring this retained report"):
        rcbench.score(official_repo, workspace)
    assert not (workspace / "external_evaluation.json").exists()


def test_task_identifiers_cannot_escape_task_root(official_repo, config_file):
    with pytest.raises(ValueError, match="task ID"):
        rcbench.prepare(official_repo, "../../other", config_file)


def test_cli_summary_has_no_invented_scores(official_repo, config_file, capsys):
    rcbench.prepare(official_repo, "Math_001", config_file)
    assert main(["rcbench", "summary", "--repo", str(official_repo)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["prepared"] == 1 and report["scored_attempts"] == 0
    assert report["attempts"][0]["score"] is None


def test_batch_fails_preflight_without_creating_billable_attempts(official_repo, config_file, monkeypatch):
    monkeypatch.setattr(rcbench, "doctor", lambda *a: {"configuration_ready_for_scoring": True})
    from researchclaw.llm import __name__ as llm_module
    monkeypatch.setattr(f"{llm_module}.create_llm_client", lambda config: SimpleNamespace(preflight=lambda: (False, "HTTP 502")))
    with pytest.raises(ValueError, match="no batch workspaces"):
        rcbench.batch(official_repo, config_file, ["Math_001"])
    assert not (official_repo / "workspaces").exists()


def test_transport_failed_attempt_can_resume_without_resetting_budget_or_artifacts(official_repo, config_file, monkeypatch):
    from researchclaw.pipeline.stages import Stage, StageStatus
    from researchclaw.pipeline.models import StageResult
    workspace = rcbench.prepare(official_repo, 'Math_001', config_file)
    preflights, invocations = [], []
    monkeypatch.setattr('researchclaw.llm.create_llm_client', lambda cfg: SimpleNamespace(
        preflight=lambda: (preflights.append(1) or True, 'OK')))
    def interrupted(**kwargs):
        run = kwargs['run_dir']
        budget = read_json(run/'model_call_budget.json')
        invocations.append(budget.get('attempts', 0))
        write_json(run/'model_call_budget.json', {'attempts': budget.get('attempts', 0)+1, 'limit': 48})
        write_json(run/'compact_state.json', {'phases': {'verify': {'status': 'failed',
            'failure_kind': 'transport', 'error': 'Model connection timed out'}}})
        retained = run/'retained-artifact.txt'
        if retained.exists():
            assert retained.read_text() == 'retained work'
        else:
            retained.write_text('retained work')
        return [StageResult(Stage.CITATION_VERIFY, StageStatus.FAILED, (), error='Model connection timed out')]
    monkeypatch.setattr('researchclaw.pipeline.runner.execute_pipeline', interrupted)
    assert rcbench.run_agent(workspace, config_file) == 1
    with pytest.raises(ValueError, match='new prepared workspace'):
        rcbench.run_agent(workspace, config_file)
    assert len(preflights) == 1
    assert rcbench.run_agent(workspace, config_file, resume=True) == 1
    assert invocations == [0, 1] and len(preflights) == 2
    event = read_json(workspace/'.claw/resume_history.json')['events'][0]
    assert event['calls_before'] == 1 and event['calls_after'] == 2 and event['status'] == 'failed'
    assert read_json(workspace/'_meta.json')['latency_comparable'] is False
    write_json(workspace/'.claw/run/model_call_budget.json', {'attempts': 48, 'limit': 48})
    with pytest.raises(ValueError, match='budget is exhausted'):
        rcbench.run_agent(workspace, config_file, resume=True)
    assert len(preflights) == 2


@pytest.mark.parametrize('failure_kind,status', [('contract_or_execution', 'failed'), ('transport', 'completed')])
def test_resume_cannot_repeat_a_scientific_failure_or_completed_result(official_repo, config_file, monkeypatch, failure_kind, status):
    workspace = rcbench.prepare(official_repo, 'Math_001', config_file)
    meta = read_json(workspace/'_meta.json');meta['status'] = status;write_json(workspace/'_meta.json', meta)
    write_json(workspace/'.claw/run/compact_state.json', {'phases': {'verify': {'status': 'failed', 'failure_kind': failure_kind}}})
    monkeypatch.setattr('researchclaw.llm.create_llm_client', lambda cfg: pytest.fail('Invalid resume must not call the provider'))
    with pytest.raises(ValueError, match='transport'):
        rcbench.run_agent(workspace, config_file, resume=True)


def test_resume_keeps_original_source_identity_guard(official_repo, config_file, monkeypatch):
    workspace = rcbench.prepare(official_repo, 'Math_001', config_file)
    monkeypatch.setattr(rcbench, 'source_identity', lambda: 'changed-source')
    with pytest.raises(ValueError, match='source changed'):
        rcbench.run_agent(workspace, config_file, resume=True)


@pytest.mark.parametrize("status", ["completed", "completed_with_gaps"])
def test_batch_keeps_failed_attempts_and_successful_scores(official_repo, config_file, monkeypatch, status):
    monkeypatch.setattr(rcbench, "doctor", lambda *a: {"configuration_ready_for_scoring": True})
    monkeypatch.setenv("JUDGE_MODEL_NAME", "fixed-independent-judge")
    monkeypatch.setattr("researchclaw.llm.create_llm_client", lambda config: SimpleNamespace(preflight=lambda: (True, "OK")))
    created = []
    class Process:
        def __init__(self, command, **kwargs):
            self.workspace = Path(command[-1])
            created.append(self.workspace)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self, timeout=None):
            return 1 if len(created) == 1 else 0
    original_prepare = rcbench.prepare
    def prepare(*args):
        workspace = original_prepare(*args)
        meta = read_json(workspace / "_meta.json")
        meta["status"] = status
        meta["scientific_status"] = "gaps" if status.endswith("gaps") else "internally_accepted"
        write_json(workspace / "_meta.json", meta)
        return workspace
    monkeypatch.setattr(rcbench, "prepare", prepare)
    monkeypatch.setattr(rcbench.subprocess, "Popen", Process)
    monkeypatch.setattr(rcbench, "score", lambda *a: {"score": 32.0})
    report = rcbench.batch(official_repo, config_file, ["Math_001"], repeats=2)
    assert report["status"] == "finished" and report["planned_attempts"] == 2
    assert [row["status"] for row in report["attempts"]] == ["failed", "scored"]
    assert report["attempts"][0]["score"] is None
    assert report["attempts"][1]["score"] == 32.0
    assert read_json(Path(report["ledger"]))["attempts"] == report["attempts"]


def test_stopping_a_benchmark_cleans_nested_tool_process_groups(tmp_path):
    script = ("import subprocess,sys,time\nfrom pathlib import Path\n"
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)\n"
              "Path('nested.pid').write_text(str(child.pid))\ntime.sleep(30)\n")
    process = subprocess.Popen([sys.executable, "-c", script], cwd=tmp_path, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "nested.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "nested.pid").exists()
        nested = int((tmp_path / "nested.pid").read_text())
        rcbench._terminate_run(process)
        assert process.returncode is not None
        stat = Path(f"/proc/{nested}/stat")
        try:
            nested_status = stat.read_text().split()[2]
        except FileNotFoundError:
            nested_status = None  # The child can be reaped between inspection and read.
        assert nested_status in {None, "Z"}
    finally:
        if process.poll() is None:
            rcbench._terminate_run(process)
