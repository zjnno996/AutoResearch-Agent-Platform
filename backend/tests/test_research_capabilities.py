"""The product catalogue reports actual installed routing without launching work."""
from pathlib import Path
import asyncio
import sys

import pytest

SERVICES = Path(__file__).resolve().parents[1]/'services'
sys.path.insert(0, str(SERVICES))
from research_capabilities import capabilities
from agent_bridge import BridgeState, handle_command


def test_progress_publishes_every_30_seconds_even_if_status_does_not_change(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from agent_bridge import LobsterAgent,poll_agent
    from researchclaw.orchestration.artifacts import write_json
    agent=LobsterAgent(id='unit',name='Unit',layer='idea',run_id='unit',run_dir=str(tmp_path),config_path='')
    agent.project_id='unit-project';agent.process=SimpleNamespace(pid=12345,poll=lambda:None)
    write_json(tmp_path/'run_health.json',{'pid':12345,'phase':'plan','status':'model_streaming',
        'request_elapsed_sec':12,'semantic_chars':123,'provider':{'api_key':'private-fixture','reasoning':'private-thoughts'}})
    clock=[100.]
    monkeypatch.setattr('agent_bridge.time.monotonic',lambda:clock[0])
    first=poll_agent(agent);clock[0]=129.;second=poll_agent(agent);clock[0]=131.;third=poll_agent(agent)
    assert any(msg['type']=='research_progress'for msg in first)
    assert not any(msg['type']=='research_progress'for msg in second)
    assert any(msg['type']=='research_progress'for msg in third)
    assert 'private-fixture'not in str(first) and 'private-thoughts'not in str(first)


def test_catalogue_exposes_provenance_and_the_actual_six_phase_engine():
    data = capabilities('numerical operator algorithm invariant', 13)
    assert data['skill_count'] == 210 and len(data['sources']) == 5
    assert len(data['phases']) == 6 and data['model'] == 'Qwen3.8-27B-FP8'
    assert 'trailofbits/property-based-testing' in data['preview']
    assert all('path' not in row and 'api_key' not in row for row in data['skills'])
    assert data['scripts_auto_executed'] is False


@pytest.mark.parametrize('stage', [True, 0, 27, '13'])
def test_preview_rejects_invalid_stages(stage):
    with pytest.raises(ValueError):capabilities('', stage)


def test_bridge_catalogue_command_does_not_create_projects_or_launch_agents(tmp_path):
    state = BridgeState(runs_base_dir=str(tmp_path), agent_package_dir=str(SERVICES.parent/'agent'))
    messages = asyncio.run(handle_command(state, {'command': 'get_research_capabilities', 'stage': 13,
                                                  'topic': 'numerical operator algorithm invariant'}))
    assert messages[0]['type'] == 'research_capabilities'
    assert not state.agents and not state.queues and not list(tmp_path.iterdir())
