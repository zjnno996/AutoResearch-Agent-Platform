"""Capacity checks for long-context native benchmark queues; no model calls."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import urllib.request


def capacity_policy(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    policy = json.loads(raw)
    if (type(policy.get('max_active_jobs')) is not int or not 1 <= policy['max_active_jobs'] <= 3
            or type(policy.get('kv_cache_threshold')) not in (int, float)
            or not 0 < policy['kv_cache_threshold'] < 1
            or policy.get('model') != 'Qwen3.8-27B-FP8'):
        raise ValueError('Invalid native capacity policy')
    return policy, hashlib.sha256(raw).hexdigest()


def service_capacity(policy: dict) -> dict:
    """Read aggregate gauges only, without sending provider credentials."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(policy['metrics_url'],
            headers={'User-Agent': 'EMNETS-RA-runtime-health/1.0'})
        with opener.open(request, timeout=2) as response:
            raw = response.read(512*1024).decode('utf-8', errors='replace')
        result = {}
        for name in ('num_requests_running', 'num_requests_waiting', 'kv_cache_usage_perc', 'num_preemptions_total'):
            rows = re.findall(r'(?m)^vllm:'+name+r'\{([^\n]*)\}\s+([^\s]+)\s*$', raw)
            values = [float(value) for labels, value in rows
                      if 'model_name="'+policy['model']+'"' in labels]
            if not values or not all(math.isfinite(v) and v >= 0 for v in values):
                raise ValueError('Missing or invalid aggregate service metric')
            result[name] = max(values) if name == 'kv_cache_usage_perc' else sum(values)
        if result['kv_cache_usage_perc'] > 1:
            raise ValueError('Invalid KV-cache usage fraction')
        return {'status': 'available', **result}
    except (OSError, ValueError, KeyError) as exc:
        return {'status': 'unavailable', 'error_type': type(exc).__name__}


def capacity_decision(active_jobs: int, policy: dict, service: dict) -> tuple[bool, str]:
    if active_jobs >= policy['max_active_jobs']:
        return False, 'owned_native_job_limit'
    if service.get('status') != 'available':
        return True, 'metrics_unavailable_conservative_job_limit'
    if service['num_requests_waiting'] > 0:
        return False, 'provider_request_queue_nonempty'
    if service['num_requests_running'] >= policy['max_active_jobs']:
        return False, 'provider_running_request_limit'
    # Idle cached prefixes need not prevent useful work from restarting.
    if service['num_requests_running'] == 0:
        return True, 'provider_idle'
    if service['kv_cache_usage_perc'] >= policy['kv_cache_threshold']:
        return False, 'provider_context_headroom_low'
    return True, 'context_headroom_available'
