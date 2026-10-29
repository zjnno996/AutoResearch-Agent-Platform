"""Quality-first Qwen calls remain uncapped and survive healthy long streams."""
import copy
import io
import json
import urllib.request
from dataclasses import replace

import pytest

from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.llm.provider_policy import apply_output_policy, deployment_policy_identity
from researchclaw.orchestration.compact import CompactResearch


@pytest.fixture
def quality(monkeypatch):
    for name, value in {
        'EMNETS_PROVIDER_LOCK': '1', 'EMNETS_UNLIMITED_TOKENS': '1', 'EMNETS_QUALITY_FIRST': '1',
        'EMNETS_API_BASE': 'http://quality.test/v1', 'EMNETS_MODEL': 'Qwen3.8-27B-FP8',
        'EMNETS_API_KEY_ENV': 'QUALITY_FIXTURE_KEY', 'QUALITY_FIXTURE_KEY': 'fixture-only',
    }.items():
        monkeypatch.setenv(name, value)


def test_nested_token_limits_are_removed_without_mutating_shared_template(quality):
    template = {'enable_thinking': True, 'reasoning_effort': 'xhigh',
                'thinking_token_budget': 1, 'max_new_tokens': 1}
    original = copy.deepcopy(template)
    body = {'max_tokens': 1, 'max_completion_tokens': 1, 'max_output_tokens': 1,
            'max_new_tokens': 1, 'thinking_token_budget': 1, 'chat_template_kwargs': template}
    assert apply_output_policy(body) == {'chat_template_kwargs': {'enable_thinking': True, 'reasoning_effort': 'xhigh'}}
    assert template == original
    assert deployment_policy_identity()['quality_first'] is True


@pytest.mark.parametrize('stage,scientific,native_effort', [
    (8, False, None), (9, True, 'medium'), (9, False, 'medium'),
    (13, False, None), (25, False, None), (26, True, None),
])
def test_every_research_role_keeps_full_thinking_in_actual_request(quality, monkeypatch, tmp_path, stage, scientific, native_effort):
    from researchclaw.observability.tracing import StageTrace
    config = RCConfig.from_dict({'project': {'name': 'quality-fixture'},
        'research': {'topic': 'Source-grounded research'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': str(tmp_path/'kb')},
        'llm': {'api_key_env': 'QUALITY_FIXTURE_KEY'}, 'research_engine': {'artifact_reasoning_effort': 'none'}})
    client = LLMClient(LLMConfig(base_url='http://stale.test/v1', api_key='stale-fixture',
        extra_body={'chat_template_kwargs': {'enable_thinking': False}, 'max_tokens': 1}))
    observed = []
    def request(req, **kwargs):
        observed.append(json.loads(req.data))
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    engine = CompactResearch(tmp_path, 'quality', config, client)
    with StageTrace(tmp_path, tmp_path/f'stage-{stage:02d}', stage, 'quality', 'quality'):
        engine._json('Return the complete artifact', lambda value: None, system='Fixture',
                     scientific=scientific, native_effort=native_effort)
    body = observed[0]
    assert body['model'] == 'Qwen3.8-27B-FP8'
    assert body['chat_template_kwargs'] == {'enable_thinking': True, 'reasoning_effort': 'xhigh'}
    assert not {'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'max_new_tokens', 'thinking_token_budget'} & body.keys()
    assert (body['temperature'], body['top_p'], body['top_k']) == (1.0, .95, 20)


def test_retry_never_lowers_quality_first_scientific_effort(quality, monkeypatch):
    from researchclaw.llm.errors import GenerationDeadlineExceeded
    client = LLMClient(LLMConfig(base_url='http://stale.test/v1', api_key='stale-fixture',
        extra_body={'thinking': {'type': 'enabled'}}))
    client.config = replace(client.config, max_retries=2, retry_base_delay=0)
    observed = []
    def request(req, **kwargs):
        observed.append(json.loads(req.data))
        if len(observed) == 1:
            raise GenerationDeadlineExceeded('Fixture old total deadline')
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    assert client.chat([{'role': 'user', 'content': 'research'}]).content == 'done'
    assert [body['chat_template_kwargs']['reasoning_effort'] for body in observed] == ['xhigh', 'xhigh']


def test_native_tools_use_the_same_uncapped_high_effort_request(quality, monkeypatch):
    from researchclaw.llm.tool_client import call_tool_model
    observed = []
    def request(req, **kwargs):
        observed.append(json.loads(req.data))
        return io.BytesIO(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'complete'}, 'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    call_tool_model(LLMConfig(base_url='http://stale.test', api_key='stale-fixture',
        extra_body={'chat_template_kwargs': {'enable_thinking': False, 'thinking_token_budget': 1}}),
        'old-model', [{'role': 'user', 'content': 'Implement the scientific method'}], [])
    body = observed[0]
    assert body['model'] == 'Qwen3.8-27B-FP8'
    assert body['chat_template_kwargs'] == {'enable_thinking': True, 'reasoning_effort': 'xhigh'}
    assert not {'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'thinking_token_budget'} & body.keys()


def stream_fixture(monkeypatch, ticks, events):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __iter__(self):
            for tick, event in zip(ticks, events):
                clock[0] = tick
                yield b'data:' + json.dumps(event).encode()
    clock = [0.0]
    monkeypatch.setattr('researchclaw.llm.client.time.monotonic', lambda: clock[0])
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: Response())


