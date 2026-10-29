"""User-scoped, read-only data for the research workspace dashboard."""
from collections import Counter
from datetime import datetime, timezone

PHASES = ('ground', 'plan', 'implement', 'execute', 'report', 'verify')


def workspace_overview(projects: list[dict]) -> dict:
    visible = []
    phases = Counter({name: 0 for name in PHASES})
    for project in projects:
        row = {key: project[key] for key in ('projectId', 'topic', 'status', 'timestamp',
            'lastCompletedStage', 'lastCompletedName', 'workflow') if key in project}
        recorded = project.get('completedPhases')
        recorded = recorded if isinstance(recorded, list) else []
        completed = [name for name in PHASES if name in recorded]
        row['completedPhases'] = completed
        if project.get('workflow') == 'compact':
            row['progress'] = round(len(completed)/6*100)
            if project.get('status') == 'running':
                current = next((name for name in PHASES if name not in completed), 'verify')
                phases[current] += 1
        else:
            stage = project.get('lastCompletedStage', 0)
            row['progress'] = round(min(max(stage if type(stage) is int else 0, 0), 26)/26*100)
        visible.append(row)
    visible.sort(key=lambda row: str(row.get('timestamp', '')), reverse=True)
    counts = Counter(str(row.get('status', 'new')) for row in visible)
    return {'schema_version': 'emnets-workspace-v1',
        'updatedAt': datetime.now(timezone.utc).isoformat(), 'projects': visible,
        'counts': {'total': len(visible), **{name: counts[name] for name in
            ('running', 'queued', 'completed', 'failed', 'interrupted', 'new')}},
        'phaseCounts': dict(phases)}
