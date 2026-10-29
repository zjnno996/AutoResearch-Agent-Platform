"""Production module boundaries and contracts exposed by the new composition."""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import json
import symtable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.evaluation.reviews import validate_review
from researchclaw.orchestration.artifacts import write_json
from researchclaw.orchestration.context import augment_system, skill_read_directories, stage_guidance
from researchclaw.orchestration.artifacts import Artifacts
from researchclaw.orchestration.rubric import _check
from researchclaw.observability.tracing import StageTrace
from researchclaw.pipeline.ideation.branches import _run_parallel
from researchclaw.pipeline.builtins import default_registry
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.runtime import default_bindings
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.steps import quality_gate
from researchclaw.pipeline.writing.outline import _execute_paper_outline
from researchclaw.pipeline.support.domain import _extract_keywords
from researchclaw.pipeline.support.artifacts import active_stage_glob, _find_prior_file, _read_prior_artifact
from researchclaw.pipeline.support.experiment_evidence import _collect_raw_experiment_metrics
from researchclaw.pipeline.support.research_memory import _load_ideation_memory, _resolve_shared_results_path, append_experience
from researchclaw.pipeline.support.templates import _default_quality_report, _generate_neurips_checklist


def test_all_production_stages_resolve_to_real_business_modules():
    registry = default_registry()
    assert set(registry.handlers) == set(Stage)
    assert default_bindings().registry.handlers is registry.handlers
    for stage, handler in registry.handlers.items():
        assert ".executor" not in handler.__module__
        assert any(f".{area}." in handler.__module__ for area in ("steps", "writing", "ideation"))
        inspect.signature(handler).bind_partial(None, None, None, None, llm=None, prompts=None)
    for item in registry.describe():
        assert item["inputs"] is not None and item["outputs"]


def test_business_modules_have_acyclic_dependencies_and_do_not_import_the_facade():
    root = Path(importlib.import_module("researchclaw.pipeline").__file__).parent
    module_paths = {"researchclaw.pipeline." + ".".join(p.relative_to(root).with_suffix("").parts): p
                    for area in ("steps", "writing", "ideation", "support")
                    for p in (root / area).glob("*.py") if p.name != "__init__.py"}
    graph = {}
    for module, path in module_paths.items():
        tree = ast.parse(path.read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert "researchclaw.pipeline.executor" not in imports, path
        if ".support." in module:
            assert not any(name and any(f".{area}." in name for area in ("steps", "writing", "ideation")) for name in imports), path
        graph[module] = {name for name in imports if name in module_paths}
    completed = set()
    def visit(node, active):
        assert node not in active, f"Cyclic business dependency: {active} -> {node}"
        if node in completed:
            return
        for dependency in graph[node]:
            visit(dependency, active | {node})
        completed.add(node)
    for module in graph:
        visit(module, set())


def test_execution_packages_use_shared_models_without_loading_the_facade():
    root = Path(importlib.import_module("researchclaw.pipeline").__file__).parent
    for area in ("codegen", "sanity_check", "experiment_run", "iterative_refine", "result_analysis"):
        for path in (root / area).rglob("*.py"):
            imports = [node.module for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom)]
            assert "researchclaw.pipeline.executor" not in imports, path


def test_split_modules_have_no_undefined_global_dependencies():
    root = Path(importlib.import_module("researchclaw.pipeline").__file__).parent
    def referenced_globals(table):
        names = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_global() and symbol.is_referenced()}
        for child in table.get_children():
            names.update(referenced_globals(child))
        return names
    for area in ("steps", "writing", "ideation", "support"):
        for path in (root / area).glob("*.py"):
            table = symtable.symtable(path.read_text(), str(path), "exec")
            defined = {symbol.get_name() for symbol in table.get_symbols()
                       if symbol.is_imported() or symbol.is_assigned() or symbol.is_namespace()}
            undefined = referenced_globals(table) - defined - set(dir(builtins)) - {"__name__", "__file__", "__package__"}
            assert not undefined, f"{path}: {sorted(undefined)}"


