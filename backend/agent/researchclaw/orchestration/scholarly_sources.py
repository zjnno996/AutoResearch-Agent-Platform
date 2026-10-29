"""Credential-free scholarly lookup and bounded primary-paper reading.

This transport is separate from model inference. It accepts no model API key,
never installs code, and records the actual HTTP destinations it contacted.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class _ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.math_script = False
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag == 'script' and not self.hidden and dict(attrs).get('type', '').startswith('math/tex'):
            self.math_script = True
        elif tag in {'script', 'style', 'nav', 'footer'}:
            self.hidden += 1
        elif not self.hidden and tag in {'p', 'div', 'h1', 'h2', 'h3', 'br', 'li'}:
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag == 'script' and self.math_script:
            self.math_script = False
        elif tag in {'script', 'style', 'nav', 'footer'}:
            self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class ScholarlySources:
    def __init__(self, root: Path, *, deadline_seconds: float = 35, max_requests: int = 8,
                 shared_cache_dir: Path | None = None):
        self.root = root
        self.deadline = time.monotonic() + deadline_seconds
        self.max_requests = max_requests
        self.requests = []
        self.cache = root / 'compact_literature' / 'public_http'
        from researchclaw.orchestration.public_source_cache import PublicSourceCache
        self.shared_cache = PublicSourceCache(shared_cache_dir) if shared_cache_dir is not None else None

    def _read(self, url: str) -> tuple[bytes, str]:
        from researchclaw.orchestration.grounding import primary_url
        parent = self
        def allowed(value):
            host = urllib.parse.urlsplit(value).hostname
            parsed = urllib.parse.urlsplit(value)
            metadata = (host == 'api.crossref.org' or (host == 'www.ebi.ac.uk'
                and parsed.path == '/europepmc/webservices/rest/search'))
            return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                    and parsed.port in (None, 443) and (metadata or bool(primary_url(value))))
        class Redirect(urllib.request.HTTPRedirectHandler):
            max_redirections = 3
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if not allowed(newurl) or len(parent.requests) >= parent.max_requests:
                    raise ValueError('Unsupported primary-source redirect')
                parent.requests.append({'url': newurl, 'redirect': True, 'status': 'requested'})
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or len(self.requests) >= self.max_requests or not allowed(url):
            raise ValueError('Public scholarly acquisition budget or domain limit reached')
        row = {'url': url, 'status': 'requested'}
        self.requests.append(row)
        cached = self.shared_cache.get(url) if self.shared_cache else None
        if cached:
            raw, record = cached
            row.update(status='ok', cache_hit=True, sha256=record['sha256'], bytes=len(raw),
                       original_retrieved_at=record['retrieved_at'])
            self.cache.mkdir(parents=True, exist_ok=True)
            target = self.cache/(record['sha256']+'.bin')
            target.write_bytes(raw)
            row['cache'] = str(target)
            return raw, record['content_type']
        request = urllib.request.Request(url, headers={'User-Agent': 'EMNETS-RA/1.0 scholarly source reader'})
        # Model credentials are intentionally unavailable to this component.
        opener = urllib.request.build_opener(Redirect())
        try:
            with opener.open(request, timeout=min(10, remaining)) as response:
                chunks, total = [], 0
                read = getattr(response, 'read1', response.read)
                while total <= 4 * 1024 * 1024:
                    if time.monotonic() >= self.deadline:
                        raise ValueError('Public-source acquisition deadline reached')
                    part = read(min(65536, 4 * 1024 * 1024 + 1 - total))
                    if not part:
                        break
                    total += len(part)
                    chunks.append(part)
                raw = b''.join(chunks)
                content_type = response.headers.get('Content-Type', '')
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('Primary-source response exceeds byte budget')
            self.cache.mkdir(parents=True, exist_ok=True)
            target = self.cache / (hashlib.sha256(raw).hexdigest() + '.bin')
            target.write_bytes(raw)
            row.update(status='ok', sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), cache=str(target))
            if self.shared_cache:
                try:
                    self.shared_cache.put(url, raw, content_type)
                except (OSError, ValueError):
                    row['shared_cache_write'] = 'unavailable'
            return raw, content_type
        except urllib.error.HTTPError as exc:
            row.update(status='unavailable', http_status=exc.code)
            raise
        except (OSError, ValueError) as exc:
            row.update(status='unavailable', error_type=type(exc).__name__)
            raise

    def search(self, query: str) -> dict:
        from researchclaw.orchestration.grounding import primary_url
        parameters = urllib.parse.urlencode({'query.bibliographic': query, 'rows': 8})
        try:
            raw, _ = self._read('https://api.crossref.org/works?' + parameters)
            payload = json.loads(raw)
        except (OSError, ValueError):
            # A single metadata index outage must not establish that the
            # requested primary paper is unavailable.
            payload = {}
        results = []
        for index, row in enumerate(payload.get('message', {}).get('items', [])[:8]):
            title = ' '.join(row.get('title', []))
            urls = [row.get('resource', {}).get('primary', {}).get('URL', ''), row.get('URL', '')]
            urls += [link.get('URL', '') for link in row.get('link', []) if isinstance(link, dict)]
            source = next((primary_url(url) for url in urls if primary_url(url)), '')
            if not source:
                continue
            abstract = _ArticleText()
            abstract.feed(str(row.get('abstract', '')))
            context = (title + '\nSearch-index abstract only; this is not the article methods.\n'
                       + ''.join(abstract.parts))[:1100]
            results.append({'url': source, 'title': title, 'content': context, 'rerank_score': 8-index,
                            'document_metadata': {'doi': row.get('DOI', ''), 'document_type': 'index_metadata'}})
        # Prefer the full original PMC article when it is independently
        # indexed for the same DOI. Otherwise use a bounded, credential-free
        # alternative search. Abstracts remain explicitly index-only context.
        best_doi = results[0]['document_metadata']['doi'] if results else ''
        alternate_query = 'DOI:"' + best_doi + '"' if best_doi else query
        parameters = urllib.parse.urlencode({'query': alternate_query, 'format': 'json', 'pageSize': 8})
        try:
            raw, _ = self._read('https://www.ebi.ac.uk/europepmc/webservices/rest/search?' + parameters)
            rows = json.loads(raw).get('resultList', {}).get('result', [])
            public = []
            for index, row in enumerate(rows[:8]):
                if not isinstance(row, dict):
                    continue
                doi = str(row.get('doi', ''))
                if best_doi and doi.casefold() != best_doi.casefold():
                    continue
                pmcid = str(row.get('pmcid', ''))
                if re.fullmatch(r'PMC\d{1,12}', pmcid):
                    source = 'https://pmc.ncbi.nlm.nih.gov/articles/' + pmcid + '/'
                else:
                    source = primary_url('https://doi.org/' + doi) if doi else ''
                if not source:
                    continue
                title = str(row.get('title', ''))
                public.append({'url': source, 'title': title,
                    'content': title+'\nSearch-index metadata only; obtain the original article before inferring its methods.',
                    'rerank_score': 10-index, 'document_metadata': {
                        'doi': doi, 'pmcid': pmcid, 'document_type': 'index_metadata', 'index': 'Europe PMC'}})
            if public:
                urls = {row['url'] for row in public}
                results = [*public, *(row for row in results if row['url'] not in urls)]
        except (OSError, ValueError):
            pass  # Preserve any independently indexed Crossref result.
        return {'results': results}

    def fetch(self, url: str) -> dict:
        parsed = urllib.parse.urlsplit(url)
        fetch_url = url
        if parsed.hostname in {'arxiv.org', 'www.arxiv.org'} and parsed.path.startswith('/abs/'):
            fetch_url = 'https://arxiv.org/pdf/' + parsed.path.removeprefix('/abs/')
        raw, content_type = self._read(fetch_url)
        if 'pdf' in content_type or raw.startswith(b'%PDF'):
            from researchclaw.orchestration.literature import extract_pdf
            path = self.cache / (hashlib.sha256(raw).hexdigest() + '.pdf')
            path.write_bytes(raw)
            text, metadata = extract_pdf(path, self.cache, max_ocr_pages=0)
            return {'content': text[:256000], 'source_scope': 'Retrieved primary PDF; reader coverage is recorded, not assumed.',
                    'document_metadata': {'reader': metadata, 'text_truncated': len(text) > 256000,
                                          'raw_source_sha256': hashlib.sha256(raw).hexdigest()}}
        reader = _ArticleText()
        reader.feed(raw.decode('utf-8', errors='replace'))
        text = ''.join(reader.parts)
        return {'content': text[:256000], 'source_scope': 'Retrieved public article HTML text; scripts, figures and inaccessible content are omitted. Full-paper coverage is not certified.',
                'document_metadata': {'text_truncated': len(text) > 256000,
                                      'raw_source_sha256': hashlib.sha256(raw).hexdigest()}}
