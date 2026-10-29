"""Controller-owned original quotations for method-source selection."""
from __future__ import annotations

import hashlib
import json
import re

from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.compact_state import file_hash


def build_source_anchors(task: str, references: dict | list, query: str = '') -> dict:
    """Keep bounded original spans, never generated summaries or new instructions."""
    from researchclaw.orchestration.method_probes import _reference_rows, _reference_file
    from researchclaw.orchestration.task_fidelity import instruction_sources
    anchors = []
    existing = set()
    def add(reference: str, text: str, sha: str, start: int, end: int) -> None:
        quote = text[start:end]
        if not quote.strip():
            return
        identity = hashlib.sha256(json.dumps([reference, sha, start, end]).encode()).hexdigest()[:20]
        if identity in existing:
            return
        existing.add(identity)
        anchors.append({'id': 'A'+identity, 'reference': reference, 'source_sha256': sha,
            'start_character': start, 'end_character': end, 'quote': quote,
            'quote_sha256': hashlib.sha256(quote.encode()).hexdigest()})
    task_sha = hashlib.sha256(task.encode()).hexdigest()
    for fragment in list(instruction_sources(task).values())[:24]:
        start = task.find(fragment)
        if start >= 0:
            add('task', task, task_sha, start, start+len(fragment))
    words = set(re.findall(r'[A-Za-z][A-Za-z0-9_]{3,}', (task+' '+query).casefold()))
    ignored = {'this','that','with','from','have','will','which','their','were','been','into','source','method','report'}
    words -= ignored
    pools = []
    seen = set()
    for row in _reference_rows(references):
        identity = (row['path'], row['sha256'])
        if identity in seen:
            continue
        seen.add(identity)
        path = _reference_file(row['path'])
        if file_hash(path) != row['sha256']:
            raise ContractError('Method-source anchor cache hash changed')
        text = path.read_text(encoding='utf-8')
        reference = next(name for name in row['names'] if isinstance(name, str) and name.strip())
        spans = []
        start = 0
        while start < len(text):
            end = min(len(text), start+1200)
            if end < len(text):
                boundary = text.rfind('\n', start+600, end)
                if boundary > start:
                    end = boundary
            quote = text[start:end]
            tokens = set(re.findall(r'[A-Za-z][A-Za-z0-9_]{3,}', quote.casefold()))
            if len(quote.strip()) >= 40:
                spans.append((len(tokens & words), start, end))
            start = end
        spans.sort(key=lambda item: (-item[0], item[1]))
        # Equations often have few lexical topic words. Preserve bounded,
        # complete original formula blocks before ranking prose; otherwise a
        # recovered method can remain impossible to select from the catalog.
        formulas = [(len(words)+1, match.start(), match.end()) for match in re.finditer(
            r'(?m)^\[Original JATS formula[^\n]*\]\n```(?:latex|mathml)\n.*?\n```', text, re.S)
            if match.end()-match.start() <= 8192]
        pools.append([reference, text, row['sha256'], formulas[:16]+spans[:16]])
    # Round-robin source selection prevents one long source consuming every
    # anchor. Full scientific source packets remain available independently.
    while len(anchors) < 64 and any(pool[3] for pool in pools):
        for reference, text, sha, spans in pools:
            if spans and len(anchors) < 64:
                _, start, end = spans.pop(0)
                add(reference, text, sha, start, end)
    return {'schema_version': 'method-source-anchors-v1', 'task_sha256': task_sha, 'anchors': anchors,
        'scope': 'Selected original spans only. Selection does not prove scientific relevance or method equivalence. '
                 'Unselected text is not evidence of absence; the full source packet is separate.'}


def resolve_source_anchors(plan: dict, catalog: dict) -> None:
    """Insert owned bytes before normal method/source/scientific validation."""
    by_id = {row['id']: row for row in catalog['anchors']}
    for step in plan.get('method_steps', []):
        source = step.get('source') if isinstance(step, dict) else None
        if not isinstance(source, dict) or 'anchor_id' not in source:
            continue  # Historical quote-based plans retain strict validation.
        anchor = by_id.get(source['anchor_id'])
        if anchor is None:
            raise ContractError('Method source anchor must identify a supplied original source span')
        for key in ('reference', 'quote', 'source_sha256', 'start_character', 'end_character'):
            if key in source and source[key] != anchor[key]:
                raise ContractError('Method source anchor conflicts with supplied reference/quote; select an anchor without retyping its fields')
        source.update({key: anchor[key] for key in ('reference', 'quote', 'source_sha256', 'start_character', 'end_character')})


def anchor_prompt(catalog: dict) -> str:
    return ('\nController-owned original method-source anchors:\n' + json.dumps(catalog, ensure_ascii=False)
        + '\nFor each method_steps source return ONLY {"anchor_id":"an existing ID above"}. '
          'The controller inserts the exact original reference and quotation. Select a span that actually '
          'supports the operation and assumptions; an anchor is not proof or permission to substitute a method. '
          'Do not quote controller guidance as RAW task evidence and do not invent a source/anchor. '
          'Other method fields and independent probes remain required. Historical reference/quote examples '
          'above explain the eventual expanded representation; this generation selects source anchors instead.\n')
