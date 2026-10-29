"""A known execution-scope prefix is metadata, not a reason to regenerate science."""
from copy import deepcopy

import pytest

from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.compact_revision import apply_edits, digest, normalize_scope_aliases


def edit(path='stage-25/scientific_analysis.md', **changes):
    return {'line_edits': [{'path': path, 'sha256': digest('Old argument.\n'),
                           'start_line': 1, 'end_line': 1, 'new': 'Correct argument.\n', **changes}]}


def test_exact_scope_alias_preserves_hash_line_addresses_and_scientific_text(tmp_path):
    original = {'scientific_analysis.md': 'Old argument.\n'}
    value = edit()
    before = deepcopy(value)
    normalized, aliases = normalize_scope_aliases(value, original, 'stage-25')
    assert value == before and aliases == [{'original': 'stage-25/scientific_analysis.md',
                                           'scope_relative': 'scientific_analysis.md'}]
    assert normalized['line_edits'][0] == {**before['line_edits'][0], 'path': 'scientific_analysis.md'}
    assert apply_edits(tmp_path, original, normalized, visible_lines={'scientific_analysis.md': [(1, 1)]}) == {
        'scientific_analysis.md': 'Correct argument.\n'}


@pytest.mark.parametrize('path', ['/stage-25/scientific_analysis.md', 'other/scientific_analysis.md',
                                  'stage-25/../scientific_analysis.md', 'stage-25/missing.md'])
def test_no_basename_absolute_parent_or_missing_file_guessing(tmp_path, path):
    value = edit(path)
    normalized, aliases = normalize_scope_aliases(value, {'scientific_analysis.md': 'Old argument.\n'}, 'stage-25')
    assert normalized == value and not aliases
    with pytest.raises(ContractError):
        apply_edits(tmp_path, {'scientific_analysis.md': 'Old argument.\n'}, normalized,
                    visible_lines={'scientific_analysis.md': [(1, 1)]})


def test_alias_cannot_bypass_stale_hash_or_omitted_line_guard(tmp_path):
    original = {'scientific_analysis.md': 'Old argument.\n'}
    normalized, _ = normalize_scope_aliases(edit(sha256='0'*64), original, 'stage-25')
    with pytest.raises(ContractError, match='Stale'):
        apply_edits(tmp_path, original, normalized, visible_lines={'scientific_analysis.md': [(1, 1)]})
    normalized, _ = normalize_scope_aliases(edit(), original, 'stage-25')
    with pytest.raises(ContractError, match='omitted'):
        apply_edits(tmp_path, original, normalized, visible_lines={})


def test_new_source_files_do_not_gain_aliases_and_exact_existing_paths_take_precedence():
    value = {'add_files': [{'path': 'stage-11/experiment/helper.py', 'content': 'pass\n'}]}
    assert normalize_scope_aliases(value, {}, 'stage-11/experiment') == (value, [])
    value = edit()
    originals = {'scientific_analysis.md': 'Old argument.\n', 'stage-25/scientific_analysis.md': 'Different source.\n'}
    assert normalize_scope_aliases(value, originals, 'stage-25') == (value, [])
