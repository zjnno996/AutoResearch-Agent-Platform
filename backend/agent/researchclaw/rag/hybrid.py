"""Stdlib-only hybrid retrieval over ResearchClaw artifacts.

This is a production-friendly foundation: metadata-rich chunks are persisted as
JSONL and retrieval combines lexical BM25-style scoring with a lightweight
semantic-ish token overlap score. It can later be swapped for pgvector/Milvus +
a neural reranker without changing stage code.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}")
_STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'that', 'this', 'into', 'using', 'based',
    '研究', '论文', '方法', '这个', '一个', '我们', '以及', '进行', '可以', '问题',
}


@dataclass
class RetrievalChunk:
    chunk_id: str
    text: str
    source: str
    stage: str
    artifact: str
    title: str = ''
    paper_id: str = ''
    year: str = ''
    chunk_type: str = 'artifact'
    citation_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalHit:
    chunk: RetrievalChunk
    score: float
    lexical_score: float
    vector_score: float
    matched_terms: list[str]
    rerank_score: float = 0.0


def tokenize(text: str) -> list[str]:
    tokens = [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]


def _hash_bucket(token: str, dims: int = 512) -> int:
    digest = hashlib.blake2b(token.encode('utf-8', errors='ignore'), digest_size=8).hexdigest()
    return int(digest, 16) % dims


def _hashed_vector(tokens: list[str], *, dims: int = 512) -> dict[int, float]:
    counts = Counter(tokens)
    vec: dict[int, float] = defaultdict(float)
    for token, count in counts.items():
        vec[_hash_bucket(token, dims=dims)] += 1.0 + math.log(count)
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def _cosine_sparse(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(v * right.get(k, 0.0) for k, v in left.items())


def _split_text(text: str, *, max_chars: int = 1200) -> list[str]:
    text = text.strip()
    if not text:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    chunks: list[str] = []
    current = ''
    for block in blocks:
        if len(current) + len(block) + 2 <= max_chars:
            current = f"{current}\n\n{block}".strip()
            continue
        if current:
            chunks.append(current)
        if len(block) <= max_chars:
            current = block
        else:
            chunks.extend(block[i:i + max_chars] for i in range(0, len(block), max_chars))
            current = ''
    if current:
        chunks.append(current)
    return chunks


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _find_artifact(run_dir: Path, name: str) -> Path | None:
    for stage_dir in sorted(run_dir.glob('stage-*')):
        candidate = stage_dir / name
        if candidate.exists():
            return candidate
    return None


def build_artifact_chunks(run_dir: Path) -> list[RetrievalChunk]:
    chunks: list[RetrievalChunk] = []

    def add_text(source_path: Path, text: str, *, title: str = '', chunk_type: str = 'artifact', paper_id: str = '', year: str = '', citation_count: int = 0, metadata: dict[str, Any] | None = None) -> None:
        stage = source_path.parent.name if source_path.parent.name.startswith('stage-') else ''
        for idx, chunk_text in enumerate(_split_text(text)):
            chunks.append(RetrievalChunk(
                chunk_id=f"{source_path.as_posix()}#{idx}",
                text=chunk_text,
                source=source_path.as_posix(),
                stage=stage,
                artifact=source_path.name,
                title=title,
                paper_id=paper_id,
                year=year,
                chunk_type=chunk_type,
                citation_count=citation_count,
                metadata=metadata or {},
            ))

    for name in ('shortlist.jsonl', 'candidates.jsonl'):
        path = _find_artifact(run_dir, name)
        if not path:
            continue
        for idx, row in enumerate(_jsonl_rows(path), start=1):
            title = str(row.get('title', '') or f'Paper {idx}').strip()
            abstract = str(row.get('abstract', '') or row.get('summary', '') or '').strip()
            venue = str(row.get('venue', '') or row.get('journal', '') or row.get('source', '') or '').strip()
            keep_reason = str(row.get('keep_reason', '') or row.get('relevance_reason', '') or '').strip()
            text = '\n'.join(part for part in (
                f"Title: {title}",
                f"Venue/Source: {venue}" if venue else '',
                f"Year: {row.get('year', '')}" if row.get('year') else '',
                f"Abstract: {abstract}" if abstract else '',
                f"Reason: {keep_reason}" if keep_reason else '',
            ) if part)
            add_text(
                path,
                text,
                title=title,
                chunk_type='paper',
                paper_id=str(row.get('paper_id', '') or row.get('id', '') or title),
                year=str(row.get('year', '') or ''),
                citation_count=int(row.get('citation_count', 0) or 0) if str(row.get('citation_count', 0) or 0).isdigit() else 0,
                metadata={
                    'venue': venue,
                    'url': str(row.get('url', '') or ''),
                    'doi': str(row.get('doi', '') or ''),
                    'source': str(row.get('source', '') or ''),
                },
            )

    cards_dir = _find_artifact(run_dir, 'cards')
    if cards_dir and cards_dir.is_dir():
        for card_path in sorted(cards_dir.glob('*.md')):
            add_text(card_path, card_path.read_text(encoding='utf-8', errors='ignore'), title=card_path.stem, chunk_type='knowledge_card')

    indexed_sources = {c.source for c in chunks}
    for name in ('synthesis.md', 'core_ideas.md', 'idea_review.md', 'idea_branch_synthesis.md', 'exp_plan.yaml'):
        path = _find_artifact(run_dir, name)
        if path and path.is_file() and path.as_posix() not in indexed_sources:
            add_text(path, path.read_text(encoding='utf-8', errors='ignore'), title=name, chunk_type='artifact')
            indexed_sources.add(path.as_posix())

    # Fallback: make the retriever useful even for early-stage projects that
    # have only goal/problem/search-plan artifacts and no screened papers yet.
    for stage_dir in sorted(run_dir.glob('stage-*')):
        if not stage_dir.is_dir():
            continue
        for path in sorted(stage_dir.iterdir()):
            if path.is_dir() or path.as_posix() in indexed_sources:
                continue
            if path.suffix.lower() not in {'.md', '.yaml', '.yml', '.json', '.jsonl', '.txt'}:
                continue
            try:
                text = path.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if text.strip():
                add_text(path, text, title=path.name, chunk_type='artifact')
                indexed_sources.add(path.as_posix())
    return chunks


def write_index(run_dir: Path, index_path: Path | None = None) -> Path:
    index_path = index_path or (run_dir / 'rag_index.jsonl')
    chunks = build_artifact_chunks(run_dir)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open('w', encoding='utf-8') as handle:
        for chunk in chunks:
            handle.write(json.dumps(asdict(chunk), ensure_ascii=False) + '\n')
    return index_path


def load_index(index_path: Path) -> list[RetrievalChunk]:
    chunks: list[RetrievalChunk] = []
    if not index_path.exists():
        return chunks
    for line in index_path.read_text(encoding='utf-8', errors='ignore').splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get('text'):
            chunks.append(RetrievalChunk(**{k: data.get(k, '') for k in RetrievalChunk.__dataclass_fields__}))
    return chunks


def _intent_bonus(chunk: RetrievalChunk, query_terms: set[str], intent: str) -> float:
    text = (chunk.text + ' ' + chunk.title).lower()
    bonus = 0.0
    if intent in {'idea', 'novelty'}:
        if chunk.chunk_type in {'paper', 'knowledge_card'}:
            bonus += 0.18
        if any(t in text for t in ('limitation', '局限', 'gap', '空缺', 'future work', 'baseline', 'reviewer')):
            bonus += 0.22
        if chunk.citation_count:
            bonus += min(math.log10(chunk.citation_count + 1) * 0.08, 0.25)
    elif intent in {'experiment', 'design'}:
        if any(t in text for t in ('dataset', '数据集', 'metric', '指标', 'baseline', 'split', 'hyperparameter')):
            bonus += 0.30
    elif intent in {'chat', 'qa'}:
        if chunk.artifact in {'core_ideas.md', 'synthesis.md', 'idea_review.md'}:
            bonus += 0.20
    if chunk.year and chunk.year.isdigit():
        year = int(chunk.year)
        if year >= 2023:
            bonus += 0.05
    return bonus


def _rerank_hits(hits: list[RetrievalHit], *, intent: str, query_terms: set[str]) -> list[RetrievalHit]:
    reranked: list[RetrievalHit] = []
    for hit in hits:
        bonus = _intent_bonus(hit.chunk, query_terms, intent)
        # Prefer concise chunks with multiple query concepts over very long noisy chunks.
        length_penalty = min(len(hit.chunk.text) / 6000.0, 0.18)
        rerank_score = hit.score + bonus - length_penalty
        reranked.append(RetrievalHit(
            chunk=hit.chunk,
            score=round(rerank_score, 6),
            lexical_score=hit.lexical_score,
            vector_score=hit.vector_score,
            matched_terms=hit.matched_terms,
            rerank_score=round(bonus - length_penalty, 6),
        ))
    reranked.sort(key=lambda h: h.score, reverse=True)
    return reranked


def hybrid_search(
    chunks: list[RetrievalChunk],
    query: str,
    *,
    top_k: int = 10,
    stage_filter: set[str] | None = None,
    intent: str = 'general',
) -> list[RetrievalHit]:
    if stage_filter:
        chunks = [c for c in chunks if c.stage in stage_filter]
    if not chunks:
        return []
    query_terms = tokenize(query)
    if not query_terms:
        return []
    q_counter = Counter(query_terms)
    q_vec = _hashed_vector(query_terms)
    doc_tokens = [tokenize(c.text + ' ' + c.title) for c in chunks]
    doc_vecs = [_hashed_vector(toks) for toks in doc_tokens]
    avgdl = sum(len(t) for t in doc_tokens) / max(len(doc_tokens), 1)
    df: dict[str, int] = defaultdict(int)
    for toks in doc_tokens:
        for term in set(toks):
            df[term] += 1
    n_docs = len(chunks)
    hits: list[RetrievalHit] = []
    for chunk, toks, d_vec in zip(chunks, doc_tokens, doc_vecs):
        tf = Counter(toks)
        dl = max(len(toks), 1)
        lexical = 0.0
        matched: list[str] = []
        for term, qtf in q_counter.items():
            if tf[term] <= 0:
                continue
            matched.append(term)
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf[term] + 1.5 * (1 - 0.75 + 0.75 * dl / max(avgdl, 1))
            lexical += idf * (tf[term] * 2.5 / denom) * qtf
        vector = _cosine_sparse(q_vec, d_vec)
        if not matched and vector < 0.08:
            continue
        type_bonus = {'paper': 0.14, 'knowledge_card': 0.12, 'artifact': 0.04}.get(chunk.chunk_type, 0.0)
        score = lexical * 0.68 + vector * 3.2 + type_bonus
        hits.append(RetrievalHit(chunk=chunk, score=round(score, 6), lexical_score=round(lexical, 6), vector_score=round(vector, 6), matched_terms=matched[:12]))
    return _rerank_hits(hits, intent=intent, query_terms=set(query_terms))[:top_k]


def retrieval_report(hits: list[RetrievalHit]) -> dict[str, Any]:
    return {
        'count': len(hits),
        'hits': [
            {
                'score': h.score,
                'lexical_score': h.lexical_score,
                'vector_score': h.vector_score,
                'rerank_score': h.rerank_score,
                'matched_terms': h.matched_terms,
                'source': h.chunk.source,
                'stage': h.chunk.stage,
                'artifact': h.chunk.artifact,
                'title': h.chunk.title,
                'paper_id': h.chunk.paper_id,
                'chunk_type': h.chunk.chunk_type,
                'citation_count': h.chunk.citation_count,
                'metadata': h.chunk.metadata,
                'preview': h.chunk.text[:500],
            }
            for h in hits
        ],
    }


def expand_research_queries(topic: str, *, intent: str = 'idea') -> list[str]:
    base = topic.strip()
    if intent == 'idea':
        suffixes = [
            '相近文献 research gap limitation novelty risk reviewer',
            'strong baseline dataset metric experiment ablation failure condition',
            'survey benchmark state of the art recent methods future work',
            'two week MVP feasibility compute budget implementation',
        ]
    elif intent == 'experiment':
        suffixes = [
            'dataset split baseline metric hyperparameter implementation',
            'ablation robustness generalization compute budget',
        ]
    else:
        suffixes = ['summary evidence related work limitation method']
    return [f'{base} {suffix}'.strip() for suffix in suffixes]


def merge_hits(*hit_lists: list[RetrievalHit], top_k: int = 12) -> list[RetrievalHit]:
    best: dict[str, RetrievalHit] = {}
    for hits in hit_lists:
        for hit in hits:
            key = hit.chunk.chunk_id
            if key not in best or hit.score > best[key].score:
                best[key] = hit
    merged = sorted(best.values(), key=lambda h: h.score, reverse=True)
    return merged[:top_k]


def retrieve_research_evidence(
    chunks: list[RetrievalChunk],
    topic: str,
    *,
    intent: str = 'idea',
    top_k: int = 12,
) -> list[RetrievalHit]:
    hit_lists = [hybrid_search(chunks, q, top_k=top_k, intent=intent) for q in expand_research_queries(topic, intent=intent)]
    return merge_hits(*hit_lists, top_k=top_k)


def _paper_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in ('shortlist.jsonl', 'candidates.jsonl'):
        path = _find_artifact(run_dir, name)
        if not path:
            continue
        for row in _jsonl_rows(path):
            title = str(row.get('title', '') or '').strip()
            key = str(row.get('paper_id', '') or row.get('id', '') or title).lower()
            if not title or key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def build_citation_graph(run_dir: Path) -> dict[str, Any]:
    """Build a citation/related-work graph from available paper metadata.

    Explicit citation edges are used when candidate rows contain references or
    citations. When APIs did not provide citation lists, the graph adds clearly
    marked ``inferred_similarity`` edges based on title/abstract overlap so the
    downstream workflow still gets related-work clusters without pretending they
    are true citations.
    """
    rows = _paper_rows(run_dir)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    title_to_id: dict[str, str] = {}
    for idx, row in enumerate(rows, start=1):
        title = str(row.get('title', '') or f'Paper {idx}').strip()
        paper_id = str(row.get('paper_id', '') or row.get('id', '') or title)
        title_to_id[title.lower()] = paper_id
        nodes.append({
            'id': paper_id,
            'title': title,
            'year': row.get('year', ''),
            'source': row.get('source', ''),
            'citation_count': row.get('citation_count', 0),
            'url': row.get('url', ''),
        })
    for row in rows:
        src_title = str(row.get('title', '') or '').strip()
        src = str(row.get('paper_id', '') or row.get('id', '') or src_title)
        for field_name, edge_type in (('references', 'backward_citation'), ('citations', 'forward_citation')):
            refs = row.get(field_name, [])
            if not isinstance(refs, list):
                continue
            for ref in refs[:30]:
                ref_title = str(ref.get('title', '') if isinstance(ref, dict) else ref).strip()
                dst = str(ref.get('paper_id', '') or ref.get('id', '') if isinstance(ref, dict) else '') or title_to_id.get(ref_title.lower(), ref_title)
                if dst:
                    edges.append({'source': src, 'target': dst, 'type': edge_type, 'weight': 1.0})
    # Inferred related-work edges from token overlap when explicit citations are sparse.
    paper_text = []
    for row in rows:
        title = str(row.get('title', '') or '')
        abstract = str(row.get('abstract', '') or row.get('summary', '') or '')
        paper_text.append((str(row.get('paper_id', '') or row.get('id', '') or title), set(tokenize(title + ' ' + abstract))))
    for i, (left_id, left_terms) in enumerate(paper_text):
        scored: list[tuple[float, str]] = []
        for j, (right_id, right_terms) in enumerate(paper_text):
            if i >= j or not left_terms or not right_terms:
                continue
            score = len(left_terms & right_terms) / max(len(left_terms | right_terms), 1)
            if score >= 0.08:
                scored.append((score, right_id))
        for score, right_id in sorted(scored, reverse=True)[:3]:
            edges.append({'source': left_id, 'target': right_id, 'type': 'inferred_similarity', 'weight': round(score, 4)})
    return {
        'nodes': nodes,
        'edges': edges,
        'stats': {
            'nodes': len(nodes),
            'edges': len(edges),
            'explicit_edges': sum(1 for e in edges if e['type'] != 'inferred_similarity'),
            'inferred_edges': sum(1 for e in edges if e['type'] == 'inferred_similarity'),
        },
    }


def write_citation_graph(run_dir: Path, output_path: Path) -> Path:
    graph = build_citation_graph(run_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding='utf-8')
    return output_path


def write_global_memory_index(projects_dir: Path, output_path: Path, *, max_projects: int = 20, max_chunks: int = 400) -> Path:
    chunks: list[RetrievalChunk] = []
    if projects_dir.is_dir():
        for project_dir in sorted((p for p in projects_dir.iterdir() if p.is_dir() and not p.name.startswith('_')), key=lambda p: p.stat().st_mtime, reverse=True)[:max_projects]:
            run_dirs = sorted(project_dir.glob('run-*')) or [project_dir]
            for run_dir in run_dirs[:3]:
                for chunk in build_artifact_chunks(run_dir):
                    chunk.metadata = {**chunk.metadata, 'project_id': project_dir.name, 'memory_scope': 'global'}
                    chunks.append(chunk)
                    if len(chunks) >= max_chunks:
                        break
                if len(chunks) >= max_chunks:
                    break
            if len(chunks) >= max_chunks:
                break
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8') as handle:
        for chunk in chunks:
            handle.write(json.dumps(asdict(chunk), ensure_ascii=False) + '\n')
    return output_path


def build_research_rag_bundle(
    run_dir: Path,
    topic: str,
    output_dir: Path,
    *,
    projects_dir: Path | None = None,
    intent: str = 'idea',
    top_k: int = 12,
) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    """Build project/global indexes, citation graph, retrieval report and prompt evidence."""
    output_dir.mkdir(parents=True, exist_ok=True)
    project_index_path = write_index(run_dir, output_dir / 'rag_index.jsonl')
    chunks = build_artifact_chunks(run_dir)
    global_chunks: list[RetrievalChunk] = []
    artifacts: list[str] = ['rag_index.jsonl']
    if projects_dir is not None:
        global_index_path = write_global_memory_index(projects_dir, output_dir / 'global_rag_index.jsonl')
        global_chunks = load_index(global_index_path)
        artifacts.append('global_rag_index.jsonl')
    citation_graph_path = write_citation_graph(run_dir, output_dir / 'citation_graph.json')
    artifacts.append('citation_graph.json')
    project_hits = retrieve_research_evidence(chunks, topic, intent=intent, top_k=top_k)
    global_hits = retrieve_research_evidence(global_chunks, topic, intent='chat', top_k=max(4, top_k // 3)) if global_chunks else []
    hits = merge_hits(project_hits, global_hits, top_k=top_k)
    report = retrieval_report(hits)
    report['index'] = {
        'project_index': project_index_path.as_posix(),
        'project_chunks': len(chunks),
        'global_chunks': len(global_chunks),
        'citation_graph': citation_graph_path.as_posix(),
        'intent': intent,
        'queries': expand_research_queries(topic, intent=intent),
    }
    (output_dir / 'rag_retrieval_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    artifacts.append('rag_retrieval_report.json')
    return format_hits_for_prompt(hits, max_chars=9000), tuple(artifacts), report


def format_hits_for_prompt(hits: list[RetrievalHit], *, max_chars: int = 8000) -> str:
    parts: list[str] = []
    used = 0
    for idx, hit in enumerate(hits, start=1):
        c = hit.chunk
        header = f"### Evidence {idx}: {c.title or c.artifact} ({c.stage}/{c.chunk_type}, score={hit.score})"
        block = f"{header}\n来源：{c.source}\n匹配词：{', '.join(hit.matched_terms)}\n{c.text.strip()}"
        if used + len(block) + 2 > max_chars:
            break
        parts.append(block)
        used += len(block) + 2
    return '\n\n'.join(parts)
