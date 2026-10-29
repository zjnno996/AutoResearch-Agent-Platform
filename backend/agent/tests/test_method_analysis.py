"""Scientific draft isolation, honest conditions and exact-input reuse."""
import copy
import json
from pathlib import Path

import pytest
import jsonschema

from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.method_analysis import derive_method_draft, draft_schema, validate_draft


CONTRACT = {'requirements': [{'id': 'R1', 'kind': 'proof'}, {'id': 'R2', 'kind': 'experiment'}, {'id': 'R3', 'kind': 'report'}]}
DRAFT = {'scientific_analysis_markdown': 'Under the stated invertibility condition, Ax=b implies x=A^-1 b.',
    'obligations': {'R1': {'status': 'conditional', 'statement': 'Unique solution of Ax=b',
        'conditions': ['A is invertible'], 'evidence': 'Multiply Ax=b by A^-1.', 'remaining_work': ''}}}


def test_scientific_draft_reuse_requires_the_same_task_sources_and_observations(tmp_path):
    calls = []
    def ask(prompt, validator, **kwargs):
        calls.append((prompt, kwargs))
        value = copy.deepcopy(DRAFT)
        validator(value)
        return value
    first, path = derive_method_draft(tmp_path, 'Prove uniqueness', CONTRACT, 'Original source equation.', ask,
                                     formal=True, observations='A is 2x2')
    again, reused = derive_method_draft(tmp_path, 'Prove uniqueness', CONTRACT, 'Original source equation.', ask,
                                       formal=True, observations='A is 2x2')
    assert first == again == DRAFT and reused == path and len(calls) == 1
    assert calls[0][1]['scientific'] is True and 'response_schema' in calls[0][1]
    assert calls[0][1]['native_effort'] == 'medium'
    assert first['obligations']['R1']['status'] == 'conditional'
    record = json.loads((tmp_path / path).read_text())
    assert record['assessment'] == 'unverified_scientific_draft'
    _, changed = derive_method_draft(tmp_path, 'Prove uniqueness', CONTRACT, 'Corrected source equation.', ask,
                                     formal=True, observations='A is 2x2')
    assert changed != path and len(calls) == 2 and (tmp_path / path).is_file()
    _, observed = derive_method_draft(tmp_path, 'Prove uniqueness', CONTRACT, 'Corrected source equation.', ask,
                                      formal=True, observations='A is singular')
    assert observed != changed and len(calls) == 3
    _, guided = derive_method_draft(tmp_path, 'Prove uniqueness', CONTRACT, 'Corrected source equation.', ask,
        formal=True, observations='A is singular', guidance='Source-pinned mathematical guidance.')
    assert guided != observed and len(calls) == 4
    assert 'Source-pinned mathematical guidance.' in calls[-1][0]
    _, reconfigured = derive_method_draft(tmp_path, 'Prove uniqueness', CONTRACT, 'Corrected source equation.', ask,
        formal=True, observations='A is singular', guidance='Source-pinned mathematical guidance.', cache_scope='different-model-settings')
    assert reconfigured != guided and len(calls) == 5


@pytest.mark.parametrize('defect', ['missing_argument', 'missing_obligation', 'conditional_without_conditions', 'unresolved_without_gap'])
def test_unresolved_science_cannot_be_erased_by_a_status_label(defect):
    value = copy.deepcopy(DRAFT)
    if defect == 'missing_argument':
        value['scientific_analysis_markdown'] = ''
    elif defect == 'missing_obligation':
        value['obligations'] = {}
    elif defect == 'conditional_without_conditions':
        value['obligations']['R1']['conditions'] = []
    else:
        value['obligations']['R1']['status'] = 'unresolved'
    with pytest.raises(ContractError):
        validate_draft(value, CONTRACT, formal=True)


def test_empirical_method_draft_retains_scientific_work_without_claiming_report_delivery():
    schema = draft_schema(CONTRACT, formal=False)
    value = copy.deepcopy(DRAFT)
    value['obligations']['R2'] = {'status': 'unresolved', 'statement': 'Measure convergence on supplied data',
        'conditions': [], 'evidence': 'Source-defined numerical protocol; no execution yet.',
        'remaining_work': 'Execute the numerical experiment and inspect its actual output.'}
    validate_draft(value, CONTRACT, formal=False)
    jsonschema.validate(value, schema)
    assert set(schema['properties']['obligations']['required']) == {'R1', 'R2'}
    assert 'R3' not in value['obligations']
