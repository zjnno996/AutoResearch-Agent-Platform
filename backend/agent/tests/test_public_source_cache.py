"""Public-source reuse must survive outages without accepting altered evidence."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.orchestration.public_source_cache import PublicSourceCache
from researchclaw.orchestration.scholarly_sources import ScholarlySources


ARTICLE = 'https://www.nature.com/articles/s41586-024-08545-z'


def test_another_run_reuses_exact_public_response_when_the_network_is_down(tmp_path, monkeypatch):
    sent = []
    raw = b'<h1>Original scientific methods</h1><p>Source equation and assumptions.</p>'
    def request(req, **kwargs):
        sent.append(req)
        assert req.get_header('Authorization') is None
        response = io.BytesIO(raw)
        response.headers = {'Content-Type': 'text/html'}
        return response
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: SimpleNamespace(open=request))
    first = ScholarlySources(tmp_path/'run-one', shared_cache_dir=tmp_path/'public-cache')
    assert first._read(ARTICLE) == (raw, 'text/html')
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: pytest.fail('Public network is unavailable'))
    second = ScholarlySources(tmp_path/'run-two', shared_cache_dir=tmp_path/'public-cache')
    assert second._read(ARTICLE) == (raw, 'text/html')
    assert len(sent) == 1 and second.requests[0]['cache_hit'] is True
    assert Path(second.requests[0]['cache']).read_bytes() == raw


def test_tampered_cache_and_mismatched_urls_cannot_become_scientific_sources(tmp_path):
    cache = PublicSourceCache(tmp_path/'cache')
    record = cache.put(ARTICLE, b'Original method', 'text/plain')
    assert cache.get(ARTICLE)[0] == b'Original method'
    (cache.root/'blobs'/(record['sha256']+'.bin')).write_bytes(b'Altered  method')
    assert cache.get(ARTICLE) is None
    cache.put(ARTICLE, b'Original method', 'text/plain')
    index = next((cache.root/'urls').glob('*.json'))
    changed = json.loads(index.read_text());changed['url'] = 'https://doi.org/10.1000/another'
    index.write_text(json.dumps(changed))
    assert cache.get(ARTICLE) is None


@pytest.mark.parametrize('url', ['http://www.nature.com/article', 'file:///root/private',
    'https://127.0.0.1/private', 'https://key@www.nature.com/article',
    'https://www.nature.com/target_study/checklist.json', 'https://www.ebi.ac.uk/private'])
def test_private_or_evaluation_destinations_never_enter_the_shared_cache(tmp_path, url):
    cache = PublicSourceCache(tmp_path/'cache')
    assert cache.get(url) is None
    with pytest.raises(ValueError):
        cache.put(url, b'not a public scientific response', 'text/plain')
    assert not cache.root.exists()


def test_metadata_expires_before_paper_responses_and_future_timestamps_are_rejected(tmp_path, monkeypatch):
    import time
    cache = PublicSourceCache(tmp_path/'cache')
    index_url = 'https://api.crossref.org/works?query.bibliographic=original+method&rows=8'
    cache.put(index_url, b'{"message":{"items":[]}}', 'application/json')
    cache.put(ARTICLE, b'Original paper', 'text/plain')
    now = time.time()
    monkeypatch.setattr('researchclaw.orchestration.public_source_cache.time.time', lambda: now+86401)
    assert cache.get(index_url) is None and cache.get(ARTICLE)[0] == b'Original paper'
    monkeypatch.setattr('researchclaw.orchestration.public_source_cache.time.time', lambda: now-120)
    assert cache.get(ARTICLE) is None


def test_shared_cache_does_not_follow_symlinked_blob_directories(tmp_path):
    cache = PublicSourceCache(tmp_path/'cache')
    cache.put(ARTICLE, b'Original paper', 'text/plain')
    blobs = cache.root/'blobs'
    moved = tmp_path/'moved';blobs.rename(moved);blobs.symlink_to(moved, target_is_directory=True)
    assert cache.get(ARTICLE) is None


def test_original_bioc_response_is_reused_without_model_credentials_or_a_network_call(tmp_path, monkeypatch):
    from researchclaw.orchestration.primary_sources import fetch_pmc
    payload = [{'documents': [{'id': 'PMC98765', 'passages': [
        {'offset': 0, 'infons': {'section_type': 'TITLE'}, 'text': 'Original paper'},
        {'offset': 100, 'infons': {'section_type': 'METHODS'}, 'text': 'Original scientific method and assumptions. '*30}]}]}]
    sent = []
    def request(req, **kwargs):
        sent.append(req)
        assert req.get_header('Authorization') is None
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr('urllib.request.urlopen', request)
    first = fetch_pmc('PMC98765', shared_cache_dir=tmp_path/'cache', prefer_jats=False)
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('Offline scientific source recovery made a network call'))
    second = fetch_pmc('PMC98765', shared_cache_dir=tmp_path/'cache', prefer_jats=False)
    assert first['content'] == second['content'] and first['raw_bioc'] == second['raw_bioc'] == payload
    assert second['document_metadata']['public_response_cache']['hit'] is True and len(sent) == 1
