"""The deployment lock covers persisted configs, overrides and native tools."""
import io
import json

import pytest

from researchclaw.config import RCConfig
from researchclaw.llm import create_llm_client
from researchclaw.llm.client import LLMClient, LLMConfig, LLMResponse
from researchclaw.llm.provider_policy import apply_provider_policy


@pytest.fixture
def locked(monkeypatch):
    for name, value in {
        'EMNETS_PROVIDER_LOCK': '1', 'EMNETS_API_BASE': 'http://provider.test:9000/v1',
        'EMNETS_MODEL': 'Qwen3.8-27B-FP8', 'EMNETS_API_KEY_ENV': 'UNIT_PROVIDER_KEY',
        'UNIT_PROVIDER_KEY': 'new-fixture-key',
    }.items():
        monkeypatch.setenv(name, value)


def stale_config():
    return LLMConfig(base_url='https://old-provider.test/v1', api_key='old-fixture-key',
                     primary_model='old-model', fallback_models=['another-model'],
                     fallback_url='https://fallback.test/v1', fallback_api_key='fallback-fixture-key',
                     extra_headers={'Authorization': 'Bearer obsolete'},
                     extra_body={'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'})


def test_lock_changes_effective_configuration_without_mutating_original(locked):
    previous = stale_config()
    active = apply_provider_policy(previous)
    assert active.base_url == 'http://provider.test:9000/v1'
    assert active.primary_model == 'Qwen3.8-27B-FP8'
    assert active.api_key == 'new-fixture-key'
    assert active.fallback_models == [] and active.fallback_url == '' and active.fallback_api_key == ''
    assert active.extra_headers == {}
    assert active.extra_body == {'chat_template_kwargs': {'enable_thinking': True}, 'thinking_token_budget': 4096}
    assert previous.primary_model == 'old-model' and previous.extra_body['thinking']['type'] == 'enabled'


def test_explicit_chat_override_cannot_escape_the_deployment_lock(locked, monkeypatch):
    calls = []
    client = LLMClient(stale_config())
    def reply(model, *args):
        calls.append(model)
        return LLMResponse(content='ok', model=model)
    monkeypatch.setattr(client, '_call_with_retry', reply)
    assert client.chat([{'role': 'user', 'content': 'hello'}], model='old-requested-model').model == 'Qwen3.8-27B-FP8'
    assert calls == ['Qwen3.8-27B-FP8']


def test_failed_locked_request_never_tries_another_model(locked, monkeypatch):
    calls = []
    client = LLMClient(stale_config())
    def fail(model, *args):
        calls.append(model)
        raise RuntimeError('fixture service unavailable')
    monkeypatch.setattr(client, '_call_with_retry', fail)
    with pytest.raises(RuntimeError, match='fixture service unavailable'):
        client.chat([{'role': 'user', 'content': 'hello'}])
    assert calls == ['Qwen3.8-27B-FP8']


