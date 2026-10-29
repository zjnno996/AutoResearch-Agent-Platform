import io
import json
from types import SimpleNamespace

import pytest

from researchclaw.orchestration.scholarly_sources import ScholarlySources
from researchclaw.orchestration.grounding import discover_sources
from researchclaw.orchestration.artifacts import read_json


def test_qwen_scholarly_lookup_never_forwards_model_credentials(tmp_path, monkeypatch):
    requests = []
    article = 'https://www.nature.com/articles/s41586-024-08545-z'
    def request(req, **kwargs):
        requests.append(req)
        assert req.get_header('Authorization') is None
        if req.full_url.startswith('https://api.crossref.org/'):
            raw = json.dumps({'message': {'items': [{'DOI': '10.1038/s41586-024-08545-z',
                'title': ['An independently indexed paper'], 'resource': {'primary': {'URL': article}}}]}}).encode()
        elif req.full_url.startswith('https://www.ebi.ac.uk/europepmc/webservices/rest/search?'):
            raw = json.dumps({'resultList': {'result': []}}).encode()
        else:
            assert req.full_url == article
            raw = ('<h1>Primary article</h1><p>Scientific method and assumptions. '+ 'observed evidence '*100+'</p>').encode()
        response = io.BytesIO(raw); response.headers = {'Content-Type': 'text/html'}
        return response
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: SimpleNamespace(open=request))
    config = SimpleNamespace(research_engine=SimpleNamespace(public_source_search=True,max_source_queries=1,max_public_sources=1),
                             research=SimpleNamespace(topic='An independently indexed paper'))
    llm = SimpleNamespace(base_url='http://model.test/v1',primary_model='Qwen3.8-27B-FP8',api_key='private-fixture-key')
    references = discover_sources(tmp_path,config,llm,{'source_queries':['indexed scientific method']})
    assert len(references)==1 and len(requests)==3
    audit=read_json(tmp_path/'public_source_retrieval.json')
    assert audit['transport']=='public_scholarly_http' and audit['llm_calls']==0
    assert 'private-fixture-key' not in json.dumps(audit)
    assert all('deepseek'not in req.full_url for req in requests)


def test_crossref_outage_falls_back_to_an_independent_original_article_index(tmp_path, monkeypatch):
    import urllib.error
    reader = ScholarlySources(tmp_path)
    calls = []
    def read(url):
        calls.append(url)
        if url.startswith('https://api.crossref.org/'):
            raise urllib.error.URLError('Fixture index unavailable')
        assert url.startswith('https://www.ebi.ac.uk/europepmc/webservices/rest/search?')
        return json.dumps({'resultList': {'result': [{'title': 'The requested original scientific paper',
            'doi': '10.1000/original', 'pmcid': 'PMC12345'}]}}).encode(), 'application/json'
    monkeypatch.setattr(reader, '_read', read)
    result = reader.search('The requested original scientific paper')['results'][0]
    assert len(calls) == 2
    assert result['url'] == 'https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/'
    assert result['document_metadata']['doi'] == '10.1000/original'
    assert 'metadata only' in result['content']


def test_open_fulltext_resolution_requires_the_same_independently_indexed_doi(tmp_path, monkeypatch):
    reader = ScholarlySources(tmp_path)
    article = 'https://www.nature.com/articles/s41586-024-08545-z'
    def read(url):
        if url.startswith('https://api.crossref.org/'):
            return json.dumps({'message': {'items': [{'DOI': '10.1038/s41586-024-08545-z',
                'title': ['The requested source'], 'resource': {'primary': {'URL': article}}}]}}).encode(), 'application/json'
        return json.dumps({'resultList': {'result': [
            {'title': 'Different article', 'doi': '10.1000/unrelated', 'pmcid': 'PMC99999'},
            {'title': 'The requested source', 'doi': '10.1038/s41586-024-08545-z', 'pmcid': 'PMC12345'}]}}).encode(), 'application/json'
    monkeypatch.setattr(reader, '_read', read)
    rows = reader.search('The requested source')['results']
    assert rows[0]['url'] == 'https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/'
    assert all('PMC99999' not in row['url'] for row in rows)


def test_scholarly_reader_keeps_math_text_and_omits_active_script(tmp_path, monkeypatch):
    reader=ScholarlySources(tmp_path)
    monkeypatch.setattr(reader,'_read',lambda url:(b'<p>Equation:</p><script type="math/tex">x^2+y^2</script><script>not_a_scientific_equation()</script>','text/html'))
    result=reader.fetch('https://arxiv.org/abs/1234.56789')
    assert 'x^2+y^2'in result['content'] and 'not_a_scientific_equation'not in result['content']


def test_untrusted_destinations_and_exhausted_budgets_make_no_request(tmp_path, monkeypatch):
    monkeypatch.setattr('urllib.request.build_opener',lambda *args:pytest.fail('Unexpected network'))
    reader=ScholarlySources(tmp_path,max_requests=0)
    with pytest.raises(ValueError):reader._read('https://doi.org/10.1000/example')
    reader=ScholarlySources(tmp_path)
    with pytest.raises(ValueError):reader._read('https://127.0.0.1/private')
