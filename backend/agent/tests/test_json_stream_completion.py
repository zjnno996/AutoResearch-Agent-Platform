"""Complete JSON followed by endless closing tokens must not hang a stage."""
import json
import urllib.request

import pytest

from researchclaw.llm.client import LLMClient, LLMConfig


class Stream:
    def __init__(self, pieces):
        self.pieces = pieces
        self.read = 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def __iter__(self):
        for content in self.pieces:
            self.read += 1
            yield b'data: '+json.dumps({'choices':[{'delta':{'content':content}}]}).encode()+b'\n'


def request():
    return urllib.request.Request('https://fixture.test', data=json.dumps({
        'response_format': {'type':'json_object'}}).encode())


def test_complete_json_repeated_closers_preserve_exact_valid_artifact(monkeypatch):
    value = {'negative_result': True, 'text': 'Literal } and escaped "quotes" remain unchanged.'}
    original = json.dumps(value)
    stream = Stream([original, *['\n}']*200])
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: stream)
    result = LLMClient._stream_read(request(), 60)
    assert result['choices'][0]['message']['content'] == original
    assert result['choices'][0]['finish_reason'] == 'json_repetition_recovered'
    assert stream.read < 201
    assert result['transport_recovery']['usage_complete'] is False


@pytest.mark.parametrize('text', ['{"unterminated":"'+'}'*100, '{"bad": nope}'+'}'*100])
def test_invalid_or_incomplete_json_is_never_promoted_to_completed_artifact(monkeypatch, text):
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: Stream([text]))
    result = LLMClient._stream_read(request(), 60)
    assert result['choices'][0]['finish_reason'] in {'stream_interrupted', 'json_repetition_rejected'}
    assert 'transport_recovery' not in result or result['transport_recovery']['valid_complete_root'] is False


def test_premature_root_followed_by_more_members_is_not_silently_dropped(monkeypatch):
    text = '{"first":1},"second":2}'+'}'*100
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: Stream([text]))
    result = LLMClient._stream_read(request(), 60)
    assert result['choices'][0]['message']['content'] == text
    assert result['choices'][0]['finish_reason'] == 'stream_interrupted'


def test_legitimate_closing_tokens_inside_json_string_are_preserved(monkeypatch):
    text = json.dumps({'literal':'\n}'*500})
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: Stream(list(text)))
    result = LLMClient._stream_read(request(), 60)
    assert result['choices'][0]['message']['content'] == text
    assert result['choices'][0]['finish_reason'] == 'stream_interrupted'


def test_plain_text_requests_have_no_json_termination_rule(monkeypatch):
    text = '{"example":true}'+'}'*100
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: Stream([text]))
    result = LLMClient._stream_read(urllib.request.Request('https://fixture.test'), 60)
    assert result['choices'][0]['message']['content'] == text
    assert result['choices'][0]['finish_reason'] == 'stream_interrupted'


def test_meaningful_tail_already_in_the_chunk_cannot_be_dropped(monkeypatch):
    text = '{"first":1}'+'}'*70+',"second":2}'
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: Stream([text]))
    result = LLMClient._stream_read(request(), 60)
    assert result['choices'][0]['message']['content'] == text
    assert result['choices'][0]['finish_reason'] == 'stream_interrupted'


def test_invalid_degenerate_response_remains_truncated_in_client(monkeypatch):
    monkeypatch.setattr(LLMClient, '_urlopen', lambda *args, **kwargs: Stream(['{"bad": nope}'+'}'*100]))
    client=LLMClient(LLMConfig(base_url='https://fixture.test',api_key='fixture',primary_model='fixture-json'))
    response=client._raw_call('fixture-json',[{'role':'user','content':'Return JSON'}],100,0,True)
    assert response.truncated and response.finish_reason=='json_repetition_rejected'


def test_recovery_audit_does_not_claim_complete_usage_or_log_model_text(monkeypatch,tmp_path):
    from researchclaw.observability.tracing import StageTrace
    text=json.dumps({'finding':'private scientific sentence'})
    monkeypatch.setattr(LLMClient,'_urlopen',lambda *args,**kwargs:Stream([text,'}'*100]))
    with StageTrace(tmp_path,tmp_path/'stage-09',9,'plan','fixture'):
        result=LLMClient._stream_read(request(),60)
    assert result['transport_recovery']['valid_complete_root'] is True
    audit=(tmp_path/'traces/transport_recoveries.jsonl').read_text()
    assert 'private scientific sentence' not in audit
    assert json.loads(audit)['transport_recovery']['usage_complete'] is False
