"""Failure and end-to-end checks for the shorter production controller."""
from __future__ import annotations

import json
import io
import sys
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from researchclaw.config import RCConfig, ResearchEngineConfig
from researchclaw.orchestration.artifacts import project_document, read_json, write_json
from researchclaw.orchestration.compact import CompactResearch
from researchclaw.orchestration.compact_contracts import ContractError, apply_plan_patch
from researchclaw.orchestration.compact_revision import digest
from researchclaw.orchestration.compact_state import PhaseStore
from researchclaw.orchestration.compaction import compact_history
from researchclaw.orchestration.workspace import array_schema
from researchclaw.pipeline.stages import Stage, StageStatus


CONTRACT = {"requirements": [{"id": "R1", "requirement": "Solve Ax=b", "acceptance": "Residual below 1e-10", "instruction_quotes": ["Solve Ax=b"]}]}
PLAN = {"method_steps": [], "method_probes": [], "method_probe_note": "Fixture tests artifact control flow; method witnesses have separate end-to-end coverage.", "methods": ["numpy.linalg.solve"], "datasets": ["supplied matrix"], "metrics": ["residual"],
        "protocol": {"command": "python main.py"},
        "requirement_map": {"R1": {"approach": "solve and compute residual", "artifact": "outputs/metrics.json", "evidence_route": "direct", "must_preserve": "Compute the requested linear solve and residual"}}}
REVIEW = {"criteria": [{"id": "R1", "status": "met", "reason": "Executed residual is zero",
           "evidence": ["stage-11/experiment/outputs/metrics.json"], "evidence_route": "direct", "mechanism_match": "yes", "artifact_match": "complete"}], "missing_requirements": [],
           "quality_score": 8, "summary": "Small deterministic diagnostic only"}
BUNDLE = {"files": [{"path": "main.py", "content":
    "from pathlib import Path\nimport json\nfrom PIL import Image\n"
    "p=Path('outputs'); p.mkdir(exist_ok=True)\n"
    "(p/'metrics.json').write_text(json.dumps({'residual': 0.0}))\n"
    "(p/'analysis.md').write_text('Exact solve: x=(2,3), residual zero.')\n"
    "Image.new('RGB',(20,20),'blue').save(p/'result.png')\n"}]}
REPORT = {"report_markdown": "# Exact linear solve\n" + "The measured residual is zero. " * 15
          + "\n![Residual](../stage-11/experiment/outputs/result.png)\n"}
REPORT_EDIT = {"edits": [{"path": "paper_final.md", "sha256": digest(REPORT["report_markdown"]),
                         "old": "# Exact linear solve", "new": "# Verified linear solve"}]}


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.config = SimpleNamespace()

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=json.dumps(reply), truncated=False, finish_reason="stop")


@pytest.mark.parametrize('regressed', [True, False])
def test_scientific_revision_is_compared_as_a_whole_reviewed_bundle(tmp_path, cfg, regressed):
    from researchclaw.benchmarks.partial_recovery import retained_review_audit
    cfg = replace(cfg, research=replace(cfg.research,
        topic='Solve Ax=b numerically and produce an independent diagnostic'))
    contract = {'requirements': [*CONTRACT['requirements'], {
        'id': 'R2', 'requirement': 'produce an independent diagnostic', 'acceptance': 'Actual diagnostic output',
        'instruction_quotes': ['produce an independent diagnostic']}]}
    plan = {**PLAN, 'requirement_map': {**PLAN['requirement_map'],
        'R2': {'approach': 'Produce a measured diagnostic', 'artifact': 'outputs/diagnostic.csv',
               'evidence_route': 'direct', 'must_preserve': 'Measured diagnostic'}}}
    second = {**REVIEW['criteria'][0], 'id': 'R2', 'status': 'unmet', 'owner_phase': 'implement',
              'reason': 'The diagnostic output is absent'}
    first_review = {**REVIEW, 'criteria': [REVIEW['criteria'][0], second]}
    candidate = BUNDLE['files'][0]['content']
    if regressed:
        candidate = candidate.replace("'residual': 0.0", "'residual': 0.5")
    candidate += "(p/'diagnostic.csv').write_text('condition,value\\nmeasured,1\\n')\n"
    patch = {'edits': [{'path': 'main.py', 'sha256': digest(BUNDLE['files'][0]['content']),
                        'old': BUNDLE['files'][0]['content'], 'new': candidate}],
             'add_files': [{'path': 'companion.py', 'content': 'DIAGNOSTIC_VERSION = 1\n'}]}
    after = {**REVIEW, 'quality_score': 9 if regressed else 7,
             'criteria': [{**REVIEW['criteria'][0], 'status': 'unmet' if regressed else 'met',
                            'reason': 'Residual exceeds tolerance' if regressed else 'Residual remains zero'},
                           {**REVIEW['criteria'][0], 'id': 'R2',
                            'evidence': ['stage-11/experiment/outputs/diagnostic.csv']}]}
    llm = FakeLLM([contract, plan, BUNDLE, REPORT, first_review, patch, REPORT_EDIT, after,
                   *([first_review] if regressed else [])])
    engine = CompactResearch(tmp_path, 'test', cfg, llm)
    result = engine.run(Stage.TOPIC_INIT, None)
    assert len(llm.calls) == (9 if regressed else 8)
    state = read_json(tmp_path / 'compact_state.json')
    assert len([row for row in state['attempts'] if row['phase'] == 'execute']) == 2
    work = tmp_path / 'stage-11/experiment'
    if regressed:
        assert result[-1].status == StageStatus.FAILED
        assert read_json(work / 'outputs/metrics.json')['residual'] == 0
        assert (work / 'main.py').read_text() == BUNDLE['files'][0]['content']
        assert not (work / 'companion.py').exists() and not (work / 'outputs/diagnostic.csv').exists()
        assert read_json(tmp_path / 'research_verification.json')['all_satisfied'] is False
        assert read_json(tmp_path / 'compact_review.json')['criteria'][0]['status'] == 'met'
        assert read_json(tmp_path / 'compact_review.json')['criteria'][1]['status'] == 'unmet'
        assert state['phases']['verify']['status'] == 'failed'
        assert all(state['phases'][name]['status'] == 'done' for name in ['ground','plan','implement','execute','report'])
        assert len(list((tmp_path / 'research_reviews').glob('*/review.json'))) == 2
        assert retained_review_audit(tmp_path)['eligible']
        assert list((tmp_path / 'bundle_publications').glob('*/before/*.bin'))
        assessment_path = tmp_path/read_json(tmp_path/'compact_retention.json')['decision']['fresh_baseline_assessment']
        assessment = read_json(assessment_path)
        assert assessment['review']['criteria'][0]['status'] == 'met'
        assert len(list((tmp_path/'retention_reassessments').glob('*.json'))) == 1
        packet = llm.calls[-1][0][0]['content'][0]['text']
        assert REPORT['report_markdown'] in packet
        assert '# Verified linear solve' not in packet
        fresh_bytes = assessment_path.read_bytes()
        assessment_path.write_text('{"changed":"assessment"}')
        assert not retained_review_audit(tmp_path)['eligible']
        assessment_path.write_bytes(fresh_bytes)
        assert retained_review_audit(tmp_path)['eligible']
        (work / 'outputs/unreviewed.txt').write_text('Not part of the reviewed bundle')
        assert not retained_review_audit(tmp_path)['eligible']
        (work / 'outputs/unreviewed.txt').unlink()
        metadata = tmp_path / 'stage-25/research_engine_summary.json'
        original_metadata = metadata.read_bytes()
        metadata.write_text('{"unreviewed":"changed metadata"}')
        assert not retained_review_audit(tmp_path)['eligible']
        metadata.write_bytes(original_metadata)
        assert retained_review_audit(tmp_path)['eligible']
    else:
        assert result[-1].status == StageStatus.DONE
        assert read_json(tmp_path / 'research_verification.json')['all_satisfied'] is True
        assert (work / 'companion.py').is_file() and (work / 'outputs/diagnostic.csv').is_file()
        assert not (tmp_path / 'compact_retention.json').exists()


