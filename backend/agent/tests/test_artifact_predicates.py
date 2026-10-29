"""Prevent malformed scientific artifacts from escaping the output audit."""
import hashlib
import zipfile

import numpy as np
import pytest
from PIL import Image

from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.execution_checks import evaluate_checks, validate_checks


def inspect(tmp_path, name, **predicates):
    return evaluate_checks(tmp_path, [{'path': f'outputs/{name}', 'requirement_id': 'R1', **predicates}])


def test_numeric_archive_checks_preserve_actual_artifact(tmp_path):
    (tmp_path / 'outputs').mkdir()
    path = tmp_path / 'outputs/curves.npz'
    np.savez(path, objective=np.arange(20).reshape(4, 5), residual=np.zeros(4))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = inspect(tmp_path, 'curves.npz', required_arrays=['objective', 'residual'],
                     finite_arrays=True, nonempty_arrays='all')
    assert result['passed']
    assert result['checks'][0]['arrays']['objective']['shape'] == [4, 5]
    assert result['scientific_validity'] == 'requires_independent_review'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('values,predicates,reason', [
    (np.array([1., np.nan]), {'finite_arrays': ['value']}, 'non-finite'),
    (np.array([]), {'nonempty_arrays': True}, 'empty'),
    (np.array([1]), {'required_arrays': ['absent']}, 'missing'),
])
def test_invalid_measurements_are_failed_checks(tmp_path, values, predicates, reason):
    (tmp_path / 'outputs').mkdir()
    np.savez(tmp_path / 'outputs/curves.npz', value=values)
    result = inspect(tmp_path, 'curves.npz', **predicates)
    assert not result['passed'] and reason in str(result['checks'][0]['errors'])


def test_plain_npy_supports_alias_and_numeric_validation(tmp_path):
    (tmp_path / 'outputs').mkdir()
    np.save(tmp_path / 'outputs/gradient.npy', np.array([1., 2.]))
    assert inspect(tmp_path, 'gradient.npy', required_arrays=['gradient'],
                   finite_arrays=['array'], nonempty_arrays=True)['passed']


@pytest.mark.parametrize('suffix', ['npy', 'npz'])
def test_object_arrays_are_never_unpickled(tmp_path, suffix):
    (tmp_path / 'outputs').mkdir()
    value = np.array([{'unsafe': 'object'}], dtype=object)
    if suffix == 'npy':
        np.save(tmp_path / 'outputs/result.npy', value)
    else:
        np.savez(tmp_path / 'outputs/result.npz', value=value)
    result = inspect(tmp_path, f'result.{suffix}', finite_arrays=True)
    assert not result['passed']
    assert any(term in str(result['checks'][0]['errors']).lower() for term in ('pickle', 'objects'))


@pytest.mark.parametrize('name,content', [('result.npz', b'not a zip'), ('result.npy', b'\x93NUMPY')])
def test_corrupt_arrays_return_audit_failures_instead_of_crashing(tmp_path, name, content):
    (tmp_path / 'outputs').mkdir()
    (tmp_path / 'outputs' / name).write_bytes(content)
    assert not inspect(tmp_path, name, finite_arrays=True)['passed']


def test_non_array_archive_member_is_an_audit_failure(tmp_path):
    (tmp_path / 'outputs').mkdir()
    with zipfile.ZipFile(tmp_path / 'outputs/result.npz', 'w') as archive:
        archive.writestr('notes', 'not an array')
    assert not inspect(tmp_path, 'result.npz', finite_arrays=True)['passed']


def test_png_format_is_verified_from_actual_bytes(tmp_path):
    (tmp_path / 'outputs').mkdir()
    path = tmp_path / 'outputs/result.png'
    Image.new('RGB', (12, 18), 'blue').save(path)
    result = inspect(tmp_path, 'result.png', image_format='PNG', min_size_bytes=20)
    assert result['passed'] and result['checks'][0]['image']['height'] == 18
    Image.new('RGB', (12, 18), 'red').save(path, format='JPEG')
    assert not inspect(tmp_path, 'result.png', image_format='PNG')['passed']
    path.write_text('fabricated png')
    assert not inspect(tmp_path, 'result.png', nonempty=True)['passed']


def test_text_size_check_cannot_substitute_for_scientific_validity(tmp_path):
    (tmp_path / 'outputs').mkdir()
    path = tmp_path / 'outputs/proof.md'
    path.write_text('')
    assert not inspect(tmp_path, 'proof.md', nonempty=True)['passed']
    path.write_text('A proof is still required.')
    result = inspect(tmp_path, 'proof.md', nonempty=True, min_size_bytes=5)
    assert result['passed'] and result['scientific_validity'] == 'requires_independent_review'


def test_directory_cannot_satisfy_a_nonempty_file_check(tmp_path):
    (tmp_path / 'outputs/proof.md').mkdir(parents=True)
    assert not inspect(tmp_path, 'proof.md', nonempty=True)['passed']


def test_unsupported_domain_assertion_gets_actionable_feedback():
    with pytest.raises(ContractError) as caught:
        validate_checks([{'path': 'outputs/coverage.csv', 'requirement_id': 'R1',
                          'conditional_non_null': {'estimate': 'status=ok'}}], {'R1'})
    error = str(caught.value)
    assert 'outputs/coverage.csv' in error and 'conditional_non_null' in error
    assert 'Allowed keys' in error and 'protocol/source assertions' in error
