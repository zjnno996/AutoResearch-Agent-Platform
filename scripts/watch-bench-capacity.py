"""Observe aggregate model capacity until all owned native jobs/queues end."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from bench_admission import capacity_policy, service_capacity, capacity_decision


def owned_jobs(root: Path) -> tuple[list[int], list[int]]:
    active, queued = set(), set()
    for path in root.glob('qwen-v*-native-control.json'):
        try:
            record = json.loads(path.read_text())
            if record.get('model') != 'Qwen3.8-27B-FP8':
                continue
            pid = record['pid']
            proc = Path('/proc')/str(pid)
            stat = (proc/'stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] == 'Z' or (record.get('process_start_ticks') and stat[19] != record['process_start_ticks']):
                continue
            command = (proc/'cmdline').read_bytes()
            reserved = not command and record.get('status') == 'starting' and record.get('process_start_ticks') == stat[19]
            if b'researchclaw rcbench batch' in command.replace(b'\0', b' ') or reserved:
                active.add(pid)
            elif b'launch_qwen_' in command and b'_case.py' in command:
                queued.add(pid)
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return sorted(active), sorted(queued)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign-dir', required=True, type=Path)
    args = parser.parse_args()
    root = args.campaign_dir
    (root/'qwen-capacity-monitor-pid.txt').write_text(str(os.getpid())+'\n')
    while True:
        policy, digest = capacity_policy(root/'qwen-native-capacity-policy.json')
        active, queued = owned_jobs(root)
        service = service_capacity(policy)
        allowed, reason = capacity_decision(len(active), policy, service)
        result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'model': policy['model'],
            'policy_sha256': digest, 'max_active_jobs': policy['max_active_jobs'],
            'active_native_pids': active, 'queued_pids': queued, 'service': service,
            'capacity_allows_next_job': allowed, 'capacity_decision': reason}
        latest = root/'qwen-capacity-status.json'
        temporary = latest.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2)+'\n')
        os.replace(temporary, latest)
        with (root/'qwen-capacity-status.jsonl').open('a') as output:
            output.write(json.dumps(result)+'\n')
        if not active and not queued:
            print(json.dumps(result))
            return
        time.sleep(30)


if __name__ == '__main__':
    main()