@pytest.fixture
def cfg():
    config = RCConfig.from_dict({"project": {"name": "test"}, "research": {"topic": "Solve Ax=b numerically"},
                               "llm": {"primary_model": "test", "api_key_env": "TEST_KEY"},
                               "runtime": {"timezone": "UTC"}, "notifications": {"channel": "console"},
                               "knowledge_base": {"root": "/tmp/compact-test-kb"}})
    return replace(config, research_engine=ResearchEngineConfig(enabled=True, workflow="compact", skills_enabled=False,
                    council_enabled=False, max_repair_rounds=1),
                   experiment=replace(config.experiment, mode="sandbox", time_budget_sec=10,
                       sandbox=replace(config.experiment.sandbox, python_path=sys.executable)))


@pytest.mark.parametrize('withdrawn', ['unmet', 'unverified'])
def test_truthful_scientific_correction_cannot_restore_a_discredited_historical_claim(tmp_path, cfg, withdrawn):
    from researchclaw.benchmarks.partial_recovery import retained_review_audit
    cfg = replace(cfg, research=replace(cfg.research,
        topic='Solve Ax=b numerically and produce an independent diagnostic'))
    contract = {'requirements': [*CONTRACT['requirements'], {
        'id': 'R2', 'requirement': 'produce an independent diagnostic', 'acceptance': 'Measured independent diagnostic',
        'instruction_quotes': ['produce an independent diagnostic']}]}
    plan = {**PLAN, 'requirement_map': {**PLAN['requirement_map'], 'R2': {
        'approach': 'Compute a diagnostic', 'artifact': 'outputs/diagnostic.csv',
        'evidence_route': 'direct', 'must_preserve': 'An executed diagnostic'}}}
    first = {**REVIEW, 'criteria': [REVIEW['criteria'][0], {**REVIEW['criteria'][0],
        'id': 'R2', 'status': 'unmet', 'reason': 'Diagnostic missing', 'owner_phase': 'implement'}]}
    revised_source = BUNDLE['files'][0]['content'] + "(p/'diagnostic.csv').write_text('condition,value\\nmeasured,1\\n')\n"
    patch = {'edits': [{'path': 'main.py', 'sha256': digest(BUNDLE['files'][0]['content']),
        'old': BUNDLE['files'][0]['content'], 'new': revised_source}]}
    gap = {**REVIEW['criteria'][0], 'status': withdrawn,
        'reason': 'A recorded constant residual does not establish that the requested system was solved.',
        'owner_phase': 'implement'}
    current = {**REVIEW, 'criteria': [gap, {**REVIEW['criteria'][0], 'id': 'R2',
        'evidence': ['stage-11/experiment/outputs/diagnostic.csv']}], 'quality_score': 7}
    fresh_baseline = {**first, 'criteria': [gap, first['criteria'][1]], 'quality_score': 5}
    llm = FakeLLM([contract, plan, BUNDLE, REPORT, first, patch, REPORT_EDIT, current, fresh_baseline])
    engine = CompactResearch(tmp_path, 'truthful-correction', cfg, llm)
    results = engine.run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.FAILED and len(llm.calls) == 9
    assert not (tmp_path/'compact_retention.json').exists()
    assert read_json(tmp_path/'compact_repair_baseline.json')['status'] == 'not_revalidated'
    assert read_json(tmp_path/'compact_review.json')['criteria'][0]['status'] == withdrawn
    assert read_json(tmp_path/'compact_review.json')['criteria'][1]['status'] == 'met'
    assert read_json(tmp_path/'research_verification.json')['all_satisfied'] is False
    assert (tmp_path/'stage-11/experiment/outputs/diagnostic.csv').is_file()
    assert '# Verified linear solve' in (tmp_path/'stage-25/paper_final.md').read_text()
    assert retained_review_audit(tmp_path)['eligible']
    # Original assessment remains historical; no archive is rewritten to make
    # the original false positive disappear from the audit history.
    archives = [read_json(p) for p in (tmp_path/'research_reviews').glob('*/review.json')]
    assert len(archives) == 2 and any(row['review']['criteria'][0]['status'] == 'met' for row in archives)


@pytest.mark.parametrize('changed', ['snapshot', 'current'])
def test_retention_reassessment_cannot_hide_concurrent_scientific_artifact_changes(tmp_path, cfg, changed, monkeypatch):
    cfg = replace(cfg, research=replace(cfg.research,
        topic='Solve Ax=b numerically and produce an independent diagnostic'))
    contract = {'requirements': [*CONTRACT['requirements'], {'id': 'R2',
        'requirement': 'produce an independent diagnostic', 'acceptance': 'Measured diagnostic',
        'instruction_quotes': ['produce an independent diagnostic']}]}
    plan = {**PLAN, 'requirement_map': {**PLAN['requirement_map'], 'R2': {
        'approach': 'Execute diagnostic', 'artifact': 'outputs/diagnostic.csv',
        'evidence_route': 'direct', 'must_preserve': 'Measured diagnostic'}}}
    first = {**REVIEW, 'criteria': [REVIEW['criteria'][0], {**REVIEW['criteria'][0],
        'id': 'R2', 'status': 'unmet', 'reason': 'Diagnostic missing', 'owner_phase': 'implement'}]}
    revised = BUNDLE['files'][0]['content'].replace("'residual': 0.0", "'residual': 0.5")
    revised += "(p/'diagnostic.csv').write_text('condition,value\\nmeasured,1\\n')\n"
    patch = {'edits': [{'path': 'main.py', 'sha256': digest(BUNDLE['files'][0]['content']),
        'old': BUNDLE['files'][0]['content'], 'new': revised}]}
    after = {**REVIEW, 'criteria': [{**REVIEW['criteria'][0], 'status': 'unmet',
        'reason': 'Residual is too large', 'owner_phase': 'implement'},
        {**REVIEW['criteria'][0], 'id': 'R2', 'evidence': ['stage-11/experiment/outputs/diagnostic.csv']}]}
    llm = FakeLLM([contract, plan, BUNDLE, REPORT, first, patch, REPORT_EDIT, after, first])
    original = llm.chat
    def chat(messages, **kwargs):
        content = messages[0]['content']
        text = content[0]['text'] if isinstance(content, list) else content
        if 'Independently assess only this immutable earlier scientific bundle' in text:
            base = next((tmp_path/'research_retention').glob('*/artifacts')) if changed == 'snapshot' else tmp_path
            (base/'stage-11/experiment/outputs/metrics.json').write_text('{"residual":123}')
        return original(messages, **kwargs)
    monkeypatch.setattr(llm, 'chat', chat)
    results = CompactResearch(tmp_path, 'retention-concurrent-change', cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.FAILED
    assert not (tmp_path/'compact_retention.json').exists()
    assert read_json(tmp_path/'compact_review.json')['criteria'][0]['status'] == 'unmet'
    assert (tmp_path/'stage-11/experiment/outputs/diagnostic.csv').is_file()
    error = read_json(tmp_path/'compact_state.json')['phases']['verify']['error'].lower()
    assert ('changed' in error if changed == 'snapshot' else 'execution/artifact checks' in error)


def test_six_phases_execute_and_resume_without_model_calls(tmp_path, cfg):
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, REVIEW])
    engine = CompactResearch(tmp_path, "test", cfg, llm)
    results = engine.run(Stage.TOPIC_INIT, None)
    assert [int(row.stage) for row in results] == [8, 9, 13, 18, 25, 26]
    assert all(row.status == StageStatus.DONE for row in results)
    assert len(llm.calls) == 5  # no per-file generation or writing tool loop
    receipt = read_json(tmp_path / "stage-16/experiment_provenance.json")
    assert receipt["success"] and receipt["returncode"] == 0 and receipt["source_hashes"]["main.py"]
    assert (tmp_path / receipt["source_archive"] / "main.py").is_file()
    resumed = CompactResearch(tmp_path, "test", cfg, FakeLLM([])).run(Stage.CITATION_VERIFY, None)
    assert resumed[-1].status == StageStatus.DONE
    assert len(read_json(tmp_path / "compact_state.json")["attempts"]) == 6


