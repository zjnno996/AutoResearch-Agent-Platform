"""The planner, critic and repairer must use the controller's actual contract."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from researchclaw.config import RCConfig, ResearchEngineConfig
from researchclaw.orchestration.artifacts import read_json
from researchclaw.orchestration.compact import CompactResearch
from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.pipeline.stages import Stage, StageStatus


CONTRACT = {'requirements': [{'id': 'R1', 'requirement': 'Solve Ax=b', 'acceptance': 'Residual below 1e-10', 'instruction_quotes': ['Solve Ax=b']}]}
PLAN = {'method_steps': [], 'method_probes': [],
        'method_probe_note': 'Fixture isolates output-contract propagation; method execution is separately checked.',
        'methods': ['linear solve'], 'datasets': ['supplied matrix'], 'metrics': ['residual'],
        'protocol': {'command': 'python main.py'},
        'requirement_map': {'R1': {'approach': 'solve and measure', 'artifact': 'outputs/metrics.json', 'evidence_route': 'direct', 'must_preserve': 'Compute the linear solve'}}}


class Replies:
    def __init__(self, values):
        self.values, self.calls, self.config = list(values), [], SimpleNamespace()

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        value = self.values.pop(0)
        return value if isinstance(value, SimpleNamespace) else SimpleNamespace(
            content=json.dumps(value), truncated=False, finish_reason='stop')


def controller(tmp_path, llm, council=False, reference_papers=()):
    config = RCConfig.from_dict({'project': {'name': 'test'}, 'research': {'topic': 'Solve Ax=b numerically'},
        'llm': {'primary_model': 'test', 'api_key_env': 'TEST_KEY'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': '/tmp/compact-protocol-test-kb'}})
    config = replace(config, research_engine=ResearchEngineConfig(enabled=True, workflow='compact',
        skills_enabled=False, council_enabled=council, max_repair_rounds=0),
        research=replace(config.research, reference_papers=reference_papers))
    return CompactResearch(tmp_path, 'test', config, llm)


def test_plan_review_and_repair_receive_same_supported_artifact_contract(tmp_path):
    gap = {'critical_gaps': [{'field': 'execution_checks', 'gap': 'Need actual output checks', 'action': 'Add checks'}]}
    patch = {'replace_fields': {'execution_checks': [
        {'path': 'outputs/curves.npz', 'requirement_id': 'R1', 'required_arrays': ['residual'], 'finite_arrays': True},
        {'path': 'outputs/result.png', 'requirement_id': 'R1', 'image_format': 'PNG', 'nonempty': True}]}}
    llm = Replies([CONTRACT, PLAN, gap, patch])
    source = tmp_path / 'author-method.md'
    source.write_text('ORIGINAL_AUTHOR_METHOD: solve Ax=b and compute the residual under stated assumptions.')
    result = controller(tmp_path, llm, council=True, reference_papers=(str(source),)).run(Stage.TOPIC_INIT, Stage.EXPERIMENT_DESIGN)
    assert result[-1].status == StageStatus.DONE and len(llm.calls) == 4
    for messages in llm.calls[1:]:
        text = messages[0]['content']
        assert 'Controller execution_checks contract:' in text
        assert 'required_arrays' in text and 'image_format' in text
        assert 'ORIGINAL_AUTHOR_METHOD' in text
    assert 'ORIGINAL_AUTHOR_METHOD' not in llm.calls[0][0]['content']
    assert read_json(tmp_path / 'stage-09/plan_patch_candidate.json')['patch'] == patch


def test_provider_truncation_cannot_be_repaired_by_adding_delimiters(tmp_path):
    reply = SimpleNamespace(content='{"negative_result":true', truncated=True, finish_reason='length')
    llm = Replies([reply, reply])
    engine = controller(tmp_path, llm)
    with pytest.raises(ContractError, match='Truncated response'):
        engine._json('Return a result', lambda value: None, system='Return complete JSON')
    assert len(llm.calls) == 2 and not (tmp_path / 'compact_response_normalizations').exists()


def test_complete_missing_brace_keeps_negative_scientific_findings_without_retry(tmp_path):
    raw = '{"negative_result":true,"dimensions":{"method_validity":{"status":"gap"}}'
    llm = Replies([SimpleNamespace(content=raw, truncated=False, finish_reason='stop')])
    engine = controller(tmp_path, llm)
    value = engine._json('Return a result', lambda result: None, system='Return complete JSON')
    assert value['negative_result'] and value['dimensions']['method_validity']['status'] == 'gap'
    assert len(llm.calls) == 1
    record = read_json(next((tmp_path / 'compact_response_normalizations').glob('*.json')))
    assert record['content'] == raw and record['normalized'] == value


def test_instruction_induction_uses_raw_source_anchors_without_method_guidance(tmp_path):
    requirement = {'id': 'R1', 'requirement': 'Solve Ax=b', 'acceptance': 'Measured residual',
                   'instruction_sources': ['T1']}
    llm = Replies([{'requirements': [requirement]}])
    engine = controller(tmp_path, llm)
    engine._context = lambda phase: pytest.fail('Grounding must not import method/skill obligations')
    engine.ground()
    actual = read_json(tmp_path / 'compact_contract.json')
    assert len(llm.calls) == 1 and actual['requirements'][0]['instruction_quotes'] == ['Solve Ax=b numerically']
    prompt = llm.calls[0][0]['content']
    assert 'RAW ORIGINAL INSTRUCTION SOURCE FRAGMENTS' in prompt
    assert 'Match data, budgets' not in prompt and 'Study type:' not in prompt
