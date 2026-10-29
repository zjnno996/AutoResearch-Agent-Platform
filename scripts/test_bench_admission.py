"""Admission must avoid overload while remaining usable without a metrics API."""
import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bench_admission import capacity_decision, service_capacity


POLICY = {'max_active_jobs': 2, 'kv_cache_threshold': .75, 'model': 'Qwen3.8-27B-FP8',
          'metrics_url': 'http://private-model-service/metrics'}


@pytest.mark.parametrize('running,waiting,kv,allowed', [
    (3,1,.96,False), (1,0,.96,False), (1,1,.4,False), (1,0,.4,True), (0,0,.99,True)])
def test_provider_pressure_gates_new_work_without_changing_existing_work(running, waiting, kv, allowed):
    metrics={'status':'available','num_requests_running':running,'num_requests_waiting':waiting,
             'kv_cache_usage_perc':kv}
    assert capacity_decision(0, POLICY, metrics)[0] is allowed
    assert capacity_decision(2, POLICY, metrics)[0] is False


def test_metrics_failure_keeps_the_conservative_local_limit():
    assert capacity_decision(1, POLICY, {'status':'unavailable'})[0]
    assert not capacity_decision(2, POLICY, {'status':'unavailable'})[0]


def test_public_metrics_are_aggregated_only_for_the_pinned_model_without_credentials():
    lines = []
    for engine in ['0','1']:
        for name,value in [('num_requests_running',1),('num_requests_waiting',0),
                           ('kv_cache_usage_perc',.8),('num_preemptions_total',4)]:
            lines.append(f'vllm:{name}{{engine="{engine}",model_name="Qwen3.8-27B-FP8"}} {value}')
    lines.append('vllm:num_requests_running{engine="0",model_name="other-model"} 100')
    def request(req, **kwargs):
        assert req.get_header('Authorization') is None
        return io.BytesIO('\n'.join(lines).encode())
    opener = SimpleNamespace(open=request)
    with patch('urllib.request.build_opener', return_value=opener):
        result = service_capacity(POLICY)
    assert result['status']=='available' and result['num_requests_running']==2
    assert result['kv_cache_usage_perc']==.8 and result['num_preemptions_total']==8


def test_unavailable_http_metrics_do_not_remove_the_local_limit():
    def unavailable(*args, **kwargs):
        raise OSError('offline')
    opener = SimpleNamespace(open=unavailable)
    with patch('urllib.request.build_opener', return_value=opener):
        result = service_capacity(POLICY)
    assert result['status'] == 'unavailable'
    assert not capacity_decision(2, POLICY, result)[0]
