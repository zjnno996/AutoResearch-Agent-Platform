"""Actual false-success patterns: counts exist, primary estimates do not."""
import json

import pytest

from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.execution_checks import evaluate_checks, validate_checks


def test_descriptive_notes_preserve_the_actual_scientific_predicates(tmp_path):
    (tmp_path / 'outputs').mkdir()
    path = tmp_path / 'outputs/metrics.json'
    check = {'path': 'outputs/metrics.json', 'requirement_id': 'R1',
             'notes': 'Measured residual must satisfy the reviewed tolerance.',
             'numeric_bounds': {'/residual': {'min': 0, 'max': 1e-6}}}
    validate_checks([check], {'R1'})
    path.write_text('{"residual":1}')
    assert not evaluate_checks(tmp_path, [check])['passed']
    path.write_text('{"residual":0}')
    assert evaluate_checks(tmp_path, [check])['passed']
    with pytest.raises(ContractError, match='at least one output predicate'):
        validate_checks([{'path': check['path'], 'requirement_id': 'R1', 'notes': 'No measurable condition'}])
    with pytest.raises(ContractError, match='Unsupported'):
        validate_checks([{**check, 'accept_anything': True}])


def test_inventory_counts_cannot_substitute_for_required_primary_measurements(tmp_path):
    output = tmp_path / 'outputs'
    output.mkdir()
    (output / 'metrics.json').write_text(json.dumps({'n_rows': 24358, 'n_estimates': 0, 'mean': None}))
    check = {'path': 'outputs/metrics.json', 'requirement_id': 'R1',
             'positive_fields': ['/n_estimates'], 'finite_fields': ['/mean']}
    audit = evaluate_checks(tmp_path, [check])
    assert not audit['passed'] and len(audit['checks'][0]['errors']) == 2
    (output / 'metrics.json').write_text(json.dumps({'n_rows': 24358, 'n_estimates': 19, 'mean': -23.0}))
    assert evaluate_checks(tmp_path, [check])['passed']


def test_all_nan_method_labels_fail_even_when_table_has_many_rows(tmp_path):
    (tmp_path / 'outputs').mkdir()
    table = tmp_path / 'outputs/annual.csv'
    table.write_text('method,value\nnan,-2\nnan,-1\n')
    check = {'path': 'outputs/annual.csv', 'requirement_id': 'R1', 'min_rows': 1,
             'min_non_null': {'method': 1}, 'allowed_values': {'method': ['altimetry', 'gravimetry']}}
    audit = evaluate_checks(tmp_path, [check])
    assert not audit['passed'] and audit['checks'][0]['non_null_counts']['method'] == 0
    table.write_text('method,value\naltimetry,-2\ngravimetry,-1\n')
    assert evaluate_checks(tmp_path, [check])['passed']
    table.write_text('method,value\nother,-2\n')
    assert evaluate_checks(tmp_path, [check])['checks'][0]['unsupported_labels'] == {'method': ['other']}


def test_json_pointers_support_nested_arrays_and_escaped_keys_without_evaluation(tmp_path):
    (tmp_path / 'outputs').mkdir()
    (tmp_path / 'outputs/metrics.json').write_text('{"runs":[{"a/b":0}],"optional":null}')
    check = {'path': 'outputs/metrics.json', 'requirement_id': 'R1', 'finite_fields': ['/runs/0/a~1b']}
    assert evaluate_checks(tmp_path, [check])['passed']
    check['finite_fields'] = ['/absent']
    assert not evaluate_checks(tmp_path, [check])['passed']


@pytest.mark.parametrize('path', ['../metrics.json', 'data/private.csv', 'outputs/../input.csv', '/tmp/value.json'])
def test_check_definitions_cannot_escape_outputs(path):
    with pytest.raises(ContractError):
        validate_checks([{'path': path, 'requirement_id': 'R1', 'finite_fields': ['/mean']}], {'R1'})


