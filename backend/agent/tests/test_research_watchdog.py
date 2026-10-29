"""Long quiet calls are observable; real progress is not mistaken for a hang."""
import json
import urllib.request
import pytest

from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.observability.tracing import StageTrace
from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.watchdog import health_snapshot


@pytest.mark.parametrize('reasoning_field', ['reasoning_content', 'reasoning'])
def test_streaming_reasoning_updates_health_without_exposing_text(tmp_path,monkeypatch,reasoning_field):
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def __iter__(self):
            yield ('data:'+json.dumps({'choices':[{'delta':{reasoning_field:'private-reasoning-text'}}]})).encode()
            # Observe while reasoning is still streaming, before visible content.
            progress=read_json(tmp_path/'provider_activity.json')
            assert progress['status']=='streaming'
            assert progress['semantic_chars']==len('private-reasoning-text')
            assert progress['reasoning_chars']==len('private-reasoning-text') and progress['content_chars']==0
            assert health_snapshot(tmp_path,now=progress['updated_at']+1)['status']=='model_streaming'
            yield ('data:'+json.dumps({'choices':[{'delta':{'content':'{"valid":true}'},'finish_reason':'stop'}]})).encode()
    monkeypatch.setattr(LLMClient,'_urlopen',lambda *args,**kwargs:Response())
    client=LLMClient(LLMConfig(base_url='https://fixture.test',api_key='private-api-key',primary_model='deepseek-flash'))
    with StageTrace(tmp_path,tmp_path,9,'plan','test'):
        assert client.chat([{'role':'user','content':'task'}]).content=='{"valid":true}'
    progress=read_json(tmp_path/'provider_activity.json')
    assert progress['semantic_chars']>0 and progress['status']=='completed'
    assert progress['reasoning_chars']==len('private-reasoning-text')
    assert progress['content_chars']==len('{"valid":true}')
    text=(tmp_path/'provider_activity.json').read_text()
    assert 'private-reasoning-text' not in text and 'private-api-key' not in text


def test_reasoning_stream_and_stall_are_distinguished(tmp_path):
    write_json(tmp_path/'heartbeat.json',{'phase':'plan'})
    write_json(tmp_path/'provider_activity.json',{'status':'streaming','started_at':0,
        'last_semantic_progress_at':100,'semantic_chars':1000,'timeout_sec':300})
    assert health_snapshot(tmp_path,now=110)['status']=='model_streaming'
    assert health_snapshot(tmp_path,now=200)['status']=='waiting_provider'
    assert health_snapshot(tmp_path,now=500)['status']=='provider_stalled_check_required'


def test_cpu_activity_prevents_false_stall_for_a_long_experiment(tmp_path):
    write_json(tmp_path/'heartbeat.json',{'phase':'execute'})
    write_json(tmp_path/'compact_state.json',{'phases':{'execute':{'status':'running','started':0}}})
    write_json(tmp_path/'provider_activity.json',{'status':'completed','started_at':0})
    assert health_snapshot(tmp_path,now=1000,cpu_delta=2)['status']=='computing'
    assert health_snapshot(tmp_path,now=1000,cpu_delta=0)['status']=='quiet_check_required'


def test_keepalives_cannot_extend_a_request_forever(monkeypatch):
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def __iter__(self):return iter([b': keepalive\n'])
    clock=iter([0.,6.])
    monkeypatch.setattr('researchclaw.llm.client.time.monotonic',lambda:next(clock))
    monkeypatch.setattr(LLMClient,'_urlopen',lambda *args,**kwargs:Response())
    with pytest.raises(TimeoutError,match='deadline'):
        LLMClient._stream_read(urllib.request.Request('https://fixture.test'),5)


def test_finished_request_and_phase_durations_stop_growing(tmp_path):
    write_json(tmp_path/'heartbeat.json', {'phase': 'verify'})
    write_json(tmp_path/'compact_state.json', {'phases': {'verify': {'status': 'done', 'started': 50, 'elapsed_sec': 12}}})
    write_json(tmp_path/'provider_activity.json', {'status': 'completed', 'started_at': 50, 'updated_at': 60,
                                                'semantic_chars': 1200, 'model': 'Qwen3.8-27B-FP8'})
    data = health_snapshot(tmp_path, now=1000)
    assert data['phase_elapsed_sec'] == 12 and data['request_elapsed_sec'] == 10
    assert data['semantic_chars'] == 1200