def test_focused_method_argument_survives_protocol_compilation_and_resume(tmp_path, cfg):
    cfg = replace(cfg, research=replace(cfg.research, topic='Prove that Solve Ax=b has a unique solution.'),
        research_engine=replace(cfg.research_engine, method_analysis_first=True))
    draft = {'scientific_analysis_markdown': 'Under invertibility, multiply Ax=b by A^-1. The solution is unique.',
        'obligations': {'R1': {'status': 'conditional', 'statement': 'Unique solution',
            'conditions': ['A is invertible'], 'evidence': 'The explicit inverse argument.', 'remaining_work': ''}}}
    llm = FakeLLM([CONTRACT, draft, PLAN])
    engine = CompactResearch(tmp_path, 'method-first', cfg, llm)
    engine.ground()
    outputs = engine.plan()
    actual = yaml.safe_load((tmp_path / 'stage-09/exp_plan.yaml').read_text())
    assert actual['scientific_analysis_markdown'] == draft['scientific_analysis_markdown']
    assert actual['method_draft']['obligations']['R1']['status'] == 'conditional'
    assert actual['method_draft']['artifact'] in outputs
    assert 'Initial focused scientific method draft' in llm.calls[2][0][0]['content']
    assert 'Study type: formal' in llm.calls[1][0][0]['content']
    assert (tmp_path / 'stage-09/scientific_analysis.md').read_text() == draft['scientific_analysis_markdown']
    assert len(llm.calls) == 3
    engine.plan()
    assert len(llm.calls) == 3
    old_guidance = engine._method_draft_guidance()
    write_json(tmp_path/'compact_revision.json', {'status': 'pending', 'signature': 'new-evidence',
        'gaps': [{'requirement_id': 'R1', 'reason': 'The stated invertibility condition is not established'}]})
    repaired_guidance = engine._method_draft_guidance()
    assert repaired_guidance != old_guidance and 'invertibility condition' in repaired_guidance


def test_method_source_ids_are_sealed_before_design_patch_validation_and_reused_without_calls(tmp_path, cfg):
    import copy
    from researchclaw.orchestration.source_anchors import build_source_anchors
    cfg = replace(cfg, research=replace(cfg.research, topic='Solve Ax=b numerically.\nA is invertible.'),
        research_engine=replace(cfg.research_engine, method_analysis_first=True, council_enabled=True))
    draft = {'scientific_analysis_markdown': 'Under invertibility, multiply Ax=b by A^-1 to obtain the unique solution.',
        'obligations': {'R1': {'status': 'conditional', 'statement': 'Unique linear solve',
            'conditions': ['A is invertible'], 'evidence': 'Multiply by the inverse.', 'remaining_work': ''}}}
    catalog = build_source_anchors(cfg.research.topic, [], draft['scientific_analysis_markdown'])
    ids = [row['id'] for row in catalog['anchors']]
    assert len(ids) == 2
    plan = {**PLAN, 'method_steps': [{'id': 'M1', 'requirement_ids': ['R1'], 'operation': 'Solve a*x=b',
        'inputs': 'Scalar nonzero a and finite b', 'outputs': 'Scalar x', 'assumptions': ['a is nonzero'],
        'source': {'anchor_id': ids[0]}, 'binding': {'module': 'scientific_methods.py', 'callable': 'solve_scalar'}}],
        'method_probes': [{'id': 'P1', 'method_step_id': 'M1', 'kind': 'analytic', 'basis': 'A scalar linear identity',
            'wrong_alternative': 'Multiplying a by b instead of dividing b by a',
            'code': 'def check(invoke):\n    assert invoke(2.,4.) == 2.\n'}]}
    changed = copy.deepcopy(plan['method_steps'])
    changed[0]['source'] = {'anchor_id': ids[1]}
    llm = FakeLLM([CONTRACT, draft, plan,
        {'critical_gaps': [{'field': 'method_steps', 'gap': 'Bind the explicit invertibility assumption',
                            'action': 'Select its original instruction source'}]},
        {'replace_fields': {'method_steps': changed}}])
    engine = CompactResearch(tmp_path, 'source-anchor-design', cfg, llm)
    engine.ground()
    outputs = engine.plan()
    actual = yaml.safe_load(engine.plan_path.read_text())
    source = actual['method_steps'][0]['source']
    assert source['anchor_id'] == ids[1] and source['quote'] == 'A is invertible.' and source['reference'] == 'task'
    assert 'stage-09/method_source_anchors.json' in outputs
    assert len(llm.calls) == 5
    engine.plan()
    assert len(llm.calls) == 5


def test_initial_contract_uses_only_instruction_while_planning_keeps_observed_evidence(tmp_path, cfg):
    data = tmp_path / 'inputs'
    data.mkdir()
    (data / 'measurements.csv').write_text('NON_SCOPE_DATA,value\nsample,1\n')
    source = tmp_path / 'background.md'
    source.write_text('UNREQUESTED_SOURCE_ABLATION: an optional comparison from background literature.\n' * 8)
    cfg = replace(cfg, research=replace(cfg.research, reference_papers=[str(source)]),
        experiment=replace(cfg.experiment, datasets_dir=str(data)),
        research_engine=replace(cfg.research_engine, public_source_search=False))
    llm = FakeLLM([CONTRACT])
    root = tmp_path / 'run'
    engine = CompactResearch(root, 'test', cfg, llm)
    engine.ground()
    assert len(llm.calls) == 1
    messages, options = llm.calls[0]
    prompt = messages[0]['content']
    assert cfg.research.topic in prompt
    assert 'NON_SCOPE_DATA' not in prompt and 'UNREQUESTED_SOURCE_ABLATION' not in prompt
    assert str(source) not in prompt and 'Observed inputs' not in prompt
    assert 'Do not solve equations' in options['system']
    assert 'NON_SCOPE_DATA' in (root / 'stage-02/task_data_profile.json').read_text()
    assert 'UNREQUESTED_SOURCE_ABLATION' in engine._reference_context('plan')
    actual = read_json(root / 'compact_contract.json')
    assert actual['requirements'][0]['instruction_quotes'] == ['Solve Ax=b']
    assert len(actual['requirements']) == 1


def test_formal_decisions_receive_full_focal_source_but_translation_stays_small(tmp_path, cfg):
    cfg = replace(cfg, research=replace(cfg.research, topic='Prove that Solve Ax=b has a unique solution.'))
    text = 'SOURCE_START\n' + 'Exact assumptions and operator equations.\n' * 4000 + 'SOURCE_FINAL_PROOF\n'
    path = tmp_path / 'focal.md'
    path.write_text(text)
    write_json(tmp_path / 'compact_references.json', {'references': [
        {'source': 'focal', 'text_path': str(path), 'named_framework_matches': ['Method'], 'context': 'Abstract only.'}]})
    llm = FakeLLM([])
    engine = CompactResearch(tmp_path, 'test', cfg, llm)
    science = engine._reference_context('plan')
    translation = engine._reference_context('source_translation', scientific=False)
    assert text in science and len(translation) <= 20000 and not llm.calls
    records = [read_json(p) for p in (tmp_path / 'compact_source_context').glob('*.json')]
    assert len(records) == 2
    assert next(r for r in records if r['purpose'] == 'plan')['sources'][0]['full_cached_text']
    assert next(r for r in records if r['purpose'] == 'plan')['packet_sha256'] == digest(science)


