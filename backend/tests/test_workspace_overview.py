import asyncio
import json
from pathlib import Path
import sys

SERVICES = Path(__file__).resolve().parents[1]/'services'
sys.path.insert(0, str(SERVICES))
from workspace_overview import workspace_overview
from agent_bridge import BridgeState, handle_command


def test_overview_uses_actual_phase_coverage_and_does_not_expose_server_settings():
    data = workspace_overview([
        {'projectId': 'a', 'status': 'running', 'workflow': 'compact',
         'completedPhases': ['ground', 'ground', 'plan', 'invented'],
         'configPath': '/private/config.yaml', 'api_key': 'secret'},
        {'projectId': 'b', 'status': 'completed', 'workflow': 'legacy', 'lastCompletedStage': 26},
    ])
    assert data['counts']['total'] == 2 and data['counts']['running'] == 1
    assert data['projects'][0]['progress'] == 33 and data['projects'][1]['progress'] == 100
    assert data['phaseCounts']['implement'] == 1
    assert 'secret' not in str(data) and '/private' not in str(data)


def test_overview_command_requires_authentication_and_filters_real_owned_projects(tmp_path):
    state = BridgeState(runs_base_dir=str(tmp_path), agent_package_dir=str(SERVICES.parent/'agent'))
    for owner in ['alice', 'bob']:
        project = tmp_path/'projects'/owner
        project.mkdir(parents=True)
        (project/'project_meta.json').write_text(json.dumps({'user_id': owner, 'topic': owner+' research'}))
    denied = asyncio.run(handle_command(state, {'command': 'get_workspace_overview'}))
    assert denied[0]['type'] == 'workspace_overview_error'
    socket = object()
    state.user_clients[socket] = 'alice'
    messages = asyncio.run(handle_command(state, {'command': 'get_workspace_overview'}, socket))
    assert messages[0]['type'] == 'workspace_overview'
    assert [p['projectId'] for p in messages[0]['payload']['projects']] == ['alice']
    assert messages[0].get('_targetUserId') == 'alice'
    assert not state.agents and not state.queues


def test_partial_legacy_checkpoint_does_not_break_the_workspace_snapshot():
    data = workspace_overview([{'projectId': 'partial', 'workflow': 'compact',
                               'status': 'interrupted', 'completedPhases': None}])
    assert data['projects'][0]['progress'] == 0
    assert data['projects'][0]['completedPhases'] == []
    assert data['counts']['interrupted'] == 1
