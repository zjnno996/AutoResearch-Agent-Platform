"""Method citations select original bytes instead of retyping controller guidance."""
import jsonschema
import pytest

from researchclaw.orchestration.compact_state import file_hash
from researchclaw.orchestration.json_contracts import protocol_schema
from researchclaw.orchestration.method_probes import validate_method_sources
from researchclaw.orchestration.source_anchors import build_source_anchors, resolve_source_anchors


TASK = 'Compute the skew map for the supplied state.\nPreserve the signs in the original equation.'


def test_method_source_selection_preserves_exact_original_equations_and_offsets(tmp_path):
    path = tmp_path/'paper.md'
    text = 'Original skew operator and its scientific assumptions:\nF(x,z) = (z, −x).\nThe skew inner product vanishes.\n'
    path.write_text(text)
    references = [{'source': 'original-paper', 'text_path': str(path), 'text_sha256': file_hash(path)}]
    catalog = build_source_anchors(TASK, references, 'skew operator')
    anchor = next(row for row in catalog['anchors'] if row['reference'] == 'original-paper')
    value = {'method_steps': [{'source': {'anchor_id': anchor['id']}}]}
    resolve_source_anchors(value, catalog)
    bound = validate_method_sources(value, TASK, references)[0]
    assert bound['quote'] == text == path.read_text()
    assert bound['source_anchor_id'] == anchor['id']
    assert bound['quote_resolution'] == 'controller_original_source_span'
    assert bound['source_sha256'] == file_hash(path)
    assert text[bound['start_character']:bound['end_character']] == bound['quote']
    assert build_source_anchors(TASK, references, 'skew operator') == catalog


@pytest.mark.parametrize('defect', ['unknown_id', 'retyped_quote', 'different_reference', 'changed_source', 'changed_span'])
def test_generated_citations_cannot_override_an_original_anchor(tmp_path, defect):
    path = tmp_path/'paper.md';path.write_text('A source-defined operator is F(x,z)=(z,-x) under the stated conditions.\n')
    references = [{'source': 'original-paper', 'text_path': str(path), 'text_sha256': file_hash(path)}]
    catalog = build_source_anchors(TASK, references)
    anchor = next(row for row in catalog['anchors'] if row['reference'] == 'original-paper')
    value = {'method_steps': [{'source': {'anchor_id': anchor['id']}}]}
    if defect == 'unknown_id':
        value['method_steps'][0]['source']['anchor_id'] = 'Ainvented-controller-guidance'
    elif defect == 'retyped_quote':
        value['method_steps'][0]['source']['quote'] = 'F(x,z)=(z,+x)'
    elif defect == 'different_reference':
        value['method_steps'][0]['source']['reference'] = 'task'
    with pytest.raises(ValueError):
        resolve_source_anchors(value, catalog)
        if defect == 'changed_source':
            path.write_text(path.read_text().replace('-x', '+x'))
        elif defect == 'changed_span':
            value['method_steps'][0]['source']['start_character'] += 1
        validate_method_sources(value, TASK, references)


def test_task_anchors_exclude_internal_method_guidance_and_have_unique_ids():
    guidance = 'When documentation defines interval totals, derive rates using the actual fractional duration before calendar aggregation.'
    catalog = build_source_anchors(TASK+'\n'+TASK, [], guidance)
    assert len({row['id'] for row in catalog['anchors']}) == len(catalog['anchors'])
    assert all(row['quote'] in TASK for row in catalog['anchors'])
    assert all(guidance not in row['quote'] for row in catalog['anchors'])
    properties = protocol_schema({'requirements': [{'id': 'R1'}]}, [], formal=False,
        source_anchor_ids=[row['id'] for row in catalog['anchors']])['properties']['method_steps']['items']['properties']['source']
    jsonschema.validate({'anchor_id': catalog['anchors'][0]['id']}, properties)
    for invalid in [{'anchor_id': 'Ainvented'}, {'reference': 'task', 'quote': guidance}]:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, properties)


def test_anchor_selection_is_bounded_and_does_not_claim_complete_source_coverage(tmp_path):
    path = tmp_path/'long-paper.md'
    path.write_text('\n'.join('Section '+str(index)+' describes the source-defined skew operator and its assumptions.' for index in range(1500)))
    refs = [{'source': 'long-paper', 'text_path': str(path), 'text_sha256': file_hash(path)}]
    catalog = build_source_anchors(TASK, refs, 'skew operator assumptions')
    assert len(catalog['anchors']) <= 64
    assert all(len(row['quote']) <= 1200 for row in catalog['anchors'])
    assert 'Unselected text is not evidence of absence' in catalog['scope']
    assert all(row['quote'] in (TASK if row['reference'] == 'task' else path.read_text()) for row in catalog['anchors'])


@pytest.mark.parametrize('representation,expression', [
    ('latex', r'$$s_{i,i+1}=\frac{a_i+b_{i+1}}{c_i}-1$$'),
    ('mathml', '<math><mrow>'+('<mi>x</mi><mo>+</mo>'*100)+'<mi>y</mi></mrow></math>'),
])
def test_original_equation_remains_selectable_when_topic_prose_would_outscore_it(tmp_path, representation, expression):
    path = tmp_path/'equation-paper.md'
    formula = f'[Original JATS formula id=E3; label=3; representation={representation}]\n```{representation}\n{expression}\n```'
    text = ('The source-defined skew operator assumptions and scientific method.\n'*600)+'\n'+formula+'\n'
    path.write_text(text)
    refs = [{'source': 'original-paper', 'text_path': str(path), 'text_sha256': file_hash(path)}]
    catalog = build_source_anchors(TASK, refs, 'skew operator assumptions scientific method')
    anchor = next(row for row in catalog['anchors'] if formula in row['quote'])
    plan = {'method_steps': [{'source': {'anchor_id': anchor['id']}}]}
    resolve_source_anchors(plan, catalog)
    bound = validate_method_sources(plan, TASK, refs)[0]
    assert expression in bound['quote']
    assert text[bound['start_character']:bound['end_character']] == bound['quote']
    assert len(catalog['anchors']) <= 64
