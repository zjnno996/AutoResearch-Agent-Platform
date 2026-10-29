"""Scientific decisions must not lose available focal derivations to a small excerpt quota."""
import json

import pytest

from researchclaw.config import RCConfig, validate_config
from researchclaw.orchestration.compact_generation import reference_packet


def base_config():
    return {'project': {'name': 'test'}, 'research': {'topic': 'Read scientific methods'},
            'runtime': {'timezone': 'UTC'}, 'notifications': {'channel': 'local'},
            'knowledge_base': {'root': '/tmp/source-context-test'}, 'llm': {'api_key_env': 'TEST_KEY'}}


def test_complete_focal_cached_source_is_supplied_when_phase_budget_allows(tmp_path):
    text = 'FOCAL_DEFINITION\n' + 'An exact derivation with assumptions.\n' * 4800 + 'FINAL_PROOF_STEP\n'
    path = tmp_path / 'focal.md'
    path.write_text(text)
    refs = {'references': [{'source': 'primary', 'text_path': str(path), 'context': 'Only an abstract.',
                            'named_framework_matches': ['A Named Method']},
                           {'source': 'supplement', 'context': 'ADMM operator identities.\n' * 1000}]}
    audit = []
    packet = reference_packet(refs, 256000, query='A Named Method and ADMM', audit=audit)
    assert text in packet and len(packet) <= 256000
    assert audit[0]['full_cached_text'] and audit[0]['cached_chars'] == len(text)
    assert 'supplement' in packet and audit[1]['included_chars'] > 1000
    small_audit = []
    small = reference_packet(refs, 16000, query='A Named Method and ADMM', audit=small_audit)
    assert not small_audit[0]['full_cached_text'] and len(small) <= 16000
    assert path.read_text() == text


def test_short_source_does_not_waste_the_other_sources_context_budget():
    refs = {'references': [{'source': 'short', 'context': 'Short source.'},
                           {'source': 'long', 'context': 'Long exact source derivation.\n' * 1000}]}
    audit = []
    packet = reference_packet(refs, 20000, query='exact source derivation', audit=audit)
    assert audit[0]['full_cached_text'] and audit[1]['allocated_chars'] > 19000
    assert 'short' in packet and 'long' in packet and len(packet) <= 20000


def test_full_context_does_not_read_a_prohibited_cached_source(tmp_path):
    prohibited = tmp_path / 'target_study/source.md'
    prohibited.parent.mkdir()
    prohibited.write_text('NEVER_READ_HIDDEN_CONTENT')
    refs = {'references': [{'source': 'public', 'text_path': str(prohibited),
                            'context': 'Available public abstract.', 'named_framework_matches': ['Method']}]}
    audit = []
    packet = reference_packet(refs, 256000, query='Method', audit=audit)
    assert 'NEVER_READ_HIDDEN_CONTENT' not in packet
    assert audit[0]['cached_chars'] == len('Available public abstract.')


@pytest.mark.parametrize('key,value', [('source_context_chars', True), ('source_context_chars', 1),
                                      ('formal_source_context_chars', 1000001)])
def test_invalid_reference_budgets_are_rejected(key, value):
    data = json.loads(json.dumps(RCConfig.from_dict(base_config()).to_dict()))
    data['research_engine'][key] = value
    assert any(key in error for error in validate_config(data, check_paths=False).errors)


def test_reference_budgets_roundtrip_in_configuration():
    data = base_config()
    data['research_engine'] = {'source_context_chars': 48000, 'formal_source_context_chars': 200000}
    cfg = RCConfig.from_dict(data)
    reloaded = RCConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert reloaded.research_engine.source_context_chars == 48000
    assert reloaded.research_engine.formal_source_context_chars == 200000


def test_nearly_fitting_method_does_not_lose_half_its_text_to_background_reservations():
    text = ('PRIMARY_ASSUMPTIONS\n' + 'Full original method derivation.\n' * 4000)[:92200] + 'FINAL_METHOD_STEP\n'
    refs = {'references': [{'source': f'background-{i}', 'context': 'Background observations.\n' * 1500}
                           for i in range(5)] + [
        {'source': 'primary', 'context': text, 'task_identity_matches': ['NamedCorpus'],
         'public_code_urls': ['https://github.com/authors/method']}]}
    audit = []
    packet = reference_packet(refs, 96000, query='NamedCorpus observations', audit=audit)
    assert text in packet and len(packet) <= 96000
    assert audit[-1]['full_cached_text'] and all(row['included_chars'] > 0 for row in audit[:-1])
    assert 'Public code links (not fetched): ["https://github.com/authors/method"]' in packet
    assert all(f'background-{i}' in packet for i in range(5))


