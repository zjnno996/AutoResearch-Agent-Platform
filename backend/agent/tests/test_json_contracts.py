"""Generation constraints cover observed procedural replies and missing fields."""
import pytest
import jsonschema

from researchclaw.orchestration.json_contracts import protocol_schema


def example():
    return {'methods': ['Measure the requested quantity'], 'datasets': ['supplied input'],
        'dataset_policy': 'Use only the supplied input; no replacement dataset.',
        'metrics': ['residual'], 'protocol': {'command': 'python main.py'},
        'requirement_map': {'R1': {'approach': 'Execute the method', 'artifact': 'outputs/metrics.json',
            'evidence_route': 'direct', 'must_preserve': 'The requested operation'}},
        'method_steps': [{'id': 'M1', 'requirement_ids': ['R1'], 'operation': 'Skew coupling',
            'inputs': 'two coordinates', 'outputs': 'two coordinates', 'assumptions': ['Real inputs'],
            'source': {'reference': 'task', 'quote': 'Use the skew coupling'},
            'binding': {'module': 'scientific_methods.py', 'callable': 'coupling'}}],
        'method_probes': [{'id': 'P1', 'method_step_id': 'M1', 'kind': 'analytic',
            'basis': 'Independent tiny case', 'wrong_alternative': 'Wrong sign',
            'code': 'def check(invoke):\n    assert invoke([0,1]) == [1,0]\n'}]}


def test_real_scientific_fields_and_extra_derivations_fit_the_generation_schema():
    schema = protocol_schema({'requirements': [{'id': 'R1'}]}, [], formal=True)
    jsonschema.Draft202012Validator.check_schema(schema)
    value = {**example(), 'scientific_analysis_markdown': 'Explicit derivation and its assumptions.',
             'additional_scientific_conditions': ['A condition retained for independent review']}
    jsonschema.validate(value, schema)


@pytest.mark.parametrize('defect', ['procedural', 'missing_metrics', 'unknown_reference', 'unbound_requirement', 'standalone_probe', 'bare_probe_signature', 'unindented_probe', 'missing_derivation', 'missing_dataset_policy', 'empty_dataset_policy'])
def test_observed_incomplete_handoffs_are_outside_the_generation_contract(defect):
    schema = protocol_schema({'requirements': [{'id': 'R1'}]}, [], formal=True)
    value = {**example(), 'scientific_analysis_markdown': 'Explicit derivation.'}
    if defect == 'procedural':
        value = {'step': 'Read the data first', 'type': 'analysis'}
    elif defect == 'missing_metrics':
        del value['metrics']
    elif defect == 'unknown_reference':
        value['method_steps'][0]['source']['reference'] = 'task / guessed paper section'
    elif defect == 'unbound_requirement':
        value['requirement_map'] = {}
    elif defect == 'standalone_probe':
        value['method_probes'][0]['code'] = 'from scientific_methods import coupling\nassert coupling([0,1]) == [1,0]'
    elif defect == 'bare_probe_signature':
        value['method_probes'][0]['code'] = 'def check(invoke):'
    elif defect == 'unindented_probe':
        value['method_probes'][0]['code'] = 'def check(invoke):\nassert invoke([0,1]) == [1,0]'
    elif defect == 'missing_derivation':
        del value['scientific_analysis_markdown']
    elif defect == 'missing_dataset_policy':
        value['datasets'] = []
        del value['dataset_policy']
    elif defect == 'empty_dataset_policy':
        value['datasets'] = []
        value['dataset_policy'] = ''
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, schema)
