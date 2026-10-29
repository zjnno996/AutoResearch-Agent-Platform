"""Preserve original PMC formula representations from the public JATS article."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
from typing import Any
import urllib.request
from xml.etree import ElementTree as ET

from researchclaw.orchestration.public_source_cache import MAX_BYTES, PublicSourceCache


def render_jats(raw: bytes, pmcid: str) -> dict[str, Any]:
    """Render source text and verbatim TeX/MathML; never reconstruct equations."""
    if not re.fullmatch(r'PMC\d{1,12}', pmcid):
        raise ValueError('Expected a public PMC identifier')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('JATS response must be bounded original bytes')
    # ElementTree does not fetch external DTDs. Refuse internal entity
    # declarations as well, including non-UTF-8 encodings of those declarations.
    declaration_view = raw.replace(b'\x00', b'')
    if re.search(br'<!\s*ENTITY\b', declaration_view, re.I):
        raise ValueError('JATS entity declarations are unsupported')
    try:
        article = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('Invalid JATS article XML') from exc
    if article.tag != 'article':
        raise ValueError('JATS response is not a single original article')
    ids = article.findall('./front/article-meta/article-id')
    identities = {('PMC'+str(n.text).strip().removeprefix('PMC')) for n in ids
                  if n.get('pub-id-type') in {'pmcid', 'pmc'} and n.text}
    if identities != {pmcid}:
        raise ValueError('JATS response did not identify exactly the requested public article')
    formulas: list[dict[str, Any]] = []

    def name(node: ET.Element) -> str:
        return node.tag.rsplit('}', 1)[-1]

    def xml(node: ET.Element) -> str:
        isolated = copy.copy(node)
        isolated.tail = None
        return ET.tostring(isolated, encoding='unicode')

    def render(node: ET.Element) -> str:
        tag = name(node)
        if tag in {'disp-formula', 'inline-formula', 'math', 'tex-math'}:
            tex = next((n for n in node.iter() if name(n) == 'tex-math'), None)
            math = next((n for n in node.iter() if name(n) == 'math'), None)
            label = next((''.join(n.itertext()).strip() for n in node if name(n) == 'label'), '')
            if tex is not None and ''.join(tex.itertext()).strip():
                representation, expression = 'latex', ''.join(tex.itertext())
            elif math is not None:
                representation, expression = 'mathml', xml(math)
            else:
                representation, expression = 'unavailable', 'Expression not extracted; original formula markup retained below.\n'+xml(node)
            formulas.append({'id': node.get('id', ''), 'label': label, 'kind': tag,
                'representation': representation, 'expression_sha256': hashlib.sha256(expression.encode()).hexdigest()})
            return (f'\n[Original JATS formula id={node.get("id", "")}; label={label}; representation={representation}]\n'
                    f'```{representation}\n{expression}\n```\n')
        content = node.text or ''
        for child in node:
            content += render(child) + (child.tail or '')
        if tag == 'ext-link':
            href = node.get('{http://www.w3.org/1999/xlink}href', '')
            if href.startswith(('https://', 'http://')) and href not in content:
                content += ' ('+href+')'
        if tag in {'sup', 'sub'}:
            return '<'+tag+'>'+content+'</'+tag+'>'
        if tag in {'td', 'th'}:
            return content+' | '
        if tag in {'article-title', 'title', 'sec', 'p', 'abstract', 'fig', 'caption', 'table-wrap',
                   'table', 'tr', 'thead', 'tbody', 'list', 'list-item', 'disp-quote', 'notes', 'fn'}:
            return '\n'+content+'\n'
        return content

    meta = article.find('./front/article-meta')
    body = article.find('body')
    if body is None:
        raise ValueError('JATS article has no scientific body')
    title_node = meta.find('./title-group/article-title')
    title = ''.join(title_node.itertext()).strip() if title_node is not None else pmcid
    parts = [render(title_node)] if title_node is not None else []
    parts.extend(render(n) for n in meta.findall('abstract'))
    parts.append(render(body))
    # Availability sections and appendices can contain the actual method/code.
    # Reference-list text and unrelated author/funding metadata stay omitted.
    back = article.find('back')
    if back is not None:
        parts.extend(render(n) for n in back if name(n) in {'sec', 'app-group', 'notes'})
    content = '\n'.join(parts)
    if len(content.strip()) < 500:
        raise ValueError('JATS article has no usable scientific body text')
    doi = next((str(n.text).strip() for n in ids if n.get('pub-id-type') == 'doi' and n.text), '')
    return {'content': content, 'title': title, 'raw_jats': raw, 'content_origin': 'pmc_jats',
        'document_metadata': {'pmcid': pmcid, 'doi': doi, 'document_type': 'main article',
            'original_response_sha256': hashlib.sha256(raw).hexdigest(),
            'formulas': formulas, 'formula_count': len(formulas),
            'unextracted_formula_count': sum(n['representation'] == 'unavailable' for n in formulas)},
        'source_scope': 'Original JATS article-body, abstract, captions/tables and selected availability/appendix sections; '
            'reference list and selected author metadata omitted. Original LaTeX or MathML is retained without mathematical '
            'reconstruction, with formula labels and IDs. Graphic-only formulas are marked unextracted. '
            'Original XML is retained separately; this extraction does not certify scientific equivalence or completeness.'}


def fetch_jats(pmcid: str, *, shared_cache_dir: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r'PMC\d{1,12}', pmcid):
        raise ValueError('Expected a public PMC identifier')
    url = 'https://www.ebi.ac.uk/europepmc/webservices/rest/'+pmcid+'/fullTextXML'
    cache = PublicSourceCache(shared_cache_dir) if shared_cache_dir is not None else None
    cached = cache.get(url) if cache else None
    if cached:
        raw, record = cached
        result = render_jats(raw, pmcid)
        result['document_metadata']['public_response_cache'] = {'hit': True, 'url': url,
            'raw_source_sha256': record['sha256'], 'original_retrieved_at': record['retrieved_at']}
        return result
    request = urllib.request.Request(url, headers={'User-Agent': 'EMNETS-RA-scientific-source/1.0'})
    with urllib.request.urlopen(request, timeout=25) as response:
        final = response.geturl() if hasattr(response, 'geturl') else url
        if final != url:
            raise ValueError('JATS response redirected outside the requested original article API')
        raw = response.read(MAX_BYTES+1)
    result = render_jats(raw, pmcid)
    if cache:
        try:
            record = cache.put(url, raw, 'application/xml')
            result['document_metadata']['public_response_cache'] = {'hit': False, 'url': url,
                'raw_source_sha256': record['sha256'], 'original_retrieved_at': record['retrieved_at']}
        except (OSError, ValueError):
            result['document_metadata']['public_response_cache'] = {'hit': False, 'write_status': 'unavailable'}
    return result
