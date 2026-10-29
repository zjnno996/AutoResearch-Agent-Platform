"""Original equations must survive acquisition, caching and scientific grounding."""
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.orchestration.bibliography import deduplicate_references
from researchclaw.orchestration.grounding import discover_sources
from researchclaw.orchestration.jats_sources import fetch_jats, render_jats
from researchclaw.orchestration.primary_sources import fetch_pmc
from researchclaw.orchestration.public_source_cache import MAX_BYTES, PublicSourceCache


EXPRESSION = r'$$s_{i,i+1}=\frac{a_i+b_{i+1}}{c_i}-1$$'


def jats(pmcid='PMC98765', formula=None):
    if formula is None:
        formula = ('<disp-formula id="E3"><label>3</label><alternatives>'
            '<tex-math><![CDATA['+EXPRESSION+']]></tex-math>'
            '<mml:math><mml:mfrac><mml:mi>LOST_NUMERATOR</mml:mi><mml:mi>LOST_DENOMINATOR</mml:mi>'
            '</mml:mfrac></mml:math></alternatives></disp-formula>')
    return ('<article xmlns:mml="http://www.w3.org/1998/Math/MathML" xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<front><article-meta><article-id pub-id-type="pmcid">'+pmcid+'</article-id>'
        '<article-id pub-id-type="doi">10.1000/example</article-id><title-group>'
        '<article-title>Original NamedCorpus method</article-title></title-group>'
        '<abstract><p>A source-backed mathematical method.</p></abstract></article-meta></front>'
        '<body><sec><title>Scientific method</title><p>Before the equation.'+formula+'After the equation.</p>'
        '<p>Dimension: m<sup>2</sup>; variable x<sub>i</sub>.</p><p>'
        + 'Measurements, assumptions, units and derivation are specified in the original source. '*20
        + '</p><table-wrap><table><tr><td>quantity</td><td>unit</td></tr></table></table-wrap></sec></body>'
        '<back><notes><p>Author code: <ext-link xlink:href="https://github.com/authors/method">repository</ext-link>'
        '</p></notes><ref-list><ref>REFERENCE_ONLY_NOT_ARTICLE_BODY</ref></ref-list></back></article>').encode()


def test_original_tex_keeps_operators_indices_label_and_position_without_duplicate_mathml():
    raw = jats()
    result = render_jats(raw, 'PMC98765')
    text = result['content']
    assert EXPRESSION in text and 'id=E3; label=3; representation=latex' in text
    assert text.index('Before the equation.') < text.index(EXPRESSION) < text.index('After the equation.')
    assert 'LOST_NUMERATOR' not in text and 'REFERENCE_ONLY_NOT_ARTICLE_BODY' not in text
    assert 'm<sup>2</sup>' in text and 'x<sub>i</sub>' in text
    assert 'quantity | unit |' in text and 'https://github.com/authors/method' in text
    assert result['raw_jats'] == raw
    assert result['document_metadata']['original_response_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['document_metadata']['formula_count'] == 1
    assert result['document_metadata']['unextracted_formula_count'] == 0


def test_mathml_structure_is_retained_and_graphic_only_formula_remains_an_explicit_gap():
    formula = ('<disp-formula id="E1"><label>1</label><mml:math><mml:mfrac>'
               '<mml:mn>2</mml:mn><mml:mi>x</mml:mi></mml:mfrac></mml:math></disp-formula>'
               '<inline-formula id="E2"><graphic xlink:href="equation.gif"/></inline-formula>')
    result = render_jats(jats(formula=formula), 'PMC98765')
    assert 'representation=mathml' in result['content'] and 'mfrac' in result['content']
    assert 'Expression not extracted' in result['content'] and 'equation.gif' in result['content']
    assert result['document_metadata']['unextracted_formula_count'] == 1
    assert result['document_metadata']['formula_count'] == 2


@pytest.mark.parametrize('raw', [jats('PMC11111'), b'<article/>', b'<html>Not an article</html>',
    b'<!DOCTYPE article [<!ENTITY x "fabricated">]><article>&x;</article>',
    '<!DOCTYPE article [<!ENTITY x "fabricated">]><article>&x;</article>'.encode('utf-16'),
    b'<article', b'x'*(MAX_BYTES+1)])
def test_wrong_article_malformed_or_expanding_xml_never_becomes_a_scientific_source(raw):
    with pytest.raises(ValueError):
        render_jats(raw, 'PMC98765')


def test_original_xml_replays_offline_without_provider_credentials(tmp_path, monkeypatch):
    sent, raw = [], jats()
    def request(req, **kwargs):
        sent.append(req.full_url)
        assert req.get_header('Authorization') is None
        assert req.full_url == 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC98765/fullTextXML'
        return io.BytesIO(raw)
    monkeypatch.setattr('urllib.request.urlopen', request)
    first = fetch_pmc('PMC98765', shared_cache_dir=tmp_path/'cache')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('Offline recovery used the network'))
    second = fetch_pmc('PMC98765', shared_cache_dir=tmp_path/'cache')
    assert first['content_origin'] == second['content_origin'] == 'pmc_jats'
    assert first['raw_jats'] == second['raw_jats'] == raw and len(sent) == 1
    assert first['content'] == second['content']
    assert second['document_metadata']['public_response_cache']['hit'] is True


