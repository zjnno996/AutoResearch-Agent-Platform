"""Explicit availability and bounded contents of controller-recorded outputs."""
from __future__ import annotations

import json
from pathlib import Path
import re

import yaml

from researchclaw.orchestration.artifacts import project_document, read_json
from researchclaw.orchestration.compact_state import file_hash


def executed_packet(root: Path, limit: int = 18000) -> tuple[str, list[Path]]:
    work = root / 'stage-11/experiment'
    receipt = read_json(root / 'stage-16/experiment_provenance.json')
    plan_path = root / 'stage-09/exp_plan.yaml'
    plan = yaml.safe_load(plan_path.read_text(encoding='utf-8')) if plan_path.is_file() else {}
    declared = set(re.findall(r'outputs/[\w./-]+', json.dumps((plan or {}).get('requirement_map', {}))))
    records, candidates = [], []
    for relative, sha in sorted(receipt.get('output_hashes', {}).items()):
        path = work / relative
        if (not relative.startswith('outputs/') or path.is_symlink()
                or not path.resolve().is_relative_to(work.resolve() / 'outputs')):
            continue
        exists = path.is_file()
        matches = exists and file_hash(path) == sha
        records.append({'path': relative, 'exists': exists, 'matches_execution': matches,
                        'bytes': path.stat().st_size if exists else None})
        if (matches and path.suffix.lower() in {'.md', '.txt', '.json', '.csv', '.tsv', '.py'}
                and path.name not in {'metrics.json', 'analysis.md', 'stdout.log', 'stderr.log'}):
            candidates.append(path)
    def priority(path: Path) -> tuple[int, int, str]:
        relative = path.relative_to(work).as_posix()
        proof = any(term in relative.casefold() for term in ('proof', 'certificate', 'theorem'))
        return (0 if proof else 1 if relative in declared else 2, path.stat().st_size, relative)
    candidates.sort(key=priority)
    selected = candidates[:16]
    recorded = {row['path'] for row in records}
    manifest = {'recorded_output_count': len(records), 'outputs': records[:200],
                'manifest_entries_omitted': max(0, len(records) - 200),
                'declared_but_not_recorded': sorted(path for path in declared
                    if path not in recorded and not any(name.startswith(path.rstrip('/') + '/') for name in recorded)),
                'content_selected': [path.relative_to(work).as_posix() for path in selected],
                'content_omitted': [path.relative_to(work).as_posix() for path in candidates[16:]]}
    heading = ('\nActual controller-recorded output manifest and bounded contents. '
               'Only listed, hash-matching files are executed outputs; do not repeat draft promises of absent files. '
               'A written mathematical argument can support a theorem within its scope. A symbolic identity '
               'certificate does not establish proof-assistant verification. '
               'Content omitted by the budget is not evidence that a file is absent.\n')
    listing = json.dumps(manifest, ensure_ascii=False)
    if len(heading) + len(listing) > limit - 500:
        return heading + listing[:max(0, limit-len(heading)-70)] + '\n[MANIFEST TRUNCATED: inspect the original execution receipt.]', []
    parts, included = [heading, listing], []
    remaining = limit - sum(map(len, parts))
    for index, path in enumerate(selected):
        header = f'\n### Executed artifact {path.relative_to(work)} sha256={file_hash(path)}\n'
        share = remaining // max(1, len(selected) - index)
        if share < len(header) + 300:
            break
        block = header + project_document(path, min(6000, share-len(header)))
        parts.append(block)
        included.append(path)
        remaining -= len(block)
    return ''.join(parts), included