@pytest.mark.parametrize('fields', [
    {'requirement_id': 'unknown', 'min_rows': 1},
    {'requirement_id': 'R1', 'min_rows': True},
    {'requirement_id': 'R1', 'min_non_null': {'value': 0}},
    {'requirement_id': 'R1', 'shell': 'rm -rf /'},
    {'requirement_id': 'R1', 'allowed_values': {'method': []}},
])
def test_malformed_or_untraceable_checks_are_rejected(fields):
    with pytest.raises(ContractError):
        validate_checks([{'path': 'outputs/series.csv', **fields}], {'R1'})


def test_output_symlinks_cannot_turn_checks_into_input_reads(tmp_path):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    (inputs / 'private.json').write_text('{"value":10}')
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'outputs').symlink_to(inputs, target_is_directory=True)
    audit = evaluate_checks(work, [{'path': 'outputs/private.json', 'requirement_id': 'R1', 'finite_fields': ['/value']}])
    assert not audit['passed'] and 'escapes' in audit['checks'][0]['errors'][0]


def test_missing_output_or_required_column_remains_a_failure(tmp_path):
    (tmp_path / 'outputs').mkdir()
    check = {'path': 'outputs/series.tsv', 'requirement_id': 'R1', 'min_rows': 1, 'min_non_null': {'value': 1}}
    assert not evaluate_checks(tmp_path, [check])['passed']
    (tmp_path / 'outputs/series.tsv').write_text('method\naltimetry\n')
    assert not evaluate_checks(tmp_path, [check])['passed']


@pytest.mark.parametrize('value,passed', [
    (0, True), (1e-6, True), (5e-7, True), (1.0, False), (-1e-8, False),
    (None, False), (True, False), ('0', False), (float('nan'), False), (float('inf'), False)])
def test_finite_residual_must_also_satisfy_the_reviewed_tolerance(tmp_path, value, passed):
    (tmp_path / 'outputs').mkdir()
    path = tmp_path / 'outputs/metrics.json'
    path.write_text(json.dumps({'runs': [{'residual/relative': value}]}))
    check = {'path': 'outputs/metrics.json', 'requirement_id': 'R1',
             'numeric_bounds': {'/runs/0/residual~1relative': {'min': 0, 'max': 1e-6}}}
    audit = evaluate_checks(tmp_path, [check])
    assert audit['passed'] == passed
    assert audit['checks'][0]['numeric_bounds'] == check['numeric_bounds']
    assert path.read_text() == json.dumps({'runs': [{'residual/relative': value}]})


@pytest.mark.parametrize('bounds', [
    {}, [], {'/x': {}}, {'/x': {'minimum': 0}}, {'x': {'min': 0}},
    {'/x': {'min': True}}, {'/x': {'max': float('inf')}},
    {'/x': {'max': float('nan')}}, {'/x': {'max': '1e-6'}},
    {'/x': {'min': 2, 'max': 1}}, {'/x': {'min': 10**400}},
])
def test_ambiguous_or_non_numeric_bound_definitions_are_rejected(bounds):
    with pytest.raises(ContractError):
        validate_checks([{'path': 'outputs/metrics.json', 'requirement_id': 'R1',
                          'numeric_bounds': bounds}], {'R1'})


def test_one_sided_and_exact_count_bounds_remain_independent_of_scientific_acceptance(tmp_path):
    (tmp_path / 'outputs').mkdir()
    path = tmp_path / 'outputs/metrics.json'
    path.write_text('{"n_conditions":3,"error":0.0,"missing":null}')
    check = {'path': 'outputs/metrics.json', 'requirement_id': 'R1',
             'numeric_bounds': {'/n_conditions': {'min': 3, 'max': 3}, '/error': {'max': 1e-8}}}
    audit = evaluate_checks(tmp_path, [check])
    assert audit['passed'] and audit['scientific_validity'] == 'requires_independent_review'
    check['numeric_bounds']['/missing'] = {'min': 0}
    assert not evaluate_checks(tmp_path, [check])['passed']