def test_native_tool_transport_uses_locked_model_endpoint_and_key(locked, monkeypatch):
    from researchclaw.llm.tool_client import call_tool_model
    requests = []
    def request(req, **kwargs):
        requests.append(req)
        return io.BytesIO(json.dumps({'model': 'Qwen3.8-27B-FP8',
             'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    call_tool_model(stale_config(), 'old-requested-model', [{'role': 'user', 'content': 'hello'}], [])
    assert len(requests) == 1
    assert requests[0].full_url == 'http://provider.test:9000/v1/chat/completions'
    assert json.loads(requests[0].data)['model'] == 'Qwen3.8-27B-FP8'
    assert requests[0].get_header('Authorization') == 'Bearer new-fixture-key'


def test_effective_rcconfig_and_factory_match_the_locked_model(locked):
    config = RCConfig.from_dict({'project': {'name': 'policy-fixture'},
        'research': {'topic': 'Solve the supplied mathematical problem'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': '/tmp/policy-fixture-kb'},
        'llm': {'provider': 'anthropic', 'base_url': 'https://old-provider.test',
                'primary_model': 'old-model', 'api_key': 'old-fixture-key', 'api_key_env': 'OLD_FIXTURE_KEY'}})
    assert config.llm.provider == 'openai-compatible'
    assert config.llm.primary_model == config.llm.coding_model == config.llm.image_model == 'Qwen3.8-27B-FP8'
    assert config.llm.base_url == 'http://provider.test:9000/v1'
    assert config.llm.api_key == '' and not config.llm.fallback_models
    client = create_llm_client(config)
    assert isinstance(client, LLMClient) and client._anthropic is None


def test_missing_private_key_fails_without_using_an_old_key(locked, monkeypatch):
    monkeypatch.delenv('UNIT_PROVIDER_KEY')
    with pytest.raises(ValueError, match='private API-key'):
        LLMClient(stale_config())


def test_unlocked_installations_keep_explicit_provider_settings(monkeypatch):
    monkeypatch.delenv('EMNETS_PROVIDER_LOCK', raising=False)
    config = stale_config()
    assert apply_provider_policy(config) is config


def test_quality_only_mode_omits_output_and_thinking_token_caps(locked, monkeypatch):
    from researchclaw.llm.provider_policy import apply_output_policy
    from researchclaw.llm.tool_client import tool_request
    monkeypatch.setenv('EMNETS_UNLIMITED_TOKENS','1')
    config=apply_provider_policy(stale_config())
    assert 'thinking_token_budget'not in config.extra_body
    assert apply_output_policy({'max_tokens':1,'max_completion_tokens':2,'thinking_token_budget':3,'model':'Qwen'}) == {'model':'Qwen'}
    _,body=tool_request(config,config.primary_model,[{'role':'user','content':'task'}],[])
    assert 'max_tokens'not in body and 'thinking_token_budget'not in body


def test_zero_call_quota_counts_requests_without_stopping_them(tmp_path):
    from researchclaw.llm.budget import ModelCallBudget
    budget=ModelCallBudget(tmp_path/'calls.json',0,8)
    for _ in range(30):budget.claim(9)
    assert budget.available(9) and budget.available(26)
    assert budget.state['attempts']==30 and budget.reserve==0


def test_artifact_schema_is_validated_locally_and_scoped_correction_is_merged(locked, monkeypatch, tmp_path):
    from researchclaw.observability.tracing import StageTrace
    from researchclaw.orchestration.compact import CompactResearch
    from researchclaw.orchestration.compact_contracts import ContractError
    monkeypatch.setenv('EMNETS_UNLIMITED_TOKENS', '1')
    config = RCConfig.from_dict({'project': {'name': 'schema-fixture'},
        'research': {'topic': 'Transport validation'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': str(tmp_path/'kb')},
        'llm': {'api_key_env': 'UNIT_PROVIDER_KEY'}})
    client = LLMClient(stale_config())
    observed = []
    replies = [{'ok': 'invalid-type'}, {'replace_fields': {'ok': True}}]
    def request(req, **kwargs):
        observed.append(json.loads(req.data))
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(replies.pop(0))}, 'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    engine = CompactResearch(tmp_path, 'schema-test', config, client)
    def validate(value):
        if value.get('ok') is not True:
            raise ContractError('The actual condition is not satisfied')
    schema = {'type': 'object', 'required': ['ok'], 'properties': {'ok': {'type': 'boolean'}}}
    with StageTrace(tmp_path, tmp_path/'stage-09', 9, 'schema', 'schema-test'):
        value = engine._json('Return an artifact', validate, system='Fixture', field_repair=True, response_schema=schema)
    assert value == {'ok': True}
    assert observed[0]['response_format'] == {'type': 'json_object'}
    assert json.dumps(schema) in observed[0]['messages'][-1]['content']
    assert observed[1]['response_format'] == {'type': 'json_object'}
    assert 'Artifact JSON Schema at /ok' in observed[1]['messages'][-1]['content']
    assert 'response_format' not in client.config.extra_body
    assert all('max_tokens' not in body and 'thinking_token_budget' not in body for body in observed)


def test_scoped_schema_repair_preserves_wire_fields_before_controller_expansion(locked, monkeypatch, tmp_path):
    from researchclaw.orchestration.compact import CompactResearch
    from researchclaw.orchestration.compact_contracts import ContractError
    config = RCConfig.from_dict({'project': {'name': 'anchor-fixture'},
        'research': {'topic': 'Transport validation'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': str(tmp_path/'kb')},
        'llm': {'api_key_env': 'UNIT_PROVIDER_KEY'}})
    client = LLMClient(stale_config())
    replies = [{'source': {'anchor_id': 'A1'}, 'ok': False}, {'replace_fields': {'ok': True}}]
    monkeypatch.setattr(client, 'chat', lambda *a, **kw: LLMResponse(content=json.dumps(replies.pop(0)), model='fixture'))
    engine = CompactResearch(tmp_path, 'anchor-test', config, client)
    schema = {'type': 'object', 'required': ['source', 'ok'], 'properties': {
        'source': {'type': 'object', 'required': ['anchor_id'], 'properties': {
            'anchor_id': {'enum': ['A1']}}, 'additionalProperties': False},
        'ok': {'type': 'boolean'}}}
    def validate(value):
        value['source']['quote'] = 'Controller-owned exact source'
        if not value['ok']:
            raise ContractError('scientific defect')
    value = engine._json('Return artifact', validate, system='Fixture', field_repair=True, response_schema=schema)
    assert value == {'source': {'anchor_id': 'A1', 'quote': 'Controller-owned exact source'}, 'ok': True}


def test_scoped_compiler_effort_keeps_the_scientific_client_at_xhigh(locked, monkeypatch, tmp_path):
    from researchclaw.observability.tracing import StageTrace
    from researchclaw.orchestration.compact import CompactResearch
    monkeypatch.setenv('EMNETS_UNLIMITED_TOKENS', '1')
    config = RCConfig.from_dict({'project': {'name': 'effort-fixture'},
        'research': {'topic': 'Compile an approved scientific argument'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': str(tmp_path/'kb')},
        'llm': {'api_key_env': 'UNIT_PROVIDER_KEY'}})
    client = LLMClient(stale_config())
    observed = []
    def request(req, **kwargs):
        observed.append(json.loads(req.data))
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    engine = CompactResearch(tmp_path, 'effort-test', config, client)
    with StageTrace(tmp_path, tmp_path/'stage-09', 9, 'compiler', 'effort-test'):
        engine._json('Compile the existing derivation', lambda value: None, system='Fixture', native_effort='medium')
        engine._json('Independently inspect the scientific argument', lambda value: None, system='Fixture', scientific=True)
    assert [body['chat_template_kwargs']['reasoning_effort'] for body in observed] == ['medium', 'xhigh']
    assert client.config.extra_body['chat_template_kwargs']['reasoning_effort'] == 'xhigh'
    assert all('thinking_token_budget' not in body and 'max_tokens' not in body for body in observed)


@pytest.mark.parametrize('generation_deadline,next_effort', [(True, 'medium'), (False, 'xhigh')])
def test_generation_deadline_adapts_only_the_owned_retry_without_mutating_other_calls(locked, monkeypatch, generation_deadline, next_effort):
    from dataclasses import replace
    from researchclaw.llm.errors import GenerationDeadlineExceeded
    monkeypatch.setenv('EMNETS_UNLIMITED_TOKENS', '1')
    client = LLMClient(stale_config())
    client.config = replace(client.config, max_retries=2, retry_base_delay=0)
    bodies = []
    def request(req, **kwargs):
        bodies.append(json.loads(req.data))
        if len(bodies) == 1:
            raise (GenerationDeadlineExceeded('Live generation deadline') if generation_deadline else TimeoutError('Socket timeout'))
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    assert json.loads(client.chat([{'role': 'user', 'content': 'Return the requested artifact'}]).content) == {'ok': True}
    assert [body['chat_template_kwargs']['reasoning_effort'] for body in bodies] == ['xhigh', next_effort]
    assert all(body['model'] == 'Qwen3.8-27B-FP8' for body in bodies)
    assert all('max_tokens' not in body and 'thinking_token_budget' not in body for body in bodies)
    assert client.config.extra_body['chat_template_kwargs']['reasoning_effort'] == 'xhigh'


@pytest.mark.parametrize('stage,effort', [(8, 'low'), (9, 'xhigh'), (25, 'xhigh'), (26, 'xhigh')])
def test_unlimited_qwen_uses_native_sampling_with_scoped_contract_effort(locked, monkeypatch, tmp_path, stage, effort):
    from researchclaw.observability.tracing import StageTrace
    from researchclaw.orchestration.compact import CompactResearch
    monkeypatch.setenv('EMNETS_UNLIMITED_TOKENS', '1')
    config = RCConfig.from_dict({'project': {'name': 'sampling-fixture'},
        'research': {'topic': 'A mathematical research task'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': str(tmp_path/'kb')},
        'llm': {'api_key_env': 'UNIT_PROVIDER_KEY'},
        'research_engine': {'artifact_reasoning_effort': 'high'}})
    client = LLMClient(stale_config())
    requests = []
    def request(req, **kwargs):
        requests.append(json.loads(req.data))
        return io.BytesIO(json.dumps({'model': 'Qwen3.8-27B-FP8',
            'choices': [{'message': {'role': 'assistant', 'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(LLMClient, '_urlopen', request)
    engine = CompactResearch(tmp_path, 'sampling-test', config, client)
    with StageTrace(tmp_path, tmp_path/f'stage-{stage:02d}', stage, 'test', 'sampling-test'):
        engine._json('Return JSON', lambda value: None, system='Fixture')
    body = requests[0]
    assert body['chat_template_kwargs'] == {'enable_thinking': True, 'reasoning_effort': effort}
    assert (body['temperature'], body['top_p'], body['top_k']) == (1.0, .95, 20)
    assert body['min_p'] == body['presence_penalty'] == 0.0 and body['repetition_penalty'] == 1.0
    assert not {'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'thinking_token_budget'} & body.keys()
    assert client.config.extra_body['chat_template_kwargs']['reasoning_effort'] == 'xhigh'


@pytest.mark.parametrize('mode,enabled', [('disabled', False), ('enabled', True)])
def test_qwen_independent_judge_options_are_explicit(monkeypatch, mode, enabled):
    from researchclaw.benchmarks.judge import judge_options
    monkeypatch.setenv('JUDGE_MODEL_NAME', 'Qwen3.8-27B-FP8')
    monkeypatch.setenv('JUDGE_THINKING', mode)
    assert judge_options() == {'chat_template_kwargs': {'enable_thinking': enabled}}


@pytest.mark.parametrize('stage,scientific,budget,thinking', [
    (8, False, 512, True), (13, False, 0, False), (25, False, 0, False),
    (25, True, 4096, True), (26, False, 4096, True),
])
def test_qwen_phase_allocations_preserve_scientific_reasoning(tmp_path, monkeypatch, stage, scientific, budget, thinking):
    from researchclaw.observability.tracing import StageTrace
    from researchclaw.orchestration.compact import CompactResearch
    config = RCConfig.from_dict({'project': {'name': 'thinking-fixture'}, 'research': {'topic': 'A numerical task'},
        'runtime': {'timezone': 'UTC'}, 'notifications': {'channel': 'console'},
        'knowledge_base': {'root': str(tmp_path/'kb')}, 'llm': {'api_key_env': 'TEST_KEY', 'primary_model': 'Qwen3.8-27B-FP8'}})
    client = LLMClient(LLMConfig(base_url='http://provider.test/v1', api_key='fixture',
        extra_body={'chat_template_kwargs': {'enable_thinking': True}, 'thinking_token_budget': 4096}))
    observed = []
    def reply(active, *args, **kwargs):
        observed.append(active.config.extra_body)
        return LLMResponse(content='{"ok":true}', model='Qwen3.8-27B-FP8')
    monkeypatch.setattr(LLMClient, 'chat', reply)
    engine = CompactResearch(tmp_path, 'thinking-test', config, client)
    with StageTrace(tmp_path, tmp_path/f'stage-{stage:02d}', stage, 'test', 'thinking-test'):
        engine._json('Return JSON', lambda value: None, system='Fixture', scientific=scientific)
    assert observed[0]['thinking_token_budget'] == budget
    assert observed[0]['chat_template_kwargs']['enable_thinking'] is thinking
    assert client.config.extra_body['thinking_token_budget'] == 4096
