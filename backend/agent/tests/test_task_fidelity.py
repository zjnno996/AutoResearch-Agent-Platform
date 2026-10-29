"""High scores and populated files must not close admitted method substitutions."""
from copy import deepcopy

import pytest

from researchclaw.orchestration.compact_contracts import ContractError, validate_plan, validate_review
from researchclaw.orchestration.task_fidelity import enforce_review_consistency, seal_contract, validate_task_identity
from researchclaw.orchestration.task_fidelity import instruction_sources, induction_context


TASK = 'Reconcile independent source observations into annual regional estimates, including uncertainty.'


def contract(kind='experiment'):
    return seal_contract({'requirements': [{'id': 'R1', 'kind': kind,
        'requirement': 'Reconcile independent source observations',
        'acceptance': 'Actual input-derived annual estimates with uncertainty',
        'instruction_quotes': ['Reconcile independent source observations', 'including uncertainty']}]}, TASK)


def review(**changes):
    item = {'id': 'R1', 'status': 'met', 'reason': 'Files exist and reported counts are correct.',
            'evidence': ['stage-11/experiment/outputs/annual.csv'], 'evidence_route': 'direct',
            'mechanism_match': 'yes', 'artifact_match': 'complete', **changes}
    return {'criteria': [item], 'missing_requirements': [], 'quality_score': 10,
            'summary': 'Optimistic scientific review',
            'dimensions': {'task_fidelity': {'status': 'pass', 'reason': 'Claimed full completion'}}}


def test_instruction_scope_is_bound_to_literal_original_task():
    scope = contract()
    validate_task_identity(scope, TASK)
    with pytest.raises(ContractError, match='different RAW'):
        validate_task_identity(scope, 'Reformat the published combined annual product.')
    changed = deepcopy(scope)
    changed['requirements'][0]['instruction_quotes'] = ['Run an optional ablation from a skill reference']
    with pytest.raises(ContractError, match='literal RAW'):
        validate_task_identity(changed, TASK)


@pytest.mark.parametrize('change,expected', [
    ({'evidence_route': 'context_only'}, 'route is context_only'),
    ({'evidence_route': 'unavailable'}, 'route is unavailable'),
    ({'mechanism_match': 'no'}, 'mechanism match is no'),
    ({'mechanism_match': 'partial'}, 'mechanism match is partial'),
    ({'mechanism_match': 'not_applicable'}, 'cannot be marked not_applicable'),
    ({'artifact_match': 'partial'}, 'artifact coverage is partial'),
    ({'artifact_match': 'missing'}, 'artifact coverage is missing'),
])
def test_admitted_core_gaps_remain_unverified_despite_score_ten(change, expected):
    original = review(**change)
    before = deepcopy(original)
    validated = enforce_review_consistency(original, contract())
    row = validated['criteria'][0]
    assert row['status'] == 'unverified' and row['model_status'] == 'met'
    assert row['owner_phase'] == 'implement' and expected in row['reason']
    assert original == before and validated['quality_score'] == 10


def test_faithful_transparent_reconstruction_can_close_the_original_operation():
    original = review(evidence_route='transparent_surrogate')
    validated = enforce_review_consistency(original, contract())
    assert validated['criteria'][0]['status'] == 'met'
    assert 'model_status' not in validated['criteria'][0]


def test_written_proof_gaps_route_to_report_and_cannot_be_waived():
    validated = enforce_review_consistency(review(mechanism_match='partial'), contract(kind='proof'))
    assert validated['criteria'][0]['owner_phase'] == 'report'
    assert validated['criteria'][0]['status'] == 'unverified'


def test_report_only_artifact_does_not_need_a_numerical_mechanism():
    validated = enforce_review_consistency(review(mechanism_match='not_applicable'), contract(kind='report'))
    assert validated['criteria'][0]['status'] == 'met'


def test_new_contract_cannot_omit_evidence_route_checks():
    value = review()
    del value['criteria'][0]['mechanism_match']
    with pytest.raises(ContractError, match='mechanism_match'):
        validate_review(value, contract())
    plan = {'methods': ['reconcile'], 'datasets': ['observations'], 'metrics': ['uncertainty'],
            'protocol': {'command': 'python main.py'},
            'requirement_map': {'R1': {'approach': 'Read published combined values', 'artifact': 'outputs/annual.csv'}}}
    with pytest.raises(ContractError, match='evidence_route'):
        validate_plan(plan, contract())
    plan['requirement_map']['R1'].update(evidence_route='context_only', must_preserve='Reconcile source estimates')
    validate_plan(plan, contract())  # Planning a partial route does not claim execution success.


def test_old_contracts_keep_their_original_validation_protocol():
    legacy = {'requirements': [{'id': 'R1', 'requirement': 'An old task', 'acceptance': 'Original evidence'}]}
    value = review()
    del value['criteria'][0]['mechanism_match']
    validate_review(value, legacy)
    assert enforce_review_consistency(value, legacy) is value


def test_controller_fills_exact_markdown_and_punctuation_from_source_ids():
    topic = '**Figures are mandatory** — save to `report/images/`.\nReport path: `report/report.md`.'
    source = {'requirements': [{'id': 'R1', 'requirement': 'Generate figures', 'acceptance': 'Real images',
                               'instruction_sources': ['T1']}]}
    sealed = seal_contract(source, topic)
    assert sealed['requirements'][0]['instruction_quotes'] == [topic.splitlines()[0]]
    assert 'instruction_quotes' not in source['requirements'][0]
    validate_task_identity(sealed, topic)
    sealed['requirements'][0]['instruction_quotes'] = ['Figures are mandatory']
    with pytest.raises(ContractError, match='quotation changed'):
        validate_task_identity(sealed, topic)


def test_invalid_source_id_cannot_quote_advisory_method_guidance_as_task_scope():
    value = {'requirements': [{'id': 'R1', 'instruction_sources': ['method_guidance']} ]}
    with pytest.raises(ContractError, match='existing RAW source IDs'):
        seal_contract(value, TASK)
    assert 'Match data, budgets' not in induction_context(TASK)
    assert 'mechanism ablations from baseline' not in induction_context(TASK)


def test_long_instruction_fragments_remain_bounded_exact_original_substrings():
    topic = 'Required observation and mechanism. ' * 200
    sources = instruction_sources(topic)
    assert len(sources) > 1 and all(0 < len(text) <= 1200 and text in topic for text in sources.values())
    value = {'requirements': [{'id': 'R1', 'instruction_sources': list(sources)}]}
    sealed = seal_contract(value, topic)
    validate_task_identity(sealed, topic)
