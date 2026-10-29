#!/usr/bin/env python3
"""Read-only end-to-end checks for a pinned native multi-task campaign.

Only aggregate official scores are read. No benchmark targets, checklists or
per-item evaluator feedback are opened. A three-task mean is not a leaderboard.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time


def read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def matches(root: Path, relative: str, digest: str) -> bool:
    part = Path(relative)
    if part.is_absolute() or '..' in part.parts:
        return False
    path = root/part
    try:
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
            return False
        with path.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest() == digest
    except OSError:
        return False


def check(suite: dict) -> dict:
    cases = []
    phases = ['ground', 'plan', 'implement', 'execute', 'report', 'verify']
    terminal_states = {'scored', 'scoring_failed', 'failed', 'interrupted', 'not_started_batch_interrupted'}
    for task_id, ledger_name in suite['case_ledgers'].items():
        if not ledger_name and task_id in suite.get('case_status_files', {}):
            status = read(Path(suite['case_status_files'][task_id]))
            if status and status.get('pid') != suite['control_pids'][task_id]:
                raise ValueError('Suite observer belongs to a different preselected attempt')
            ledger_name = status.get('ledger', '')
            if not ledger_name and status.get('status') in {'process_ended', 'queue_requires_check'}:
                cases.append({'task_id': task_id, 'status': 'failed', 'failure_phase': 'startup',
                    'end_to_end_scored': False, 'scientifically_accepted': False, 'official_score': None})
                continue
        ledger = read(Path(ledger_name))
        rows = [row for row in ledger.get('attempts', []) if row.get('task_id') == task_id]
        if len(rows) > 1:
            raise ValueError('A suite must identify one preselected attempt per task, not best-of-N')
        if not rows:
            status = 'not_started_batch_interrupted' if ledger.get('status') in {'finished', 'interrupted'} else 'pending'
            cases.append({'task_id': task_id, 'status': status, 'end_to_end_scored': False,
                          'scientifically_accepted': False, 'official_score': None})
            continue
        row = rows[0]
        workspace = Path(row['workspace'])
        allowed = Path(suite['workspace_root']).resolve()
        if not workspace.resolve().is_relative_to(allowed) or workspace.is_symlink():
            raise ValueError('Suite workspace is outside the pinned benchmark workspace root')
        run = workspace/'.claw/run'
        meta, identity = read(workspace/'_meta.json'), read(workspace/'_claw_benchmark.json')
        state, receipt = read(run/'compact_state.json'), read(run/'stage-16/experiment_provenance.json')
        exported, verification = read(workspace/'_claw_export.json'), read(run/'research_verification.json')
        same = (identity.get('agent_source_sha256') == suite['source_sha256']
            and identity.get('agent_settings_sha256') == suite['settings_sha256']
            and meta.get('model') == suite['model'] and ledger.get('judge_model') == suite['model'])
        work = run/'stage-11/experiment'
        sources, outputs = receipt.get('source_hashes', {}), receipt.get('output_hashes', {})
        executed = bool(receipt.get('success') is True and receipt.get('returncode') == 0 and sources and outputs
            and all(matches(work, name, digest) for name, digest in {**sources, **outputs}.items()))
        code_exported = bool(sources and all(matches(workspace/'code', name, digest) for name, digest in sources.items()))
        report_exported = bool(exported.get('deliverables_complete') is True and (workspace/'report/report.md').is_file()
            and exported.get('images') and not exported.get('missing_images')
            and all((workspace/'report/images'/name).is_file() for name in exported['images']))
        source_outputs_exported = bool(outputs and all(
            matches(workspace, name, digest) for name, digest in outputs.items()))
        score = row.get('score')
        valid_score = type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 100
        graded = row.get('status') == 'scored' and valid_score
        phase_state = {name: state.get('phases', {}).get(name, {}).get('status', 'pending') for name in phases}
        accepted = bool(phase_state['verify'] == 'done' and verification.get('all_satisfied') is True)
        cases.append({'task_id': task_id, 'workspace': str(workspace), 'status': row.get('status'),
            'identities_match': same, 'phases': phase_state, 'real_execution_verified': executed,
            'held_method_evidence': receipt.get('method_probes', {}).get('status', 'not_recorded'),
            'code_export_hashes_match': code_exported, 'report_and_images_exported': report_exported,
            'output_export_hashes_match': source_outputs_exported, 'scientifically_accepted': accepted,
            'scientific_status': meta.get('scientific_status', 'not_yet_assessed'),
            'official_score': score if graded else None,
            'end_to_end_scored': bool(same and executed and code_exported and report_exported
                and source_outputs_exported and graded),
            'model_calls': read(run/'model_call_budget.json').get('attempts', 0),
            'health': read(run/'run_health.json').get('status', 'initializing')})
    complete = bool(cases and all(case['end_to_end_scored'] for case in cases))
    all_terminal = bool(cases and all(case['status'] in terminal_states for case in cases))
    return {'observed_at': datetime.now(timezone.utc).isoformat(), 'suite': suite['name'], 'model': suite['model'],
        'source_sha256': suite['source_sha256'], 'settings_sha256': suite['settings_sha256'],
        'cases': cases, 'all_cases_terminal': all_terminal, 'all_cases_end_to_end_scored': complete,
        'all_cases_scientifically_accepted': bool(cases and all(case['scientifically_accepted'] for case in cases)),
        'three_task_mean_score': sum(case['official_score'] for case in cases)/len(cases) if complete else None,
        'scope': 'Only the preselected tasks; not a 40-task leaderboard mean or public submission.'}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    suite = read(args.suite)
    while True:
        result = check(suite)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        os.replace(temporary, args.output)
        if not args.watch or result['all_cases_terminal']:
            print(json.dumps(result, ensure_ascii=False))
            return
        time.sleep(30)


if __name__ == '__main__':
    main()
