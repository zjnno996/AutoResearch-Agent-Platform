"""Recover a canonical PMC article with original formulas and explicit fallbacks.

https://pmc.ncbi.nlm.nih.gov/tools/developers/
https://europepmc.org/RestfulWebService
https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any
from pathlib import Path


def public_code_urls(text: str) -> list[str]:
    """Recognize literal/Markdown-escaped GitHub links without rewriting science."""
    # Some fetch providers escape all Markdown punctuation, including URL
    # separators. Decode only a temporary lookup view; preserve cached text.
    lookup = re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]^_\x60{|}~])', r'\1', text)
    return sorted(set(url.rstrip('.') for url in re.findall(
        r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', lookup)))[:8]


def pmc_identifier(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'pmc.ncbi.nlm.nih.gov':
        return ''
    match = re.match(r'^/articles/(?:instance/)?(?:PMC)?(\d{1,12})(?:/|$)', parsed.path)
    return 'PMC' + match[1] if match else ''


def render_bioc(data: Any, pmcid: str) -> dict[str, Any]:
    collections = data if isinstance(data, list) else [data]
    documents = [document for collection in collections if isinstance(collection, dict)
                 for document in collection.get('documents', []) if isinstance(document, dict)]
    matches = [document for document in documents if str(document.get('id', '')).removeprefix('PMC') == pmcid[3:]]
    if len(matches) != 1:
        raise ValueError('BioC response did not identify exactly the requested public article')
    document = matches[0]
    parts, title, doi = [], '', ''
    included, omitted = Counter(), Counter()
    for passage in document.get('passages', []):
        text = passage.get('text') if isinstance(passage, dict) else None
        if not isinstance(text, str) or not text.strip():
            continue
        info = passage.get('infons', {})
        section = str(info.get('section_type', 'UNSPECIFIED'))
        kind = str(info.get('type', 'paragraph'))
        if section == 'TITLE' and not title:
            title, doi = text, str(info.get('article-id_doi', ''))
        if section.upper() in {'REF', 'REFERENCES', 'AUTH_CONT', 'COMP_INT', 'ACK_FUND'}:
            omitted[section] += 1
            continue
        included[section] += 1
        parts.append(f'\n[Original PMC passage offset {passage.get("offset", "unknown")}; section={section}; type={kind}]\n{text}')
    text = '\n'.join(parts)
    if len(text.strip()) < 500:
        raise ValueError('PMC article has no usable scientific body text')
    return {'content': text, 'title': title or pmcid, 'raw_bioc': data, 'content_origin': 'pmc_bioc',
            'document_metadata': {'pmcid': pmcid, 'doi': doi, 'license': document.get('infons', {}).get('license'),
                                  'included_passages': dict(included), 'omitted_passages': dict(omitted)},
            'source_scope': 'Original BioC article-body passages with source offsets; references and selected author metadata omitted. '
                            'BioC may omit display/inline formulas; formula completeness is not certified. '
                            'Full BioC response is retained separately.'}


def fetch_pmc(pmcid: str, *, shared_cache_dir: Path | None = None, prefer_jats: bool = True) -> dict[str, Any]:
    """Prefer original formula-bearing XML, retaining BioC as an explicit fallback."""
    if prefer_jats:
        from researchclaw.orchestration.jats_sources import fetch_jats
        try:
            return fetch_jats(pmcid, shared_cache_dir=shared_cache_dir)
        except (OSError, ValueError) as exc:
            result = _fetch_bioc(pmcid, shared_cache_dir=shared_cache_dir)
            result['document_metadata']['jats_recovery'] = {'status': 'unavailable',
                'error_type': type(exc).__name__, 'fallback': 'pmc_bioc', 'formulas_complete': False}
            return result
    return _fetch_bioc(pmcid, shared_cache_dir=shared_cache_dir)


def _fetch_bioc(pmcid: str, *, shared_cache_dir: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r'PMC\d{1,12}', pmcid):
        raise ValueError('Expected a public PMC identifier')
    url = 'https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json/' + pmcid + '/unicode'
    from researchclaw.orchestration.public_source_cache import PublicSourceCache
    cache = PublicSourceCache(shared_cache_dir) if shared_cache_dir is not None else None
    cached = cache.get(url) if cache else None
    if cached:
        raw, record = cached
        result = render_bioc(json.loads(raw.decode('utf-8-sig')), pmcid)
        result['document_metadata']['public_response_cache'] = {'hit': True,
            'raw_source_sha256': record['sha256'], 'original_retrieved_at': record['retrieved_at'], 'url': url}
        return result
    request = urllib.request.Request(url, headers={'User-Agent': 'EMNETS-RA-scientific-source/1.0'})
    with urllib.request.urlopen(request, timeout=25) as response:
        final = urllib.parse.urlsplit(response.geturl() if hasattr(response, 'geturl') else url)
        if final.scheme != 'https' or final.hostname not in {'www.ncbi.nlm.nih.gov', 'pmc.ncbi.nlm.nih.gov'}:
            raise ValueError('PMC full-text response redirected outside the official public API')
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('PMC full-text response exceeded the bounded 4-MiB read')
    result = render_bioc(json.loads(raw.decode('utf-8-sig')), pmcid)
    if cache:
        try:
            record = cache.put(url, raw, 'application/json')
            result['document_metadata']['public_response_cache'] = {'hit': False,
                'raw_source_sha256': record['sha256'], 'original_retrieved_at': record['retrieved_at'], 'url': url}
        except (OSError, ValueError):
            result['document_metadata']['public_response_cache'] = {'hit': False, 'write_status': 'unavailable'}
    return result