def test_healthy_reasoning_outlives_total_deadline_without_losing_final_content(quality, monkeypatch):
    events = [{'choices': [{'delta': {'reasoning': 'Scientific reasoning.'}}]} for _ in range(3)]
    events += [{'choices': [{'delta': {'content': '{"complete":true}'}, 'finish_reason': 'stop'}]},
               {'choices': [], 'usage': {'completion_tokens': 40}}]
    stream_fixture(monkeypatch, [4, 8, 12, 16, 17], events)
    result = LLMClient._stream_read(urllib.request.Request('http://quality.test'), 10)
    assert result['choices'][0]['message']['content'] == '{"complete":true}'
    assert result['usage']['completion_tokens'] == 40


def test_keepalives_do_not_reset_semantic_inactivity_timeout(quality, monkeypatch):
    events = [{'choices': [{'delta': {'reasoning': 'A real step.'}}]}, {'choices': []}, {'choices': []}]
    stream_fixture(monkeypatch, [4, 8, 15], events)
    with pytest.raises(TimeoutError, match='semantic inactivity'):
        LLMClient._stream_read(urllib.request.Request('http://quality.test'), 10)


def test_unconfigured_runs_retain_existing_total_deadline(monkeypatch):
    monkeypatch.delenv('EMNETS_QUALITY_FIRST', raising=False)
    from researchclaw.llm.errors import GenerationDeadlineExceeded
    events = [{'choices': [{'delta': {'reasoning': 'A real step.'}}]} for _ in range(3)]
    stream_fixture(monkeypatch, [4, 8, 12], events)
    with pytest.raises(GenerationDeadlineExceeded):
        LLMClient._stream_read(urllib.request.Request('http://quality.test'), 10)


def test_method_draft_cache_separates_full_effort_from_medium(quality, monkeypatch, tmp_path):
    from researchclaw.orchestration.method_analysis import derive_method_draft
    contract = {'requirements': [{'id': 'R1', 'kind': 'proof'}]}
    value = {'scientific_analysis_markdown': 'Under invertibility, Ax=b implies x=A^-1 b.',
        'obligations': {'R1': {'status': 'conditional', 'statement': 'Unique solution',
            'conditions': ['A invertible'], 'evidence': 'Multiply by inverse.', 'remaining_work': ''}}}
    efforts = []
    def ask(prompt, validator, **kwargs):
        efforts.append(kwargs['native_effort'])
        validator(value)
        return value
    _, full_path = derive_method_draft(tmp_path, 'Solve Ax=b', contract, 'Supplied source', ask, formal=True)
    monkeypatch.delenv('EMNETS_QUALITY_FIRST')
    _, medium_path = derive_method_draft(tmp_path, 'Solve Ax=b', contract, 'Supplied source', ask, formal=True)
    assert full_path != medium_path and efforts == ['xhigh', 'medium']
