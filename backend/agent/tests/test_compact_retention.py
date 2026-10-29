import copy

from researchclaw.orchestration.compact_retention import compare_reviews


def review(statuses, score=8, **extra):
    return {'criteria': [{'id': name, 'status': status} for name, status in statuses.items()],
            'quality_score': score, 'missing_requirements': [], **extra}


def test_gained_criteria_cannot_hide_loss_of_an_existing_supported_result():
    old = review({'R1':'met','R2':'unmet','R3':'unmet'}, 6)
    new = review({'R1':'unmet','R2':'met','R3':'met'}, 9)
    decision = compare_reviews(old,new)
    assert decision['regressed'] and decision['lost_criteria'] == ['R1']


def test_completing_more_requirements_can_outweigh_a_lower_subjective_score():
    old = review({'R1':'met','R2':'unmet'},8)
    new = review({'R1':'met','R2':'met'},7)
    assert not compare_reviews(old,new)['regressed']
    assert compare_reviews(new,review({'R1':'met','R2':'met'},6))['regressed']


def test_missing_requirements_or_changed_contract_ids_are_not_comparable():
    old = review({'R1':'met'})
    new = review({'R1':'unmet'},missing_requirements=['An overlooked requirement'])
    assert not compare_reviews(old,new)['comparable']
    assert not compare_reviews(old,review({'R2':'unmet'}))['comparable']


def test_passing_dimension_must_not_disappear_or_be_marked_not_applicable():
    old = review({'R1':'met'},dimensions={'measurement_validity':{'status':'pass'}})
    for dimensions in [{}, {'measurement_validity':{'status':'not_applicable'}}]:
        new = review({'R1':'met'},dimensions=dimensions)
        decision = compare_reviews(old,new)
        assert decision['regressed'] and decision['lost_dimensions'] == ['measurement_validity']
