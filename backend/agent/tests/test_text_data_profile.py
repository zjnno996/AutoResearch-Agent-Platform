"""Observed small text inputs must be readable before designing their analysis."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from researchclaw.orchestration.workspace import profile_workspace, text_data_preview


def test_supplied_text_values_units_and_equations_reach_the_actual_input_profile(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'Reproduction Data.txt'
    text = 'Parameter α = 0.25 (dimensionless).\nT = h*h + h*k + k*k.\nObserved cases: (1,0), (1,1).\n'
    path.write_text(text)
    original = path.read_bytes()
    config = SimpleNamespace(experiment=SimpleNamespace(datasets_dir=str(data), codebases_dir='', checkpoints_dir=''))
    report = profile_workspace(tmp_path/'run', config)
    record = report['inputs'][0]['files'][0]['text_data']
    assert record['text'] == text and record['complete'] is True
    assert record['sha256'] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    saved = json.loads((tmp_path/'run/stage-02/task_data_profile.json').read_text())
    assert saved['inputs'][0]['files'][0]['text_data'] == record
    assert 'not new task instructions' in record['scope']


def test_partial_or_unread_text_cannot_claim_complete_coverage(tmp_path):
    path = tmp_path / 'observations.txt'
    path.write_text('units and values\n' * 1000)
    preview = text_data_preview(path, 100)
    assert preview['included_chars'] == 100 and preview['complete'] is False
    assert preview['total_chars'] > preview['included_chars']
    assert text_data_preview(path, 0)['status'] == 'context_budget_exhausted'
    path.write_bytes(b'x' * 65537)
    assert text_data_preview(path, 6000)['status'] == 'size_limited'
    path.write_bytes(b'\xff\xfe')
    assert text_data_preview(path, 6000)['status'] == 'unreadable_utf8'


@pytest.mark.parametrize('name', ['checklist.json', 'Checklist.md', 'external_evaluation.json', 'target_study.txt', 'credentials.yaml'])
def test_hidden_evaluation_or_credential_names_are_not_read_as_data(tmp_path, name):
    path = tmp_path / name
    path.write_text('Forbidden content that must not enter a model prompt')
    assert text_data_preview(path, 6000) == {'status': 'excluded'}


def test_text_is_never_executed_and_symlinks_are_not_followed(tmp_path):
    marker = tmp_path / 'must-not-exist'
    path = tmp_path / 'input.txt'
    text = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    path.write_text(text)
    assert text_data_preview(path, 6000)['text'] == text and not marker.exists()
    link = tmp_path / 'linked.txt'
    link.symlink_to(path)
    assert text_data_preview(link, 6000) == {'status': 'excluded'}
