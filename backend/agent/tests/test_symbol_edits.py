"""Original syntax boundaries prevent off-by-one replacement of a valid Python block."""
import ast

import pytest

from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.compact_revision import apply_edits, digest, normalize_scope_aliases
from researchclaw.orchestration.symbol_edits import symbol_packet


SOURCE = ('KEEP = 42\n\nclass Solver:\n    @staticmethod\n    def estimate(x):\n'
          '        result = {\n            "value": x,\n            "unit": "mwe",\n        }\n'
          '        return result\n\ndef unchanged():\n    return KEEP\n')


def patch(symbol, new, **changes):
    return {'symbol_edits': [{'path': 'main.py', 'sha256': digest(SOURCE), 'symbol': symbol, 'new': new, **changes}]}


def apply(tmp_path, value, visible=None):
    return apply_edits(tmp_path, {'main.py': SOURCE}, value,
                       visible_lines={'main.py': [(1, len(SOURCE.splitlines()))]} if visible is None else visible)['main.py']


def test_method_replacement_uses_ast_scope_and_preserves_unrelated_source(tmp_path):
    new = '@staticmethod\ndef estimate(x):\n    return {"value": x * 2, "unit": "mwe"}\n'
    changed = apply(tmp_path, patch('Solver.estimate', new))
    assert changed.startswith('KEEP = 42\n\nclass Solver:\n')
    assert changed.endswith('\ndef unchanged():\n    return KEEP\n')
    values = {}
    exec(compile(changed, 'owned-test-source', 'exec'), values)
    assert values['Solver'].estimate(3)['value'] == 6 and values['unchanged']() == 42


def test_named_metric_dictionary_replacement_preserves_owning_method(tmp_path):
    value = patch('Solver.estimate.result', 'result = {"value": x, "unit": "mwe", "condition": "primary"}')
    changed = apply(tmp_path, value)
    assert '"condition": "primary"' in changed and '        return result\n' in changed
    ast.parse(changed)
    packet = symbol_packet({'main.py': SOURCE}, {'main.py': [(1, 14)]})
    assert 'Solver.estimate.result' in packet and 'Solver.estimate' in packet


@pytest.mark.parametrize('value', [
    patch('missing', 'missing = 1'),
    patch('Solver.estimate', 'def different(x):\n    return x'),
    patch('Solver.estimate.result', 'result = {"value": 1}}'),
    patch('Solver.estimate.result', 'result = {}; unrelated = 1'),
    patch('Solver.estimate', 'def estimate(x):\n    return x', sha256='0'*64),
])
def test_invalid_or_unrelated_constructs_are_rejected_before_publication(tmp_path, value):
    with pytest.raises(ContractError):
        apply(tmp_path, value)
    assert not list(tmp_path.iterdir())


def test_hidden_or_overlapping_original_constructs_cannot_be_replaced(tmp_path):
    value = patch('Solver.estimate', '@staticmethod\ndef estimate(x):\n    return x')
    with pytest.raises(ContractError, match='omitted'):
        apply(tmp_path, value, visible={'main.py': [(1, 5), (10, 14)]})
    value['line_edits'] = [{'path': 'main.py', 'sha256': digest(SOURCE), 'start_line': 6, 'end_line': 6, 'new': 'pass'}]
    with pytest.raises(ContractError, match='Overlapping'):
        apply(tmp_path, value)


def test_ambiguous_binding_is_not_guessed_and_scope_alias_still_requires_hash(tmp_path):
    text = 'def main():\n    value = 1\n    value = 2\n'
    value = {'symbol_edits': [{'path': 'main.py', 'sha256': digest(text), 'symbol': 'main.value', 'new': 'value = 3'}]}
    with pytest.raises(ContractError, match='exactly one original'):
        apply_edits(tmp_path, {'main.py': text}, value, visible_lines={'main.py': [(1, 3)]})
    value = patch('Solver.estimate.result', 'result = {"value": x}', path='stage-11/experiment/main.py')
    normalized, aliases = normalize_scope_aliases(value, {'main.py': SOURCE}, 'stage-11/experiment')
    assert aliases and apply(tmp_path, normalized)


def test_current_file_module_prefix_preserves_the_exact_named_method_and_guards(tmp_path):
    new = '@staticmethod\ndef estimate(x):\n    return {"value": 2*x}\n'
    assert apply(tmp_path, patch('main.Solver.estimate', new)) == apply(tmp_path, patch('Solver.estimate', new))
    with pytest.raises(ContractError, match='exactly one original'):
        apply(tmp_path, patch('unrelated.Solver.estimate', new))
    with pytest.raises(ContractError, match='Stale'):
        apply(tmp_path, patch('main.Solver.estimate', new, sha256='0'*64))
    with pytest.raises(ContractError, match='omitted'):
        apply(tmp_path, patch('main.Solver.estimate', new), visible={'main.py': [(1, 5)]})


def test_real_lexical_scope_wins_over_a_possible_module_prefix(tmp_path):
    text = ('def load_inputs():\n    return "outer"\n\n'
            'def main():\n    def load_inputs():\n        return "inner"\n    return load_inputs()\n')
    value = {'symbol_edits': [{'path': 'main.py', 'sha256': digest(text), 'symbol': 'main.load_inputs',
                              'new': 'def load_inputs():\n    return "updated inner"\n'}]}
    changed = apply_edits(tmp_path, {'main.py': text}, value,
                          visible_lines={'main.py': [(1, len(text.splitlines()))]})['main.py']
    names = {}
    exec(compile(changed, 'owned-test-source', 'exec'), names)
    assert names['load_inputs']() == 'outer' and names['main']() == 'updated inner'


def test_module_prefix_cannot_hide_an_ambiguous_exact_binding(tmp_path):
    text = 'def main():\n    value = 1\n    value = 2\n\nvalue = 3\n'
    value = {'symbol_edits': [{'path': 'main.py', 'sha256': digest(text), 'symbol': 'main.value', 'new': 'value = 4'}]}
    with pytest.raises(ContractError, match='exactly one original'):
        apply_edits(tmp_path, {'main.py': text}, value, visible_lines={'main.py': [(1, 5)]})