@pytest.mark.parametrize('limit,complete', [(48000, True), (40000, False)])
def test_multiple_focal_methods_are_preserved_together_or_share_the_bounded_packet(limit, complete):
    methods = [{'source': name, 'context': name + '\n' + 'Original derivation.\n' * 1050,
                'named_framework_matches': [name]} for name in ['method-A', 'method-B']]
    for ordered in [methods, list(reversed(methods))]:
        audit = []
        packet = reference_packet({'references': ordered + [
            {'source': 'background', 'context': 'Related study.\n' * 1000}]},
            limit, query='Compare method-A and method-B', audit=audit)
        assert len(packet) <= limit and all(row['included_chars'] > 15000 for row in audit[:2])
        assert all(row['full_cached_text'] == complete for row in audit[:2])
        assert audit[-1]['included_chars'] > 0


def test_paper_linked_code_is_distinct_unexecuted_evidence_within_the_same_budget(tmp_path):
    code = 'AUTHOR_METHOD_FUNCTION\n' + 'Original algorithm and exact settings.\n' * 700 + 'FINAL_CODE_LINE\n'
    path = tmp_path / 'code.md'
    path.write_text(code)
    paper = 'PRIMARY_PAPER\n' + 'Scientific assumptions and derivation.\n' * 2500
    references = [{'source': 'background', 'context': 'Related observations.\n' * 3000},
        {'source': 'primary-paper', 'context': paper, 'task_identity_matches': ['NamedCorpus'],
         'public_code': {'status': 'partial', 'repository_url': 'https://github.com/authors/method',
                         'commit_sha': 'a'*40, 'packet_path': str(path),
                         'source_scope': 'Selected original author-code lines; full repository omitted.'}}]
    audit = []
    packet = reference_packet({'references': references}, 96000, query='NamedCorpus method', audit=audit)
    assert len(packet) <= 96000 and code in packet and len(audit) == 3
    assert audit[-1]['full_cached_text'] and audit[1]['included_chars'] > 50000
    assert 'not executed experimental evidence' in packet and 'a'*40 in packet
    assert references[-1]['source'] == 'primary-paper' and len(references) == 2


def test_duplicate_publisher_urls_do_not_reduce_unique_method_and_code_coverage(tmp_path):
    code = 'EXACT_AUTHOR_METHOD\n' + 'Complete original computation.\n' * 1150 + 'END_AUTHOR_METHOD\n'
    path = tmp_path / 'packet.md';path.write_text(code)
    paper = 'PRIMARY_METHOD\n' + 'Original source equations.\n' * 3500
    acquired = {'status':'partial','repository_url':'https://github.com/authors/method',
                'commit_sha':'a'*40,'packet_path':str(path),'source_scope':'Partial author source, unexecuted.'}
    references = [
        {'source':'https://link.springer.com/article/10.1038/s12345-024-12345-x?fromPaywallRec=true',
         'context':paper,'task_identity_matches':['NamedCorpus'],'public_code':acquired},
        {'source':'https://www.nature.com/articles/s12345-024-12345-x:',
         'context':paper,'task_identity_matches':['NamedCorpus']}]
    audit=[]
    packet=reference_packet({'references':references},96000,query='NamedCorpus',audit=audit)
    assert len(audit)==2 and len(packet)<=96000 and code in packet
    assert audit[-1]['full_cached_text'] and 'Other URLs for this same source' in packet
    assert len(references)==2 and 'alias_sources' not in references[0]


def test_prepared_code_keeps_whole_statements_when_multiple_distinct_papers_compete(tmp_path):
    code='SOURCE_EQUATIONS\n'+'A complete original computation.\n'*1100+'FINAL_EQUATION\n'
    path=tmp_path/'packet.md';path.write_text(code)
    papers=[{'source':f'https://doi.org/10.1000/paper-{i}','context':'Method equations.\n'*5500,
             'task_identity_matches':['NamedCorpus']} for i in range(2)]
    papers[0]['public_code']={'status':'partial','repository_url':'https://github.com/authors/method',
                             'commit_sha':'b'*40,'packet_path':str(path),'source_scope':'Selected original statements.'}
    audit=[]
    packet=reference_packet({'references':papers},96000,query='NamedCorpus',audit=audit)
    assert code in packet and len(packet)<=96000 and len(audit)==3
    assert all(row['included_chars']>15000 for row in audit[:2])


def test_small_code_phase_repackages_coherent_evidence_instead_of_text_slicing(tmp_path,monkeypatch):
    path=tmp_path/'packet.md';path.write_text('Original full prepared code.\n'*1500)
    calls=[]
    def coherent(evidence,query,limit):
        calls.append((query,limit))
        return 'COHERENT_ORIGINAL_STATEMENTS_AND_PREREQUISITES'[:limit]
    monkeypatch.setattr('researchclaw.orchestration.public_code.render_code_context',coherent)
    refs=[{'source':'primary','context':'Scientific method.\n'*2000,'task_identity_matches':['NamedCorpus'],
           'public_code':{'status':'partial','repository_url':'https://github.com/authors/method',
                          'commit_sha':'c'*40,'packet_path':str(path),'files':[{'path':'method.py'}]}}]
    packet=reference_packet({'references':refs},12000,query='NamedCorpus')
    assert len(calls)==1 and 0<calls[0][1]<path.stat().st_size
    assert 'COHERENT_ORIGINAL_STATEMENTS_AND_PREREQUISITES' in packet and len(packet)<=12000