def test_context_only_result_cannot_pass_with_high_score_or_trigger_format_retry(tmp_path, cfg):
    cfg = replace(cfg, research_engine=replace(cfg.research_engine, max_repair_rounds=0))
    optimistic = {**REVIEW, 'quality_score': 10, 'criteria': [
        {**REVIEW['criteria'][0], 'evidence_route': 'context_only', 'mechanism_match': 'partial'}]}
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, optimistic])
    result = CompactResearch(tmp_path, 'test', cfg, llm).run(Stage.TOPIC_INIT, None)
    assert result[-1].status == StageStatus.FAILED and len(llm.calls) == 5
    actual = read_json(tmp_path / 'compact_review.json')
    assert actual['criteria'][0]['status'] == 'unverified' and actual['criteria'][0]['model_status'] == 'met'
    archived = read_json(next((tmp_path / 'research_reviews').glob('*/review.json')))
    assert archived['model_review']['criteria'][0]['status'] == 'met'
    assert archived['review'] == actual
    assert read_json(tmp_path / 'stage-16/experiment_provenance.json')['success']
    state = read_json(tmp_path / 'compact_state.json')
    assert sum(row['phase'] == 'execute' for row in state['attempts']) == 1
    assert not (tmp_path / 'compact_rejected').exists()


def test_final_export_figure_alias_is_resolved_before_scientific_review(tmp_path, cfg):
    aliased = {"report_markdown": REPORT["report_markdown"].replace('../stage-11/experiment/outputs/result.png', 'images/result.png')}
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, aliased, REVIEW])
    assert CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    assert len(llm.calls) == 5
    assert '../stage-11/experiment/outputs/result.png' in (tmp_path / 'stage-25/paper_final.md').read_text()
    assert CompactResearch(tmp_path, "test", cfg, FakeLLM([])).run(Stage.CITATION_VERIFY, None)[-1].status == StageStatus.DONE


def test_provider_failure_retains_completed_prefix(tmp_path, cfg):
    first = CompactResearch(tmp_path, "test", cfg, FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, RuntimeError("HTTP 403 request-id=test")]))
    assert first.run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.FAILED
    state = read_json(tmp_path / "compact_state.json")
    assert state["phases"]["execute"]["status"] == "done"
    assert state["phases"]["verify"]["status"] == "failed"
    followup = FakeLLM([REVIEW])
    results = CompactResearch(tmp_path, "test", cfg, followup).run(Stage.CITATION_VERIFY, None)
    assert results[-1].status == StageStatus.DONE and len(followup.calls) == 1


def test_extra_root_brace_preserves_all_review_members_without_another_request(tmp_path, cfg):
    class MalformedReviewLLM(FakeLLM):
        def chat(self, messages, **kwargs):
            result = super().chat(messages, **kwargs)
            if 'criteria' in json.loads(result.content):
                result.content += ',"dimensions":{"method_validity":{"status":"gap","reason":"Unresolved science","owner_phase":"report"}}}'
            return result
    llm = MalformedReviewLLM([CONTRACT, PLAN, BUNDLE, REPORT, REVIEW])
    cfg = replace(cfg, research_engine=replace(cfg.research_engine, max_repair_rounds=0))
    result = CompactResearch(tmp_path, 'test', cfg, llm).run(Stage.TOPIC_INIT, None)[-1]
    assert result.status == StageStatus.FAILED and len(llm.calls) == 5
    assert read_json(tmp_path / 'compact_review.json')['dimensions']['method_validity']['status'] == 'gap'
    records = list((tmp_path / 'compact_response_normalizations').glob('*.json'))
    assert len(records) == 1 and 'content' in read_json(records[0])


def test_transient_review_failure_retries_only_review_in_same_run(tmp_path, cfg):
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, urllib.error.URLError("connection reset"), REVIEW])
    results = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.DONE and len(llm.calls) == 6
    state = read_json(tmp_path / "compact_state.json")
    assert state["transport_retries"] == {"verify": 1}
    assert [row["status"] for row in state["attempts"] if row["phase"] == "verify"] == ["failed", "done"]
    assert sum(row["phase"] == "execute" for row in state["attempts"]) == 1
    assert not state.get("repairs")


def test_provider_retry_budget_is_not_multiplied_by_the_phase_controller(tmp_path, cfg, monkeypatch):
    from researchclaw.llm.client import LLMClient, LLMConfig
    provider = LLMClient(LLMConfig(primary_model='unit', base_url='https://unit.invalid/v1', api_key='private-fixture',
                                   fallback_models=[], max_retries=2, retry_base_delay=0))
    attempts = []
    def unavailable(*args, **kwargs):
        attempts.append(1)
        raise urllib.error.URLError('network unavailable')
    monkeypatch.setattr(provider, '_raw_call', unavailable)
    monkeypatch.setattr('researchclaw.llm.client.time.sleep', lambda seconds: None)
    class FailureAfterArtifacts(FakeLLM):
        def chat(self, messages, **kwargs):
            if self.replies:
                return super().chat(messages, **kwargs)
            self.calls.append((messages, kwargs))
            return provider.chat(messages, **kwargs)
    llm = FailureAfterArtifacts([CONTRACT, PLAN, BUNDLE, REPORT])
    result = CompactResearch(tmp_path, 'test', cfg, llm).run(Stage.TOPIC_INIT, None)
    assert result[-1].status == StageStatus.FAILED and len(attempts) == 2 and len(llm.calls) == 5
    state = read_json(tmp_path / 'compact_state.json')
    assert state['phases']['verify']['failure_kind'] == 'transport'
    assert state['phases']['verify']['provider_retries_exhausted']
    assert sum(row['phase'] == 'verify' for row in state['attempts']) == 1
    assert read_json(tmp_path / 'stage-16/experiment_provenance.json')['success']


def test_runtime_repair_replaces_a_named_python_function_in_one_request(tmp_path, cfg):
    original = 'def main():\n    raise ValueError("local defect")\n\nmain()\n'
    replacement = 'def main():\n' + ''.join('    ' + line for line in BUNDLE['files'][0]['content'].splitlines(keepends=True))
    patch = {'symbol_edits': [{'path': 'stage-11/experiment/main.py', 'sha256': digest(original),
                              'symbol': 'main', 'new': replacement}]}
    llm = FakeLLM([CONTRACT, PLAN, {'files': [{'path': 'main.py', 'content': original}]}, patch, REPORT, REVIEW])
    result = CompactResearch(tmp_path, 'test', cfg, llm).run(Stage.TOPIC_INIT, None)
    assert result[-1].status == StageStatus.DONE and len(llm.calls) == 6
    assert read_json(tmp_path / 'stage-16/experiment_provenance.json')['success']
    assert not (tmp_path / 'compact_rejected').exists()


