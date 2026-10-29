"""Resume only a transport-failed attempt under its unchanged original identity."""
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import read_json


def validate_resume(workspace: Path, enabled: bool, max_calls: int) -> dict[str, Any]:
    run = workspace / '.claw/run'
    if not enabled:
        if run.exists():
            raise ValueError('Use a new prepared workspace for each benchmark attempt; --resume is only for transport failures')
        return {}
    state = read_json(run / 'compact_state.json')
    meta = read_json(workspace / '_meta.json')
    failed = [(name, phase) for name, phase in state.get('phases', {}).items() if phase.get('status') == 'failed']
    if (meta.get('status') != 'failed' or len(failed) != 1 or failed[0][1].get('failure_kind') != 'transport'
            or (workspace / 'external_evaluation.json').exists()):
        raise ValueError('--resume requires an unscored attempt that failed on model transport, not scientific or execution failure')
    budget = read_json(run / 'model_call_budget.json')
    if (type(budget.get('attempts')) is not int or budget['attempts'] < 0
            or budget.get('limit') != max_calls):
        raise ValueError('The original model-call budget record is missing or mismatched; resuming cannot reset it')
    if budget.get('attempts', 0) >= max_calls:
        raise ValueError('The original model-call budget is exhausted; resuming cannot reset it')
    return {'failed_phase': failed[0][0], 'previous_error': failed[0][1].get('error'),
            'calls_before': budget.get('attempts', 0), 'max_calls': max_calls}
