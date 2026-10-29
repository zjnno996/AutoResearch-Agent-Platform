"""A review-letter PDF must not replace its parent article's scientific methods."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.orchestration.grounding import discover_sources, primary_url
from researchclaw.orchestration.primary_sources import pmc_identifier, render_bioc, public_code_urls


def test_public_author_code_links_survive_markdown_escaped_fetch_content():
    text = (r'Author code: https\:\/\/github\.com\/authors\/method\_code. '
            'Mirror https://github.com/authors/method_code; '
            'not a repository: https://github.com.evil/authors/method. '
            r'An equation \(a\_b\) is preserved.')
    original = text
    assert public_code_urls(text) == ['https://github.com/authors/method_code']
    assert text == original


def bioc(identifier='PMC98765'):
    return [{'documents': [{'id': identifier, 'infons': {'license': 'CC BY'}, 'passages': [
        {'offset': 0, 'infons': {'section_type': 'TITLE', 'article-id_doi': '10.1000/example'}, 'text': 'Canonical method for NamedCorpus'},
        {'offset': 100, 'infons': {'section_type': 'METHODS'},
         'text': 'NamedCorpus observations are harmonized by documented units and source intervals. ' * 20
                 + 'Code availability: https://github.com/authors/method.'},
        {'offset': 2000, 'infons': {'section_type': 'REF'}, 'text': 'Reference-list-only text.'}]}]}]


def test_pmc_supplement_and_instance_urls_resolve_to_one_canonical_article():
    expected = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC98765/'
    assert primary_url('https://pmc.ncbi.nlm.nih.gov/articles/instance/98765/bin/review.pdf') == expected
    assert primary_url('https://pmc.ncbi.nlm.nih.gov/articles/PMC98765/bin/data.pdf') == expected
    assert pmc_identifier('https://pmc.ncbi.nlm.nih.gov.evil/98765') == ''
    assert primary_url('https://pmc.ncbi.nlm.nih.gov/articles/PMC98765/target_study/checklist.json') == ''


def test_structured_body_preserves_methods_offsets_and_explicit_scope():
    result = render_bioc(bioc(), 'PMC98765')
    assert result['title'] == 'Canonical method for NamedCorpus'
    assert 'documented units' in result['content'] and 'offset 100' in result['content']
    assert 'Reference-list-only' not in result['content']
    assert result['document_metadata']['omitted_passages'] == {'REF': 1}
    assert result['raw_bioc'] == bioc()
    with pytest.raises(ValueError, match='requested public article'):
        render_bioc(bioc('PMC11111'), 'PMC98765')


@pytest.mark.parametrize('available', [True, False])
def test_parent_recovery_never_sends_provider_credentials_or_mislabels_a_supplement(tmp_path, monkeypatch, available):
    sent = []
    class Response:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(self.value).encode()
    def request(req, **kwargs):
        sent.append(req.full_url)
        if req.full_url.startswith('https://www.ebi.ac.uk/'):
            assert req.get_header('Authorization') is None
            raise OSError('Original JATS unavailable; exercise BioC fallback')
        if req.full_url.startswith('https://www.ncbi.nlm.nih.gov/'):
            assert req.get_method() == 'GET' and req.get_header('Authorization') is None
            if not available:
                raise OSError('Public full text unavailable')
            return Response(bioc())
        assert req.full_url == 'https://api.deepseek.com/v1/tools/search'
        assert req.get_header('Authorization') == 'Bearer private-fixture'
        return Response({'results': [{'url': 'https://pmc.ncbi.nlm.nih.gov/articles/instance/98765/bin/review.pdf',
            'title': 'Editorial correspondence', 'content': 'REVIEW_LETTER_NOT_METHODS. ' * 100}]})
    monkeypatch.setattr('urllib.request.urlopen', request)
    config = SimpleNamespace(research=SimpleNamespace(topic='Reconcile **NamedCorpus** observations.'),
        research_engine=SimpleNamespace(public_source_search=True, max_source_queries=2, max_public_sources=2))
    llm = SimpleNamespace(base_url='https://api.deepseek.com/v1', api_key='private-fixture')
    refs = discover_sources(tmp_path, config, llm, {'source_queries': ['NamedCorpus methodology']})
    assert len(refs) == 1 and len(sent) == 3
    text = Path(refs[0]['text_path']).read_text()
    if available:
        assert refs[0]['source'] == 'https://pmc.ncbi.nlm.nih.gov/articles/PMC98765/'
        assert refs[0]['content_origin'] == 'pmc_bioc'
        assert 'REVIEW_LETTER_NOT_METHODS' not in text and 'documented units' in text
        assert refs[0]['task_identity_matches'] == ['NamedCorpus']
        assert refs[0]['public_code_urls'] == ['https://github.com/authors/method']
        assert Path(refs[0]['raw_bioc_path']).is_file()
        assert refs[0]['document_metadata']['jats_recovery']['formulas_complete'] is False
        assert 'may omit' in refs[0]['source_scope']
    else:
        assert refs[0]['retrieval_status'] == 'supplement_only'
        assert refs[0]['source'].endswith('/bin/review.pdf')
    audit = (tmp_path/'public_source_retrieval.json').read_text()
    assert 'private-fixture' not in audit and '"llm_calls": 0' in audit