@pytest.mark.parametrize('missing', [False, True])
def test_exhausted_repair_allocation_retains_the_completed_assessment_without_another_call(tmp_path, cfg, missing):
    from researchclaw.llm.budget import model_call_budget, claim_model_call
    cfg = replace(cfg, research_engine=replace(cfg.research_engine, max_model_calls=12))
    gap = {**REVIEW, 'criteria': [{**REVIEW['criteria'][0], 'status': 'unmet',
                                 'owner_phase': 'implement', 'reason': 'Required experimental evidence remains incomplete'}],
           'missing_requirements': ['An omitted original requirement'] if missing else []}
    class BudgetedReplies(FakeLLM):
        def chat(self, messages, **kwargs):
            claim_model_call()
            return super().chat(messages, **kwargs)
    llm = BudgetedReplies([CONTRACT, PLAN, BUNDLE, REPORT, gap])
    with model_call_budget(tmp_path / 'model_call_budget.json', 12, final_reserve=8):
        result = CompactResearch(tmp_path, 'test', cfg, llm).run(Stage.TOPIC_INIT, None)
    assert result[-1].stage == Stage.CITATION_VERIFY and result[-1].status == StageStatus.FAILED
    assert len(llm.calls) == 5 and read_json(tmp_path / 'model_call_budget.json')['attempts'] == 5
    assert read_json(tmp_path / 'compact_review.json')['criteria'][0]['status'] == 'unmet'
    assert len(read_json(tmp_path / 'compact_contract.json')['requirements']) == 1
    assert not (tmp_path / 'compact_revision.json').exists()
    assert read_json(tmp_path / 'compact_repair_stop.json')['requested_target'] == ('plan' if missing else 'implement')
    assert read_json(tmp_path / 'stage-16/experiment_provenance.json')['success']
    from researchclaw.benchmarks.partial_recovery import retained_review_audit
    assert retained_review_audit(tmp_path)['eligible']


def test_transient_design_review_reuses_candidate_without_regenerating_plan(tmp_path, cfg):
    cfg = replace(cfg, research_engine=replace(cfg.research_engine, council_enabled=True))
    llm = FakeLLM([CONTRACT, PLAN, urllib.error.URLError("connection reset"), {"critical_gaps": []}])
    results = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, Stage.EXPERIMENT_DESIGN)
    assert results[-1].status == StageStatus.DONE and len(llm.calls) == 4
    state = read_json(tmp_path / "compact_state.json")
    assert state["transport_retries"] == {"plan": 1}
    assert "code/data -> ../data" in llm.calls[2][0][0]["content"]
    assert f"Runtime working directory: {tmp_path / 'stage-11/experiment'}" in llm.calls[2][0][0]["content"]
    assert sum(row["phase"] == "ground" for row in state["attempts"]) == 1
    assert yaml.safe_load((tmp_path / "stage-09/exp_plan.yaml").read_text()) == PLAN


@pytest.mark.parametrize("permanent", [False, True])
def test_transport_retry_is_bounded_and_does_not_retry_forbidden(tmp_path, cfg, permanent):
    failure = (urllib.error.HTTPError("https://fixture.test", 403, "forbidden", {}, None)
               if permanent else urllib.error.URLError("connection reset"))
    replies = [CONTRACT, PLAN, BUNDLE, REPORT, failure]
    if not permanent:
        replies.append(urllib.error.URLError("connection reset again"))
    llm = FakeLLM(replies)
    results = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.FAILED
    assert len(llm.calls) == (5 if permanent else 6)
    state = read_json(tmp_path / "compact_state.json")
    assert state["phases"]["execute"]["status"] == "done"
    assert state["phases"]["verify"]["failure_kind"] == ("contract_or_execution" if permanent else "transport")
    assert state.get("transport_retries", {}) == ({} if permanent else {"verify": 1})


@pytest.mark.parametrize("failure_at", ["design_review", "plan_patch"])
def test_planning_failure_reuses_base_plan_and_completed_review(tmp_path, cfg, failure_at):
    cfg = replace(cfg, research_engine=replace(cfg.research_engine, council_enabled=True))
    critical = {"critical_gaps": [{"field": "proof_obligations", "gap": "State assumptions", "action": "Add proof obligation"}]}
    replies = [CONTRACT, PLAN]
    if failure_at == "plan_patch":
        replies.append(critical)
    replies.append(RuntimeError("provider temporarily unavailable"))
    assert CompactResearch(tmp_path, "test", cfg, FakeLLM(replies)).run(Stage.TOPIC_INIT, Stage.EXPERIMENT_DESIGN)[-1].status == StageStatus.FAILED
    before = (tmp_path / "stage-09/plan_candidate.json").read_bytes()
    followup = FakeLLM([{"critical_gaps": []}] if failure_at == "design_review" else
                      [{"replace_fields": {"proof_obligations": ["State assumptions"]}}])
    assert CompactResearch(tmp_path, "test", cfg, followup).run(Stage.EXPERIMENT_DESIGN, Stage.EXPERIMENT_DESIGN)[-1].status == StageStatus.DONE
    assert len(followup.calls) == 1
    assert (tmp_path / "stage-09/plan_candidate.json").read_bytes() == before


def test_report_evidence_repair_does_not_regenerate_plan_or_code(tmp_path, cfg):
    bad = {**REVIEW, "criteria": [{**REVIEW["criteria"][0], "id": "R1", "status": "unmet", "reason": "Explain the residual",
            "evidence": [], "owner_phase": "report", "action": "Add explanation"}]}
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, bad, REPORT_EDIT, REVIEW])
    engine = CompactResearch(tmp_path, "test", cfg, llm)
    assert engine.run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    assert len(llm.calls) == 7
    state = read_json(tmp_path / "compact_state.json")
    assert sum(row["phase"] == "execute" for row in state["attempts"]) == 1


def test_run_relative_report_alias_does_not_add_a_model_request(tmp_path, cfg):
    bad = {**REVIEW, 'criteria': [{**REVIEW['criteria'][0], 'status': 'unmet', 'reason': 'Explain residual',
                                  'evidence': [], 'owner_phase': 'report', 'action': 'Add explanation'}]}
    patch = {'edits': [{**REPORT_EDIT['edits'][0], 'path': 'stage-25/paper_final.md'}]}
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, bad, patch, REVIEW])
    result = CompactResearch(tmp_path, 'test', cfg, llm).run(Stage.TOPIC_INIT, None)
    assert result[-1].status == StageStatus.DONE and len(llm.calls) == 7
    records = list((tmp_path / 'compact_response_normalizations').glob('scope-paths-*.json'))
    assert len(records) == 1 and read_json(records[0])['original'] == patch
    assert not (tmp_path / 'compact_rejected').exists()


def test_proof_only_method_gap_repairs_report_without_rerunning_experiment(tmp_path, cfg):
    bad = {**REVIEW, "criteria": [{**REVIEW["criteria"][0], "id": "R1", "status": "unmet", "reason": "Supply the missing proof step",
            "evidence": [], "owner_phase": "report", "action": "Complete the derivation"}],
           "dimensions": {"method_validity": {"status": "gap", "reason": "Proof step omitted"}}}
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, bad, REPORT_EDIT, REVIEW])
    assert CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    state = read_json(tmp_path / "compact_state.json")
    assert len(llm.calls) == 7
    assert sum(row["phase"] == "execute" for row in state["attempts"]) == 1
    assert state["repairs"][0]["target"] == "report"


def test_theory_revision_preserves_execution_and_initial_plan_on_resume(tmp_path, cfg):
    theory = "# Analysis\nEvery square matrix has an inverse.\n"
    corrected = "An invertible matrix has a unique solution x = A^{-1}b."
    plan = {**PLAN, "scientific_analysis_markdown": theory}
    report = {"report_markdown": REPORT["report_markdown"] + "\nEvery square matrix has an inverse.\n"}
    bad = {**REVIEW, "criteria": [{**REVIEW["criteria"][0], "id": "R1", "status": "unmet", "reason": "Incorrect invertibility assumption",
            "evidence": [], "owner_phase": "report", "action": "Correct the proof assumption"}]}
    patch = {"edits": [
        {"path": "paper_final.md", "sha256": digest(report["report_markdown"]),
         "old": "Every square matrix has an inverse.", "new": corrected},
        {"path": "scientific_analysis.md", "sha256": digest(theory),
         "old": "Every square matrix has an inverse.", "new": corrected}]}
    llm = FakeLLM([CONTRACT, plan, BUNDLE, report, bad, patch, REVIEW])
    assert CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    assert len(llm.calls) == 7
    assert (tmp_path / "stage-09/scientific_analysis.md").read_text() == theory
    assert corrected in (tmp_path / "stage-25/scientific_analysis.md").read_text()
    state = read_json(tmp_path / "compact_state.json")
    assert sum(row["phase"] == "execute" for row in state["attempts"]) == 1
    assert len(list((tmp_path / "research_reviews").glob("*/review.json"))) == 2
    assert CompactResearch(tmp_path, "test", cfg, FakeLLM([])).run(Stage.CITATION_VERIFY, None)[-1].status == StageStatus.DONE
    assert read_json(tmp_path / "compact_state.json")["attempts"] == state["attempts"]