def test_wrong_article_redirect_is_rejected_without_caching(tmp_path, monkeypatch):
    response = io.BytesIO(jats())
    response.geturl = lambda: 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC11111/fullTextXML'
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: response)
    with pytest.raises(ValueError, match='redirected'):
        fetch_jats('PMC98765', shared_cache_dir=tmp_path/'cache')
    assert not (tmp_path/'cache').exists()


def test_formula_bearing_article_is_exported_as_original_xml_with_its_own_hash(tmp_path, monkeypatch):
    import json
    raw = jats()
    def request(req, **kwargs):
        assert req.get_header('Authorization') in (None, 'Bearer private-fixture')
        if req.full_url.endswith('/fullTextXML'):
            assert req.get_header('Authorization') is None
            return io.BytesIO(raw)
        return io.BytesIO(json.dumps({'results': [{
            'url': 'https://pmc.ncbi.nlm.nih.gov/articles/instance/98765/bin/review.pdf',
            'title': 'Editorial correspondence', 'content': 'REVIEW_LETTER_NOT_METHODS. '*100}]}).encode())
    monkeypatch.setattr('urllib.request.urlopen', request)
    config = SimpleNamespace(research=SimpleNamespace(topic='Reconcile **NamedCorpus** observations.'),
        research_engine=SimpleNamespace(public_source_search=True, max_source_queries=2, max_public_sources=2))
    llm = SimpleNamespace(base_url='https://api.deepseek.com/v1', api_key='private-fixture')
    refs = discover_sources(tmp_path, config, llm, {'source_queries': ['NamedCorpus methodology']})
    assert len(refs) == 1 and refs[0]['content_origin'] == 'pmc_jats'
    assert refs[0]['retrieval_status'] == 'source_content'
    text = Path(refs[0]['text_path']).read_text()
    assert EXPRESSION in text and 'REVIEW_LETTER_NOT_METHODS' not in text
    assert Path(refs[0]['raw_jats_path']).read_bytes() == raw
    assert refs[0]['raw_jats_sha256'] == hashlib.sha256(raw).hexdigest()
    assert refs[0]['public_code_urls'] == ['https://github.com/authors/method']
    assert 'private-fixture' not in (tmp_path/'public_source_retrieval.json').read_text()


def test_formula_bearing_main_article_is_preferred_over_same_article_bioc():
    base = {'source': 'https://pmc.ncbi.nlm.nih.gov/articles/PMC98765/',
            'document_metadata': {'pmcid': 'PMC98765'}, 'retrieval_status': 'source_content', 'context': 'Body'}
    rows = deduplicate_references([base | {'content_origin': 'pmc_bioc'}, base | {'content_origin': 'pmc_jats'}])
    assert len(rows) == 1 and rows[0]['content_origin'] == 'pmc_jats'


def test_article_xml_uses_article_freshness_instead_of_search_metadata_freshness(tmp_path, monkeypatch):
    import time
    cache = PublicSourceCache(tmp_path/'cache')
    url = 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC98765/fullTextXML'
    cache.put(url, jats(), 'application/xml')
    now = time.time()
    monkeypatch.setattr('researchclaw.orchestration.public_source_cache.time.time', lambda: now+2*86400)
    assert cache.get(url)[0] == jats()
    monkeypatch.setattr('researchclaw.orchestration.public_source_cache.time.time', lambda: now+31*86400)
    assert cache.get(url) is None