def test_shared_query_keywords_work_for_both_search_and_recent_discovery():
    words = _extract_keywords("机器人 navigation using imitation learning")
    assert "robotics" in words and "imitation" in words
    assert _extract_keywords("the study of graph attention") == ["graph", "attention"]


def test_parallel_branches_preserve_stage_guidance_and_skill_paths():
    with stage_guidance("Preserve every task requirement", read_dirs=("/trusted/skills",)):
        results = _run_parallel(lambda item: (augment_system(str(item)), skill_read_directories()), [1, 2, 3])
    assert all("Preserve every task requirement" in value[0] and value[1] == ("/trusted/skills",) for value in results.values())
    assert augment_system("after") == "after"


def test_legacy_ideation_memory_uses_the_same_project_topic_boundary(tmp_path):
    config = RCConfig.from_dict({
        "project": {"name": "memory-test", "mode": "full-auto"}, "research": {"topic": "topic A"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": str(tmp_path / "kb")}, "llm": {"api_key_env": "UNSET_TEST_KEY"},
        "research_engine": {"enabled": True},
    }, check_paths=False)
    run_dir = tmp_path / "runs/one"
    run_dir.mkdir(parents=True)
    (run_dir.parent / "ideation_memory.md").write_text("unrelated shared research")
    assert _load_ideation_memory(config, run_dir) == ""
    memory_dir = _resolve_shared_results_path(config, run_dir)
    append_experience(memory_dir / "ideation_memory.md", "topic A local advice")
    assert "local advice" in _load_ideation_memory(config, run_dir)
    other = replace(config, research=replace(config.research, topic="topic B"))
    assert _load_ideation_memory(other, run_dir) == ""


def test_wildcard_evidence_reads_exclude_archives_future_stages_and_external_links(tmp_path):
    for name in ("stage-14", "stage-14_v1", "stage-23", "research_attempts/round-001/stage-14"):
        write_json(tmp_path / name / "runs/run.json", {"metrics": {"loss": 0.1}})
    outside = tmp_path / "outside.json"
    outside.write_text('{"metrics":{"loss":999}}')
    (tmp_path / "stage-14/runs/link.json").symlink_to(outside)
    with StageTrace(tmp_path, tmp_path / "stage-16", 16, "RESULT_ANALYSIS", "isolation"):
        matches = active_stage_glob(tmp_path, "stage-*/runs/*.json")
        assert matches == (tmp_path / "stage-14/runs/run.json",)
        assert active_stage_glob(tmp_path, "stage-23*") == ()
        assert [p.name for p in active_stage_glob(tmp_path, "stage-14*")] == ["stage-14"]
        assert json.loads(_read_prior_artifact(tmp_path, "runs/run.json"))["metrics"]["loss"] == 0.1
        assert _find_prior_file(tmp_path, "runs/link.json") is None
        assert _read_prior_artifact(tmp_path, "../../outside.json") is None
    (tmp_path / "stage-14/runs/run.json").unlink()
    with StageTrace(tmp_path, tmp_path / "stage-16", 16, "RESULT_ANALYSIS", "isolation"):
        assert _find_prior_file(tmp_path, "runs/run.json") is None


@pytest.mark.parametrize("report", [{}, {"score_1_to_10": "9", "verdict": "proceed"},
                                    {"score_1_to_10": True, "verdict": "proceed"},
                                    {"score_1_to_10": float("nan"), "verdict": "proceed"},
                                    {"score_1_to_10": 20, "verdict": "proceed"},
                                    {"score_1_to_10": 9}, {"score_1_to_10": 9, "verdict": "whatever"}])
def test_malformed_reviews_never_establish_a_quality_score(report):
    validated = validate_review(report)
    assert validated["review_status"] == "unavailable"
    assert validated["verdict"] == "revise" and validated["score_1_to_10"] == 0
    assert validated["strengths"] == []


def test_fallback_review_and_submission_form_do_not_invent_affirmative_answers():
    report = _default_quality_report(7)
    assert report["review_status"] == "unavailable" and report["criteria"] == {}
    checklist = _generate_neurips_checklist()
    assert "Answer: [Yes]" not in checklist
    assert "Not assessed" in checklist


def test_evidence_verifier_rejects_out_of_range_or_unavailable_review_scores(tmp_path):
    write_json(tmp_path / "stage-23/quality_report.json", {"score_1_to_10": 100, "verdict": "proceed"})
    assert not _check({"check": "quality", "evidence_paths": []}, Artifacts(tmp_path))[0]
    write_json(tmp_path / "stage-23/quality_report.json", {
        "score_1_to_10": 9, "verdict": "proceed", "review_status": "unavailable"})
    assert not _check({"check": "quality", "evidence_paths": []}, Artifacts(tmp_path))[0]


@pytest.mark.parametrize("verdict,score", [("revise", 9), ("reject", 9), ("proceed", "9"), ("proceed", float("nan"))])
def test_quality_gate_honors_reviewer_rejection_and_rejects_invalid_scores(tmp_path, monkeypatch, verdict, score):
    config = RCConfig.from_dict({
        "project": {"name": "review-test", "mode": "full-auto"},
        "research": {"topic": "Controlled comparison", "graceful_degradation": False},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": str(tmp_path / "kb")}, "llm": {"api_key_env": "UNSET_TEST_KEY"},
    }, check_paths=False)
    stage_dir = tmp_path / "stage-23"
    stage_dir.mkdir()
    write_json(tmp_path / "stage-16/experiment_summary.json", {"metrics_summary": {"loss": 0.3}})
    monkeypatch.setattr(quality_gate, "_build_claim_integrity_report", lambda *a: {"status": "passed"})
    model = SimpleNamespace(chat=lambda *a, **kw: SimpleNamespace(content=json.dumps({"score_1_to_10": score, "verdict": verdict})))
    result = quality_gate._execute_quality_gate(stage_dir, tmp_path, config, AdapterBundle(), llm=model)
    assert result.status == StageStatus.FAILED


def test_valid_passing_quality_review_still_passes(tmp_path, monkeypatch):
    config = RCConfig.from_dict({
        "project": {"name": "review-pass", "mode": "full-auto"},
        "research": {"topic": "Controlled comparison"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": str(tmp_path / "kb")}, "llm": {"api_key_env": "UNSET_TEST_KEY"},
    }, check_paths=False)
    stage_dir = tmp_path / "stage-23"
    stage_dir.mkdir()
    write_json(tmp_path / "stage-16/experiment_summary.json", {"metrics_summary": {"loss": 0.3}})
    monkeypatch.setattr(quality_gate, "_build_claim_integrity_report", lambda *a: {"status": "passed"})
    model = SimpleNamespace(chat=lambda *a, **kw: SimpleNamespace(content='{"score_1_to_10": 8, "verdict": "pass"}'))
    result = quality_gate._execute_quality_gate(stage_dir, tmp_path, config, AdapterBundle(), llm=model)
    assert result.status == StageStatus.DONE and result.decision == "proceed"


def test_outline_keeps_baseline_scope_guard_after_module_extraction(tmp_path):
    config = RCConfig.from_dict({
        "project": {"name": "outline-test", "mode": "full-auto"}, "research": {"topic": "Reproduce a baseline"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "local"},
        "knowledge_base": {"root": str(tmp_path / "kb")}, "llm": {"api_key_env": "UNSET_TEST_KEY"},
    }, check_paths=False)
    stage_dir = tmp_path / "stage-19"
    stage_dir.mkdir()
    replies = iter(["## Method Name Proposal\nWe introduce **FOO**", "# Baseline reproduction\n## Methods\nRetain the source protocol."])
    model = SimpleNamespace(chat=lambda *a, **kw: SimpleNamespace(content=next(replies)))
    result = _execute_paper_outline(stage_dir, tmp_path, config, AdapterBundle(), llm=model)
    assert result.status == StageStatus.DONE
    assert (stage_dir / "outline.md").read_text().startswith("# Baseline reproduction")