def test_numerical_revision_edits_only_faulty_source_and_executes_again(tmp_path, cfg):
    original = BUNDLE["files"][0]["content"].replace("{'residual': 0.0}", "{'residual': 1.0}")
    original += "\n# Independently supported block\ndef stable_function():\n    return 42\n"
    bundle = {"files": [{"path": "main.py", "content": original}]}
    bad = {**REVIEW, "criteria": [{**REVIEW["criteria"][0], "id": "R1", "status": "unmet", "reason": "Residual computation is wrong",
            "evidence": [], "owner_phase": "implement", "action": "Correct measured residual"}]}
    source_patch = {"edits": [{"path": "main.py", "sha256": digest(original),
                              "old": "{'residual': 1.0}", "new": "{'residual': 0.0}"}]}
    llm = FakeLLM([CONTRACT, PLAN, bundle, REPORT, bad, source_patch, REPORT_EDIT, REVIEW])
    assert CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    expected = original.replace("{'residual': 1.0}", "{'residual': 0.0}")
    assert (tmp_path / "stage-11/experiment/main.py").read_text() == expected
    assert read_json(tmp_path / "stage-16/experiment_summary.json")["residual"] == 0
    state = read_json(tmp_path / "compact_state.json")
    assert sum(row["phase"] == "execute" for row in state["attempts"]) == 2
    assert len(llm.calls) == 8


def test_execution_failure_uses_one_source_patch_then_real_execution(tmp_path, cfg):
    original = "value = 1 / 0\n" + BUNDLE["files"][0]["content"]
    patch = {"edits": [{"path": "main.py", "sha256": digest(original), "old": "value = 1 / 0", "new": "value = 1 / 1"}]}
    llm = FakeLLM([CONTRACT, PLAN, {"files": [{"path": "main.py", "content": original}]}, patch, REPORT, REVIEW])
    assert CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    assert len(llm.calls) == 6
    receipts = [read_json(path) for path in (tmp_path / "research_attempts").glob("execution-*/receipt.json")]
    assert len(receipts) == 2 and sum(row["success"] for row in receipts) == 1
    assert read_json(tmp_path / "stage-16/experiment_provenance.json")["success"]
    assert (tmp_path / "stage-11/experiment/main.py").read_text() == original.replace("value = 1 / 0", "value = 1 / 1")


def test_runtime_configuration_can_be_corrected_without_rewriting_valid_code(tmp_path, cfg):
    original = "import json\nfrom pathlib import Path\nvalue=1/json.loads(Path('solver.cfg').read_text())['denominator']\n" + BUNDLE["files"][0]["content"]
    settings = '{"denominator":0}\n'
    patch = {"edits": [{"path": "solver.cfg", "sha256": digest(settings), "old": '"denominator":0', "new": '"denominator":1'}]}
    bundle = {"files": [{"path": "main.py", "content": original}, {"path": "solver.cfg", "content": settings}]}
    llm = FakeLLM([CONTRACT, PLAN, bundle, patch, REPORT, REVIEW])
    assert CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    assert len(llm.calls) == 6
    assert (tmp_path / "stage-11/experiment/main.py").read_text() == original
    assert read_json(tmp_path / "stage-16/experiment_provenance.json")["success"]


def test_exit_zero_without_primary_measurements_is_repaired_before_reporting(tmp_path, cfg):
    source = BUNDLE['files'][0]['content'].replace("{'residual': 0.0}", "{'rows': 10, 'estimate': None}")
    source += "(p/'annual.csv').write_text('method,value\\nnan,1\\n')\n"
    plan = {**PLAN, 'execution_checks': [{'path': 'outputs/metrics.json', 'requirement_id': 'R1',
                                        'finite_fields': ['/estimate']}]}
    patch = {'edits': [{'path': 'main.py', 'sha256': digest(source), 'old': "'estimate': None", 'new': "'estimate': 2.0"}]}
    llm = FakeLLM([CONTRACT, plan, {'files': [{'path': 'main.py', 'content': source}]}, patch, REPORT, REVIEW])
    results = CompactResearch(tmp_path, 'test', cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.DONE and len(llm.calls) == 6
    prompt = llm.calls[3][0][0]['content']
    assert 'Execution preconditions failed' in prompt and '/estimate' in prompt
    assert 'Actual produced output table schemas' in prompt and 'nan' in prompt and 'annual.csv' in prompt
    assert llm.calls[3][1]['max_tokens'] == cfg.llm.max_tokens
    audits = [read_json(path) for path in (tmp_path / 'research_attempts').glob('execution-*/execution_checks.json')]
    assert len(audits) == 2 and sum(audit['passed'] for audit in audits) == 1
    assert read_json(tmp_path / 'stage-16/execution_checks.json')['passed']


def test_runtime_patch_cannot_consist_only_of_a_scratch_probe(tmp_path, cfg):
    original = "value = 1 / 0\n" + BUNDLE["files"][0]["content"]
    probe = {"add_files": [{"path": "scratch/probe.py", "content": "print('inspection only')\n"}]}
    llm = FakeLLM([CONTRACT, PLAN, {"files": [{"path": "main.py", "content": original}]}, probe, probe])
    results = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.FAILED
    assert "failing source file" in results[-1].error and len(llm.calls) == 5
    assert not (tmp_path / "stage-11/experiment/scratch/probe.py").exists()
    assert (tmp_path / "stage-11/experiment/main.py").read_text() == original


def test_consecutive_runtime_failures_do_not_reuse_an_unrelated_committed_patch(tmp_path, cfg):
    cfg = replace(cfg, research_engine=replace(cfg.research_engine, max_repair_rounds=2))
    original = "first = 1 / 0\nsecond = 1 / 0\n" + BUNDLE["files"][0]["content"]
    first = original.replace("first = 1 / 0", "first = 1 / 1")
    patches = [{"edits": [{"path": "main.py", "sha256": digest(before), "old": old, "new": new}]} for before, old, new in (
        (original, "first = 1 / 0", "first = 1 / 1"), (first, "second = 1 / 0", "second = 1 / 1"))]
    llm = FakeLLM([CONTRACT, PLAN, {"files": [{"path": "main.py", "content": original}]}, *patches, REPORT, REVIEW])
    assert CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    assert len(llm.calls) == 7
    assert len(list((tmp_path / "research_attempts").glob("execution-*/receipt.json"))) == 3
    assert len(list((tmp_path / "research_revisions").glob("*/manifest.json"))) == 2
    assert CompactResearch(tmp_path, "test", cfg, FakeLLM([])).run(Stage.CITATION_VERIFY, None)[-1].status == StageStatus.DONE


def test_unsuccessful_source_edits_stop_at_the_runtime_repair_limit(tmp_path, cfg):
    cfg = replace(cfg, research_engine=replace(cfg.research_engine, max_repair_rounds=2))
    original = "# original\nvalue = 1 / 0\n" + BUNDLE["files"][0]["content"]
    first = original.replace("# original", "# first attempted repair")
    patches = [{"edits": [{"path": "main.py", "sha256": digest(before), "old": old, "new": new}]} for before, old, new in (
        (original, "# original", "# first attempted repair"), (first, "# first attempted repair", "# second attempted repair"))]
    llm = FakeLLM([CONTRACT, PLAN, {"files": [{"path": "main.py", "content": original}]}, *patches])
    result = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)[-1]
    assert result.status == StageStatus.FAILED and "ZeroDivisionError" in result.error
    assert len(llm.calls) == 5
    assert len(list((tmp_path / "research_attempts").glob("execution-*/receipt.json"))) == 3
    assert not (tmp_path / "stage-16/experiment_provenance.json").exists()


