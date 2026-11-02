#!/usr/bin/env python3
"""Observe the preselected public acceptance run without restarting any work."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT/'.local/production/verification'


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def inspect():
    selected = read(ARTIFACTS/'research-run.json')
    if not selected:
        raise SystemExit('No preselected public acceptance run has been submitted.')
    project = ROOT/'backend/runs/production/projects'/selected['id']
    state = read(project/'compact_state.json')
    health = read(project/'run_health.json')
    activity = read(project/'provider_activity.json')
    checkpoint = read(project/'checkpoint.json')
    workers = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            command = (process/'cmdline').read_bytes().split(b'\0')
            if (b'researchclaw' in command and b'run' in command
                    and any(str(project).encode() in arg for arg in command)
                    and (process/'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'):
                workers.append(int(process.name))
        except (OSError, IndexError):
            continue
    live = bool(workers)
    pid = health.get('pid') if health.get('pid') in workers else (workers[0] if workers else None)
    phases = {name: state.get('phases', {}).get(name, {}).get('status', 'pending')
              for name in ('ground','plan','implement','execute','report','verify')}
    complete = all(value=='done' for value in phases.values()) and checkpoint.get('last_completed_stage')==26
    paused = read(project/'project_meta.json').get('paused', False)
    status = 'completed' if complete else 'paused' if paused and not live else 'running' if live else 'awaiting_worker'
    result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'id': selected['id'],
              'status': status, 'process_alive': live, 'pid': pid, 'worker_pids': workers, 'phases': phases,
              'last_completed_stage': checkpoint.get('last_completed_stage'),
              'health': {k: health[k] for k in ('status','phase','model','phase_elapsed_sec','request_elapsed_sec',
                         'request_active','provider_silence_sec') if k in health},
              'provider_progress': {k: activity[k] for k in ('status','model','reasoning_chars','content_chars',
                                   'semantic_chars','reasoning_effort','updated_at') if k in activity},
              'model_calls': read(project/'model_call_budget.json').get('attempts'),
              'scientifically_accepted': read(project/'research_verification.json').get('all_satisfied', False)}
    temporary = ARTIFACTS/f'run-status-{os.getpid()}.tmp'
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(ARTIFACTS/'run-status.json')
    with (ARTIFACTS/'run-status.jsonl').open('a') as history:
        history.write(json.dumps(result, ensure_ascii=False)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    args=parser.parse_args()
    while True:
        result=inspect()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if not args.watch or result['status'] in {'completed','paused'}:
            break
        time.sleep(30)
