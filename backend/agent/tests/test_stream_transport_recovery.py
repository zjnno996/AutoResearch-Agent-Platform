"""Actual observed failure patterns: empty streams and reasoning-budget exhaustion."""
from __future__ import annotations

import json
import http.client
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.orchestration.compact import CompactResearch
from researchclaw.orchestration.compact_contracts import ContractError


class Response:
    def __init__(self, lines):
        self.lines = lines
        self.headers = {"x-request-id": "fixture-stream"}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def __iter__(self):
        return iter(self.lines)


@pytest.mark.parametrize("prefix", [b"data: ", b"data:"])
def test_sse_spacing_does_not_discard_output_or_usage(monkeypatch, prefix):
    events = [{"model": "deepseek-flash", "choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}]
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *args, **kwargs: Response([prefix + json.dumps(row).encode() for row in events]))
    value = LLMClient._stream_read(urllib.request.Request("https://fixture.test"), 10)
    assert value["choices"][0]["message"]["content"] == '{"ok":true}'
    assert value["usage"]["total_tokens"] == 18


def test_plain_json_body_on_streaming_request_is_preserved(monkeypatch):
    value = {"model": "deepseek-flash", "choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *args, **kwargs: Response([json.dumps(value).encode()]))
    assert LLMClient._stream_read(urllib.request.Request("https://fixture.test"), 10) == value


def test_empty_stream_is_typed_transient_error_not_success(monkeypatch):
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *args, **kwargs: Response([b"data:[DONE]\n"]))
    with pytest.raises(urllib.error.HTTPError) as error:
        LLMClient._stream_read(urllib.request.Request("https://fixture.test"), 10)
    assert error.value.code == 502


def test_streamed_api_error_is_not_discarded(monkeypatch):
    value = {"error": {"code": "permission_denied", "message": "forbidden"}}
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *args, **kwargs: Response([b"data:" + json.dumps(value).encode()]))
    assert LLMClient._stream_read(urllib.request.Request("https://fixture.test"), 10) == value


def test_incomplete_stream_is_not_a_complete_artifact(monkeypatch):
    value = {"choices": [{"delta": {"content": '{"unfinished":'}}]}
    monkeypatch.setattr(LLMClient, "_urlopen", lambda *args, **kwargs: Response([b"data:" + json.dumps(value).encode()]))
    client = LLMClient(LLMConfig(base_url="https://fixture.test", api_key="fixture", primary_model="deepseek-flash"))
    response = client._raw_call("deepseek-flash", [{"role": "user", "content": "task"}], 100, 0, True)
    assert response.truncated and response.finish_reason == "stream_interrupted"


def test_truncated_structured_response_increases_only_current_request_budget(tmp_path):
    budgets = []
    class Model:
        config = SimpleNamespace(max_tokens=32768)
        def chat(self, messages, **kwargs):
            budgets.append(kwargs["max_tokens"])
            return SimpleNamespace(content='{"valid":true}', truncated=len(budgets) == 1, finish_reason="length" if len(budgets)==1 else "stop")
    engine = CompactResearch.__new__(CompactResearch)
    engine.root, engine.llm = tmp_path, Model()
    def validator(value):
        if value.get("valid") is not True:
            raise ContractError("invalid")
    assert engine._json("task", validator, system="schema", budget=6500)["valid"]
    assert budgets == [6500, 32768]


def test_incomplete_read_retries_without_regenerating_the_stage(monkeypatch):
    calls=[]
    payload={'choices':[{'message':{'content':'complete'},'finish_reason':'stop'}]}
    def request(*args,**kwargs):
        calls.append(True)
        if len(calls)==1:raise http.client.IncompleteRead(b'')
        return Response([json.dumps(payload).encode()])
    monkeypatch.setattr(LLMClient,'_urlopen',request)
    client=LLMClient(LLMConfig(base_url='https://fixture.test',api_key='fixture',primary_model='deepseek-flash',
                              max_retries=2,retry_base_delay=0))
    response=client.chat([{'role':'user','content':'task'}])
    assert response.content=='complete' and len(calls)==2
    from researchclaw.llm.retry import retryable_model_error
    assert retryable_model_error(http.client.IncompleteRead(b''))
    assert not retryable_model_error(TypeError('programming error'))


def test_scoped_json_correction_preserves_valid_science_and_revalidates_every_field(tmp_path):
    calls = []
    replies = [{'derivation': 'A valid argument that must survive unchanged.', 'tolerance': -1},
               {'replace_fields': {'tolerance': 1e-6}}]
    class Model:
        config = SimpleNamespace(max_tokens=32768)
        def chat(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(content=json.dumps(replies.pop(0)), truncated=False, finish_reason='stop')
    engine = CompactResearch.__new__(CompactResearch)
    engine.root, engine.llm = tmp_path, Model()
    checked = []
    def validate(value):
        checked.append(dict(value))
        if not (0 < value['tolerance'] < .01):
            raise ContractError('tolerance must be a small positive number')
    value = engine._json('Produce the scientific protocol', validate, system='Fixture', field_repair=True)
    assert value == {'derivation': 'A valid argument that must survive unchanged.', 'tolerance': 1e-6}
    assert len(checked) == len(calls) == 2
    assert 'A valid argument that must survive unchanged.' in calls[1][0]['content']
    assert 'replace_fields' in calls[1][0]['content']


def test_literal_string_whitespace_is_recovered_without_changing_any_value():
    from researchclaw.orchestration.artifacts import recover_string_whitespace
    value = {'analysis': 'An exact argument\nwith a\ttab\rand a \\alpha symbol.', 'metrics': [1, 2.5], 'ok': True}
    broken = json.dumps(value).replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    assert recover_string_whitespace(broken) == value


@pytest.mark.parametrize('raw', [
    '{"analysis":"incomplete\n', '{"analysis":"text\tvalue"',
    '{"analysis":"tab\there"}{"extra":1}', '{"analysis":"bad\x00control\there"}',
    '{"analysis":"tab\there","analysis":"duplicate"}', '{"analysis":"tab\there","x":NaN}',
    '{"analysis":"bad\\q escape\there"}',
])
def test_whitespace_recovery_rejects_other_errors_and_does_not_infer_missing_work(raw):
    from researchclaw.orchestration.artifacts import recover_string_whitespace
    assert recover_string_whitespace(raw) == {}


@pytest.mark.parametrize('truncated', [False, True])
def test_whitespace_recovery_uses_no_model_retry_but_never_accepts_truncation(tmp_path, truncated):
    calls = []
    class Model:
        config = SimpleNamespace(max_tokens=32768)
        def chat(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(content='{"valid":true,"analysis":"line\tvalue"}',
                truncated=truncated, finish_reason='length' if truncated else 'stop')
    engine = CompactResearch.__new__(CompactResearch)
    engine.root, engine.llm = tmp_path, Model()
    if truncated:
        with pytest.raises(ContractError, match='Truncated'):
            engine._json('Return the complete artifact', lambda value: None, system='Fixture')
        assert len(calls) == 2 and not list((tmp_path/'compact_response_normalizations').glob('*.json'))
    else:
        value = engine._json('Return the complete artifact', lambda value: None, system='Fixture')
        assert value['analysis'] == 'line\tvalue' and len(calls) == 1
        normalization = next((tmp_path/'compact_response_normalizations').glob('*.json'))
        assert json.loads(normalization.read_text())['rule'] == 'escape_literal_string_whitespace'