def test_committed_patch_is_reused_without_another_model_request(tmp_path, cfg):
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, REVIEW])
    engine = CompactResearch(tmp_path, "test", cfg, llm)
    assert engine.run(Stage.TOPIC_INIT, None)[-1].status == StageStatus.DONE
    write_json(tmp_path / "compact_revision.json", {"status": "pending", "signature": "one", "gaps": []})
    scope = tmp_path / "stage-25"
    engine.llm = FakeLLM([REPORT_EDIT])
    originals = {"paper_final.md": REPORT["report_markdown"]}
    engine._revise("report", scope, originals, lambda value: None)
    assert len(engine.llm.calls) == 1
    engine.llm = FakeLLM([])
    engine._revise("report", scope, {"paper_final.md": (scope / "paper_final.md").read_text()}, lambda value: None)
    assert engine.llm.calls == []


def test_scientific_analysis_changed_during_review_cannot_be_marked_accepted(tmp_path, cfg):
    plan = {**PLAN, "scientific_analysis_markdown": "# Assumptions\nA is invertible.\n"}
    class MutatingReviewer(FakeLLM):
        def chat(self, messages, **kwargs):
            response = super().chat(messages, **kwargs)
            if len(self.calls) == 5:
                (tmp_path / "stage-25/scientific_analysis.md").write_text("Altered during review")
            return response
    llm = MutatingReviewer([CONTRACT, plan, BUNDLE, REPORT, REVIEW])
    results = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.FAILED
    assert "changed during scientific review" in results[-1].error
    assert not (tmp_path / "stage-26/paper_final_verified.md").exists()


def test_plan_projection_preserves_late_keys_and_marks_omissions(tmp_path):
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump({"ablations": "x" * 20000, "baselines": ["reference"],
                                   "methods": ["ADMM"], "protocol": {"budget": 300}}))
    packet = project_document(path, 6000)
    assert all(field in packet for field in ("ablations", "baselines", "methods", "protocol"))
    assert "NOT evidence of absence" in packet and len(packet) <= 6000


def test_invalid_patch_preserves_valid_plan():
    before = json.dumps(PLAN)
    with pytest.raises(ContractError):
        apply_plan_patch(PLAN, {"replace_fields": {"requirement_map": {}}}, CONTRACT)
    assert json.dumps(PLAN) == before


def test_additional_proof_obligations_are_valid_without_regenerating_plan():
    patched = apply_plan_patch(PLAN, {"replace_fields": {"proof_obligations": ["State assumptions and prove the claimed bound"]}}, CONTRACT)
    assert patched["methods"] == PLAN["methods"] and patched["requirement_map"] == PLAN["requirement_map"]
    assert patched["proof_obligations"] and "proof_obligations" not in PLAN


def test_object_numpy_schema_is_inspected_without_arbitrary_unpickling(tmp_path):
    path = tmp_path / "problem.npy"
    np.save(path, {"A": np.eye(3), "b": np.ones(3)})
    result = array_schema(path)
    assert result["contents"]["A"]["shape"] == [3, 3]
    assert result["contents"]["b"]["sample_all_finite"] is True


def test_compaction_preserves_task_and_complete_tool_rounds(tmp_path):
    rows = [{"role": "user", "content": "Do ALL requested comparisons and proofs"}]
    for i in range(8):
        rows.extend([{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": '{"path":"main.py"}'}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000}])
    compacted = compact_history(rows, tmp_path, 15000)
    assert compacted[0] == rows[0]
    pending = set()
    for row in compacted:
        if row["role"] == "assistant":
            pending.update(call["id"] for call in row.get("tool_calls", []))
        elif row["role"] == "tool":
            assert row["tool_call_id"] in pending
            pending.remove(row["tool_call_id"])
    assert not pending and list(tmp_path.glob("history-*.json"))


def test_tampered_checkpoint_is_not_reused(tmp_path):
    store = PhaseStore(tmp_path, "test", "identity")
    (tmp_path / "plan.json").write_text("original")
    store.start("ground", "input")
    store.finish("ground", ["plan.json"])
    (tmp_path / "plan.json").write_text("tampered")
    assert not store.cached("ground", "input")


def test_http_error_retains_diagnostic_and_redacts_key():
    from researchclaw.llm.errors import capture_provider_error
    secret = "sk-fixture-secret-only"
    error = urllib.error.HTTPError("https://provider.test/v1", 403, "Forbidden", {"x-request-id": "req-123"},
        io.BytesIO(json.dumps({"error": {"code": "policy_rejected", "message": "Request denied " + secret}}).encode()))
    capture_provider_error(error, (secret,))
    assert secret not in str(error)
    assert "policy_rejected" in str(error) and "req-123" in str(error)
    assert error.code == 403


def test_failed_legacy_repair_preserves_previous_plan_and_open_actions(tmp_path):
    from researchclaw.orchestration.repair import begin_repair, complete_repair
    write_json(tmp_path / "stage-09/plan.json", {"methods": ["valid method"]})
    write_json(tmp_path / "research_verification.json", {"stage": 9, "summary": {"coverage": .75},
                                                       "criteria": [{"criterion_id": "C1"}]})
    begin_repair(tmp_path, {"from_stage": 9, "round": 1, "checkpoint_stage": 9,
                           "verification_before": {"coverage": .75}, "actions": [{"action": "Fix equation"}]}, "test")
    write_json(tmp_path / "stage-09/failure.json", {"error": "HTTP 403"})
    complete_repair(tmp_path, 9)
    assert read_json(tmp_path / "stage-09/plan.json")["methods"] == ["valid method"]
    attempt = read_json(tmp_path / "research_repair_history.json")["attempts"][0]
    assert not attempt["completed"] and attempt["retained"] == "previous"
    assert read_json(tmp_path / "research_revision_plan.json")["actions"]


def test_method_routing_distinguishes_improving_from_proving(cfg):
    from researchclaw.orchestration.methods import select_strategy
    improving = replace(cfg, research=replace(cfg.research, topic="Improving image classification"))
    proving = replace(cfg, research=replace(cfg.research, topic="Proving linear convergence of ADMM with Lyapunov analysis"))
    assert select_strategy(improving).study_type == "comparison"
    assert select_strategy(proving).study_type == "formal"


def test_provider_call_budget_is_persistent_and_reserves_delivery_calls(tmp_path):
    from researchclaw.llm.budget import ModelCallBudget, ModelCallBudgetExceeded
    path = tmp_path / "calls.json"
    budget = ModelCallBudget(path, 12, 4)
    for _ in range(8):
        budget.claim(13)
    with pytest.raises(ModelCallBudgetExceeded):
        budget.claim(13)
    resumed = ModelCallBudget(path, 12, 4)
    for _ in range(4):
        resumed.claim(26)
    assert read_json(path)["attempts"] == 12
    with pytest.raises(ModelCallBudgetExceeded):
        resumed.claim(26)


def test_report_section_reference_does_not_trigger_scientific_reexecution(tmp_path, cfg):
    review = {**REVIEW, "criteria": [{**REVIEW["criteria"][0],
              "evidence": ["COMPLETE report §1 and §4", "stage-11/experiment/outputs/metrics.json#residual"]}]}
    llm = FakeLLM([CONTRACT, PLAN, BUNDLE, REPORT, review])
    results = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.DONE and len(llm.calls) == 5
    assert read_json(tmp_path / "compact_state.json").get("repairs", []) == []


def test_source_bundle_rejects_input_overwrite_and_incomplete_python():
    from researchclaw.orchestration.compact_generation import validate_files
    for path, content in [("../data.npy", "x"), ("outputs/metrics.json", "{}"), ("main.py", "def broken(")]:
        with pytest.raises(ContractError):
            validate_files({"files": [{"path": path, "content": content}]})


def test_real_tool_transport_claims_each_provider_attempt(tmp_path, monkeypatch):
    from researchclaw.llm.budget import model_call_budget
    from researchclaw.llm.client import LLMClient, LLMConfig
    from researchclaw.llm.tool_client import call_tool_model
    from researchclaw.observability.tracing import StageTrace
    cfg = LLMConfig(base_url="https://fixture.test/v1", api_key="fixture", primary_model="deepseek-flash")
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}]}).encode()
    sent = []
    def request(*args, **kwargs):
        sent.append(True)
        if len(sent) == 1:
            raise urllib.error.HTTPError("https://fixture.test", 503, "temporary", {}, None)
        return Response()
    monkeypatch.setattr(LLMClient, "_urlopen", request)
    with model_call_budget(tmp_path / "budget.json", 12, 4):
        with StageTrace(tmp_path, tmp_path, 13, "implement", "test"):
            with pytest.raises(urllib.error.HTTPError):
                call_tool_model(cfg, "deepseek-flash", [{"role": "user", "content": "task"}], [])
            call_tool_model(cfg, "deepseek-flash", [{"role": "user", "content": "task"}], [])
    assert len(sent) == read_json(tmp_path / "budget.json")["attempts"] == 2


