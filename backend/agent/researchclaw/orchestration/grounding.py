"""Bounded public-source acquisition without an additional model conversation.

DeepSeek raw Search/Fetch API: https://api-docs.deepseek.com/guides/search/
Credentials are sent only to the already configured official provider host.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import write_json
from researchclaw.orchestration.bibliography import (
    article_identity, article_scope, canonical_article_url, deduplicate_references,
)
from researchclaw.orchestration.compact_generation import scientific_excerpt
from researchclaw.orchestration.literature import named_frameworks, source_queries

_PRIMARY_HOSTS = {"arxiv.org", "openreview.net", "proceedings.mlr.press", "jmlr.org", "nature.com",
    "science.org", "pnas.org", "cell.com", "journals.aps.org", "pubs.acs.org", "academic.oup.com",
    "link.springer.com", "sciencedirect.com", "epubs.siam.org", "biorxiv.org", "medrxiv.org",
    "pubmed.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "iopscience.iop.org", "ieeexplore.ieee.org",
    "proceedings.neurips.cc", "aclanthology.org", "wiley.com", "doi.org"}


def primary_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return ""
        if not any(host == name or host.endswith("." + name) for name in _PRIMARY_HOSTS):
            return ""
        if any(word in value.casefold() for word in ("researchclawbench", "target_study", "checklist.json", "leaderboard")):
            return ""
        value = canonical_article_url(value)
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").casefold()
        from researchclaw.orchestration.primary_sources import pmc_identifier
        pmcid = pmc_identifier(value)
        if pmcid:
            return 'https://pmc.ncbi.nlm.nih.gov/articles/' + pmcid + '/'
        return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))
    except (TypeError, ValueError):
        return ""


def discover_sources(root: Path, config: Any, llm_config: Any, contract: dict[str, Any]) -> list[dict[str, Any]]:
    queries = contract.get("source_queries", [])
    queries = [q for q in queries if isinstance(q, str) and q.strip()] if isinstance(queries, list) else []
    queries = source_queries(config.research.topic, queries, config.research_engine.max_source_queries)
    audit: dict[str, Any] = {"queries": [], "requests": [], "sources": [], "llm_calls": 0}
    audit_path = root / "public_source_retrieval.json"
    if not config.research_engine.public_source_search or not queries:
        audit["status"] = "not_needed_or_disabled"
        write_json(audit_path, audit)
        return []
    host = urllib.parse.urlsplit(getattr(llm_config, "base_url", "")).hostname
    provider_search = host == 'api.deepseek.com' and bool(getattr(llm_config, 'api_key', ''))
    scholarly = None
    if not provider_search and str(getattr(llm_config, 'primary_model', '')).startswith('Qwen'):
        from researchclaw.orchestration.scholarly_sources import ScholarlySources
        shared_cache = getattr(config.research_engine, 'public_source_cache_dir', '')
        scholarly = ScholarlySources(root, shared_cache_dir=Path(shared_cache) if shared_cache else None)
    if not provider_search and scholarly is None:
        audit["status"] = "configured_provider_has_no_supported_raw_search"
        write_json(audit_path, audit)
        return []
    audit['transport'] = 'configured_provider_raw_search' if provider_search else 'public_scholarly_http'
    if scholarly is not None:
        audit['public_http_requests'] = scholarly.requests

    def request(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        if len(audit["requests"]) >= config.research_engine.max_source_queries + config.research_engine.max_public_sources:
            return {}
        if endpoint in {'fetch', 'pmc_fulltext'} and sum(row['endpoint'] in {'fetch', 'pmc_fulltext'} for row in audit['requests']) >= config.research_engine.max_public_sources:
            return {}
        started = time.monotonic()
        row = {"endpoint": endpoint, "status": "unavailable"}
        audit["requests"].append(row)
        try:
            if endpoint == 'pmc_fulltext':
                from researchclaw.orchestration.primary_sources import fetch_pmc
                shared_cache = getattr(config.research_engine, 'public_source_cache_dir', '')
                value = (fetch_pmc(body['pmcid'], shared_cache_dir=Path(shared_cache)) if shared_cache
                         else fetch_pmc(body['pmcid']))
                row['status'] = 'ok'
                return value
            if scholarly is not None:
                value = scholarly.search(body['query']) if endpoint == 'search' else scholarly.fetch(body['url'])
                row['status'] = 'ok'
                return value
            req = urllib.request.Request("https://api.deepseek.com/v1/tools/" + endpoint,
                data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + llm_config.api_key,
                                                        "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as response:
                value = json.loads(response.read(4 * 1024 * 1024).decode())
            row["status"] = "ok"
            return value if isinstance(value, dict) else {}
        except urllib.error.HTTPError as exc:
            row["http_status"] = exc.code
            from researchclaw.llm.errors import capture_provider_error
            capture_provider_error(exc, (llm_config.api_key,))
            row["provider_error"] = exc.provider_error
        except (OSError, ValueError) as exc:
            row["error_type"] = type(exc).__name__
        finally:
            row["duration_sec"] = round(time.monotonic() - started, 3)
        return {}

    references, seen, pools = [], set(), []
    for query in queries:
        # Only scientific search terms leave the workspace. Discard paths,
        # credential-shaped text and benchmark/evaluation queries.
        if re.search(r"(?i)(researchclawbench|target_study|checklist\.json|leaderboard|(?<![a-z0-9])(?:sk|ask)-[a-z0-9_-]{8,}|/root/|/home/)", query):
            audit["queries"].append({"status": "rejected_non_scientific_query"})
            continue
        query = query.strip()[:104] + (" -ResearchClawBench" if provider_search else '')
        audit["queries"].append({"query": query})
        payload = request("search", {"query": query, "count": 10, "need_content": True})
        results = payload.get("results", [])
        if not isinstance(results, list):
            continue
        def rank(row: dict[str, Any]) -> float:
            try:
                return float(row.get("rerank_score") or 0)
            except (TypeError, ValueError):
                return 0
        results = sorted((row for row in results if isinstance(row, dict)), key=rank, reverse=True)
        pool = []
        for item in results:
            url = primary_url(str(item.get("url", "")))
            if not url or "researchclawbench" in str(item.get("title", "")).casefold():
                continue
            raw_url = str(item.get("url", ""))
            pool.append({"source": url, "title": str(item.get("title", "")), "_item": item,
                         "document_metadata": item.get("document_metadata", {}),
                         "source_scope": article_scope(raw_url, item),
                         "search_hit_url": raw_url,
                         "search_hits": [{"url": raw_url, "canonical_url": url, "query": query,
                                          "title": str(item.get("title", ""))}]})
        pools.append(pool)
    # Resolve aliases across all result pools before choosing/fetching. A later
    # main-article hit can replace an earlier supplement for the same parent DOI.
    representatives = {row["article_identity"]: row for row in deduplicate_references(
        [row for pool in pools for row in pool])}
    # Complementary queries each get a chance before filling remaining slots.
    selected = []
    while any(pools) and len(selected) < config.research_engine.max_public_sources:
        for pool in pools:
            while pool:
                candidate = pool.pop(0)
                identity = article_identity(candidate["source"], candidate)
                if identity in seen:
                    continue
                seen.add(identity)
                selected.append(representatives[identity])
                break
            if len(selected) >= config.research_engine.max_public_sources:
                break
    normalize = lambda text: re.sub(r'[^a-z0-9]+', ' ', text.casefold()).strip()
    concepts = named_frameworks(config.research.topic)
    task_names = [name for name in re.findall(r'\*\*([^*\n]+)\*\*', config.research.topic)
                  if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{2,48}', name)
                  and name.casefold() not in {'data', 'dataset', 'input', 'output', 'report', 'figures'}]
    def task_matches(title: str, text: str) -> list[str]:
        return [name for name in task_names if re.search(r'(?<![a-z0-9])' + re.escape(name.casefold()) + r'(?![a-z0-9])',
                                                        (title + '\n' + text).casefold())]
    for candidate in selected:
        url, item = candidate["source"], candidate["_item"]
        text = item.get('content') or ''
        text = text if isinstance(text, str) else ''
        title = str(item.get('title', ''))
        matches = [name for name, _ in concepts if normalize(name) in normalize(title + '\n' + text)]
        origin = 'search'
        from researchclaw.orchestration.primary_sources import pmc_identifier, public_code_urls
        pmcid = pmc_identifier(url)
        parent_recovery = bool(pmcid and re.search(r'/bin/|/supplement|/instance/', str(item.get('url', ''))))
        fetched: dict[str, Any] = {}
        # Fragment-index excerpts can omit the defining method. Recover the
        # canonical page once, within the same existing query/source budget.
        if len(text) < 1500 or '#' in str(item.get('url', '')) or matches or task_matches(title, text) or pmcid:
            fetched = (request('pmc_fulltext', {'pmcid': pmcid}) if pmcid else
                       request('fetch', {'url': url.replace('arxiv.org/abs/', 'arxiv.org/html/')}))
            content = fetched.get('content')
            if isinstance(content, str) and content.strip():
                if pmcid:
                    # A review-letter supplement is a different document. It
                    # must not be merged into its parent article's method text.
                    text, title, origin = content, str(fetched.get('title') or title), fetched.get('content_origin', 'pmc_bioc')
                elif article_scope(url, fetched) in {'supplement', 'review'}:
                    # A fetched attachment is its actual document; combining a
                    # main-page snippet would silently change that scope.
                    text, title, origin = content, str(fetched.get('title') or title), 'fetch'
                elif text.strip() and normalize(text) not in normalize(content) and normalize(content) not in normalize(text):
                    text = content + '\n\n[Additional search-index excerpt from the same source]\n' + text
                    origin = 'fetch_and_search'
                elif len(content) > len(text):
                    text, origin = content, 'fetch'
            if not text:
                text = item.get('summary') or item.get('snippet') or ''
                origin = 'summary_or_snippet'
        if parent_recovery and origin not in {'pmc_bioc', 'pmc_jats'}:
            url = str(item.get('url'))
            origin = 'supplement_only_main_article_unavailable'
        if not isinstance(text, str) or not text.strip():
            continue
        matches = [name for name, _ in concepts if normalize(name) in normalize(title + '\n' + text)]
        path = root / 'compact_literature' / f'public-{len(references)+1:02d}.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
        bioc_path = None
        if 'raw_bioc' in fetched:
            bioc_path = path.with_suffix('.bioc.json')
            write_json(bioc_path, fetched['raw_bioc'])
        jats_path = None
        if 'raw_jats' in fetched:
            jats_path = path.with_suffix('.jats.xml')
            jats_path.write_bytes(fetched['raw_jats'])
        identities = task_matches(title, text)
        row = {'source': url, 'title': title, 'text_path': str(path),
               'sha256': hashlib.sha256(text.encode()).hexdigest(), 'origin': 'public_primary_source',
               'content_origin': origin, 'named_framework_matches': matches,
               'task_identity_matches': identities, 'search_hit_url': str(item.get('url', '')),
               'source_scope': fetched.get('source_scope') or ('Supplementary/search document only; main article unavailable.' if parent_recovery or candidate['source_scope'] in {'supplement', 'review'} else 'Retrieved page/excerpts; full-paper coverage is not certified.'),
               'document_metadata': fetched.get('document_metadata') or candidate.get('document_metadata', {}),
               'canonical_source': canonical_article_url(url), 'alias_sources': candidate['alias_sources'],
               'search_hits': candidate['search_hits'], 'search_hit_urls': candidate['search_hit_urls'],
               'duplicate_sources': candidate['duplicate_sources'],
               'public_code_urls': public_code_urls(text),
               'retrieval_status': 'supplement_only' if parent_recovery and origin not in {'pmc_bioc', 'pmc_jats'} else 'source_content' if len(text) >= 1500 else 'snippet_only',
               'context': scientific_excerpt(title + '\n' + text, config.research.topic, 8000)}
        if bioc_path:
            row['raw_bioc_path'] = str(bioc_path)
        if jats_path:
            row['raw_jats_path'] = str(jats_path)
            row['raw_jats_sha256'] = hashlib.sha256(fetched['raw_jats']).hexdigest()
        references.append(row)
        audit['sources'].append({key: value for key, value in row.items() if key != 'context'})
    # Structured DOI metadata can identify aliases that their URLs alone could
    # not establish. Keep the actual best-scoped document and its full provenance.
    references = deduplicate_references(references)
    audit['deduplicated_sources'] = [duplicate | {'article_identity': row['article_identity'],
                                                'kept_source': row['source']}
                                       for row in references for duplicate in row['duplicate_sources']]
    from researchclaw.orchestration.public_code import acquire_public_code
    audit['public_code'] = acquire_public_code(root, references, config.research.topic)
    # Source metadata must describe the actual bounded code acquisition, rather
    # than leaving model consumers with an unfetched link and an implied method.
    audit['sources'] = [{key: value for key, value in row.items() if key != 'context'} for row in references]
    audit["status"] = "completed" if references else "unavailable"
    write_json(audit_path, audit)
    return references
