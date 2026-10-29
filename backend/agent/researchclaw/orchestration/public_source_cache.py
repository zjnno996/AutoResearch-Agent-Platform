"""Share only hash-verified, credential-free public HTTP source responses."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid


MAX_BYTES = 4 * 1024 * 1024


def public_cache_url(url: str) -> bool:
    from researchclaw.orchestration.grounding import primary_url
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                or any(word in url.casefold() for word in ('target_study', 'researchclawbench', 'checklist.json', 'leaderboard'))):
            return False
        metadata = ((parsed.hostname == 'api.crossref.org' and parsed.path == '/works')
            or (parsed.hostname == 'www.ebi.ac.uk' and parsed.path == '/europepmc/webservices/rest/search'))
        bioc = (parsed.hostname == 'www.ncbi.nlm.nih.gov' and re.fullmatch(
            r'/research/bionlp/RESTful/pmcoa.cgi/BioC_json/PMC\d{1,12}/unicode', parsed.path))
        jats = (parsed.hostname == 'www.ebi.ac.uk' and re.fullmatch(
            r'/europepmc/webservices/rest/PMC\d{1,12}/fullTextXML', parsed.path) and not parsed.query)
        return bool(metadata or bioc or jats or primary_url(url))
    except (TypeError, ValueError):
        return False


class PublicSourceCache:
    """Immutable response blobs plus replaceable URL metadata, never research outputs."""
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def get(self, url: str) -> tuple[bytes, dict] | None:
        if not public_cache_url(url):
            return None
        index = self.root/'urls'/(hashlib.sha256(url.encode()).hexdigest()+'.json')
        try:
            if ((self.root/'urls').is_symlink() or (self.root/'blobs').is_symlink()
                    or index.is_symlink() or index.stat().st_size > 16384):
                return None
            record = json.loads(index.read_text(encoding='utf-8'))
            digest = record.get('sha256', '')
            if (record.get('url') != url or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                    or type(record.get('bytes')) is not int or not 0 < record['bytes'] <= MAX_BYTES
                    or not isinstance(record.get('content_type'), str)):
                return None
            parsed = urlsplit(url)
            metadata = (parsed.hostname == 'api.crossref.org'
                        or (parsed.hostname == 'www.ebi.ac.uk' and parsed.path.endswith('/search')))
            age = time.time()-float(record['retrieved_at'])
            if not math.isfinite(age) or age < -60 or age > (86400 if metadata else 30*86400):
                return None
            blob = self.root/'blobs'/(digest+'.bin')
            if blob.is_symlink() or blob.stat().st_size != record['bytes']:
                return None
            raw = blob.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                return None
            return raw, record
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def put(self, url: str, raw: bytes, content_type: str) -> dict:
        if not public_cache_url(url) or not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
            raise ValueError('Only bounded public source HTTP responses can enter this cache')
        digest = hashlib.sha256(raw).hexdigest()
        record = {'schema_version': 'public-source-response-v1', 'url': url, 'sha256': digest,
            'bytes': len(raw), 'content_type': content_type, 'retrieved_at': time.time(),
            'scope': 'Original public HTTP response only; no agent findings, benchmark feedback or research outputs.'}
        blobs, urls = self.root/'blobs', self.root/'urls'
        blobs.mkdir(parents=True, exist_ok=True)
        urls.mkdir(parents=True, exist_ok=True)
        if blobs.is_symlink() or urls.is_symlink():
            raise ValueError('Public source cache directories cannot be symlinks')
        temporary = blobs/(digest+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, blobs/(digest+'.bin'))
        finally:
            temporary.unlink(missing_ok=True)
        index = urls/(hashlib.sha256(url.encode()).hexdigest()+'.json')
        temporary = index.with_name(index.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            os.replace(temporary, index)
        finally:
            temporary.unlink(missing_ok=True)
        return record