def test_late_reference_and_middle_theorem_are_not_lost():
    from researchclaw.orchestration.compact_generation import reference_packet, scientific_excerpt
    text = "Paper identity\n" + "Unrelated background. " * 1000 + "\nTHEOREM: ADMM Lyapunov linear convergence under strong convexity.\n" + "Appendix details. " * 1000
    excerpt = scientific_excerpt(text, "ADMM Lyapunov linear convergence", 5000)
    assert "THEOREM" in excerpt and "Paper identity" in excerpt
    packet = reference_packet({"references": [{"source": f"source_{i}.pdf", "context": "x" * 8000} for i in range(4)]}, 12000)
    assert all(f"source_{i}.pdf" in packet for i in range(4))


def test_configuration_changed_after_execution_cannot_pass_provenance(tmp_path, cfg):
    bundle = {"files": [*BUNDLE["files"], {"path": "solver_config.yaml", "content": "tolerance: 1e-10\n"}]}
    class Model(FakeLLM):
        def chat(self, messages, **kwargs):
            result = super().chat(messages, **kwargs)
            if len(self.calls) == 4:
                (tmp_path / "stage-11/experiment/solver_config.yaml").write_text("tolerance: 1\n")
            return result
    llm = Model([CONTRACT, PLAN, bundle, REPORT])
    results = CompactResearch(tmp_path, "test", cfg, llm).run(Stage.TOPIC_INIT, None)
    assert results[-1].status == StageStatus.FAILED and len(llm.calls) == 4
    assert "solver_config.yaml" in results[-1].error


@pytest.mark.parametrize("stage", [9, 13, 25, 26])
@pytest.mark.parametrize("effort", ["none", "low"])
def test_artifact_emission_does_not_reduce_scientific_review_effort(tmp_path,cfg,monkeypatch,stage,effort):
    from researchclaw.llm.client import LLMClient,LLMConfig
    from researchclaw.observability.tracing import StageTrace
    seen=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{"choices":[{"message":{"content":"{\\"valid\\":true}"},"finish_reason":"stop"}],"usage":{"total_tokens":10}}'
    def request(req,**kwargs):seen.append(json.loads(req.data));return Response()
    monkeypatch.setattr(LLMClient,"_urlopen",request)
    llm=LLMClient(LLMConfig(base_url="https://api.deepseek.com/v1",api_key="fixture",primary_model="deepseek-flash",
        extra_body={"thinking":{"type":"enabled"},"reasoning_effort":"high"}))
    engine=CompactResearch.__new__(CompactResearch)
    cfg=replace(cfg,research_engine=replace(cfg.research_engine,artifact_reasoning_effort=effort))
    engine.root,engine.config,engine.llm=tmp_path,cfg,llm
    with StageTrace(tmp_path,tmp_path,stage,"test","test"):
        assert engine._json("task",lambda value: None,system="schema")["valid"]
    if stage in {13,25} and effort=="none":
        assert seen[0]["thinking"]=={"type":"disabled"} and "reasoning_effort" not in seen[0]
    else:
        assert seen[0]["thinking"]=={"type":"enabled"}
        assert seen[0]["reasoning_effort"]==(effort if stage in {13,25} else "high")
    assert llm.config.extra_body["reasoning_effort"]=="high"
    assert llm.config.extra_body["thinking"]=={"type":"enabled"}


def test_relative_inputs_and_planned_scientific_arguments_work_without_repairs(tmp_path,cfg):
    data=tmp_path/'supplied';data.mkdir()
    np.save(data/'problem.npy',np.array([1.,2.,3.]))
    # This checks data paths and argument forwarding. Leave room for the real
    # NumPy/Pillow subprocess to start on a busy filesystem; deadline behavior
    # is exercised separately by the execution and method-pilot tests.
    cfg=replace(cfg,experiment=replace(cfg.experiment,datasets_dir=str(data),time_budget_sec=60))
    plan={**PLAN,'protocol':{'command':'python code/main.py --rounds 3 && python code/make_report.py'}}
    content=BUNDLE['files'][0]['content'].replace('import json','import json\nimport numpy as np\nimport sys')
    content=content.replace("{'residual': 0.0}","{'mean': float(np.load('data/problem.npy').mean()), 'rounds': int(sys.argv[2])}")
    bundle={'files':[{'path':'main.py','content':content}]}
    llm=FakeLLM([CONTRACT,plan,bundle,REPORT,REVIEW])
    run=tmp_path/'run'
    results=CompactResearch(run,'test',cfg,llm).run(Stage.TOPIC_INIT,None)
    assert results[-1].status==StageStatus.DONE and len(llm.calls)==5
    assert f"Runtime working directory: {run / 'stage-11/experiment'}" in llm.calls[2][0][0]['content']
    assert "ROOT=Path(__file__).resolve().parent, not parents[1]" in llm.calls[2][0][0]['content']
    metrics=read_json(run/'stage-16/experiment_summary.json')
    assert metrics=={'mean':2.,'rounds':3}
    receipt=read_json(run/'stage-16/experiment_provenance.json')
    assert '--rounds 3' in receipt['command'] and receipt['deferred_reporting_command']
    assert 'data/problem.npy' not in receipt['source_hashes']
    assert np.load(data/'problem.npy').tolist()==[1.,2.,3.]


def test_nonfinite_statistic_is_localized_without_fabricating_a_value(tmp_path):
    from researchclaw.orchestration.compact_contracts import metrics_gap
    path=tmp_path/'metrics.json'
    path.write_text('{"objective":1.2,"methods":{"APG":{"slope":NaN}}}')
    gap=metrics_gap(path)
    assert 'metrics.methods.APG.slope' in gap and 'Do not conceal' in gap
    assert 'NaN' in path.read_text()


def test_execution_finishes_watchdog_when_controller_returns(tmp_path,cfg):
    from researchclaw.orchestration.compact import execute_compact
    llm=FakeLLM([CONTRACT,PLAN,BUNDLE,REPORT,REVIEW])
    results=execute_compact(run_dir=tmp_path,run_id='test',config=cfg,from_stage=Stage.TOPIC_INIT,to_stage=None,llm=llm)
    assert results[-1].status==StageStatus.DONE
    assert read_json(tmp_path/'run_health.json')['status']=='controller_finished'
