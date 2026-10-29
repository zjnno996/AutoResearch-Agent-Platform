import json
from pathlib import Path
import sys

import pytest
import yaml

SERVICES = Path(__file__).resolve().parents[1]/'services'
sys.path.insert(0, str(SERVICES))
from agent_bridge import BridgeState, TaskQueue, quick_submit_project


@pytest.mark.parametrize('delivery', ['report', 'ieee'])
def test_focused_submission_creates_one_pipeline_with_the_requested_delivery(tmp_path, delivery):
    state = BridgeState(runs_base_dir=str(tmp_path), agent_package_dir=str(SERVICES.parent/'agent'))
    state.queues['init_to_idea'] = TaskQueue('init_to_idea', tmp_path/'queues/init_to_idea.json')
    messages = quick_submit_project(state, 'Compute a finite least-squares diagnostic.', 'web-focused',
        mode='focused', submission_mode='auto', user_id='alice', run_mode='full_chain', delivery_format=delivery)
    assert len(state.queues['init_to_idea'].tasks) == 1
    project = tmp_path/'projects/web-focused'
    meta = json.loads((project/'project_meta.json').read_text())
    config = yaml.safe_load(Path(meta['config_path']).read_text())
    assert meta['mode'] == 'focused' and meta['delivery_format'] == delivery and meta['user_id'] == 'alice'
    assert not list(project.glob('run-*')) and not state.agents
    assert config['research_engine']['workflow'] == 'compact'
    assert config['llm']['primary_model'] == 'Qwen3.8-27B-FP8'
    assert ('IEEE 双栏 LaTeX' in config['research']['topic']) is (delivery == 'ieee')
    assert any('专注研究' in str(m) for m in messages)


def test_invalid_delivery_is_rejected_before_creating_a_project(tmp_path):
    state = BridgeState(runs_base_dir=str(tmp_path), agent_package_dir=str(SERVICES.parent/'agent'))
    messages = quick_submit_project(state, 'A bounded validation task.', mode='focused', delivery_format='unknown')
    assert '请选择有效' in str(messages) and not (tmp_path/'projects').exists()
