"""Bounded schema families, original row samples and path provenance for tables."""
from __future__ import annotations

from collections import Counter, deque
import csv
import io
import json
import os
from pathlib import Path
from typing import Any


EXCLUDED = {'target_study', 'evaluation', 'private', 'secrets', 'node_modules', '__pycache__'}


def table_catalog(root: Path, *, max_entries: int = 2000, max_depth: int = 8,
                  max_groups: int = 24) -> dict[str, Any]:
    root = root.resolve()
    pending = deque([(root, 0)]) if root.is_dir() else deque()
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    scanned = 0
    tables = 0
    errors = 0
    truncated = False
    documentation = []
    document_budget = 8000
    while pending and scanned < max_entries:
        directory, depth = pending.popleft()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            errors += 1
            continue
        for entry in entries:
            scanned += 1
            if scanned > max_entries:
                truncated = True
                break
            if entry.name.startswith('.') or entry.name in EXCLUDED or entry.is_symlink():
                continue
            path = Path(entry.path)
            if entry.is_dir():
                if depth < max_depth:
                    pending.append((path, depth + 1))
                else:
                    truncated = True
                continue
            if not entry.is_file():
                continue
            if (path.suffix.lower() in {'.md', '.txt', '.rst'} and len(documentation) < 8 and document_budget > 0
                    and any(term in path.stem.casefold() for term in ('readme', 'dataset', 'data_info', 'schema', 'metadata', 'dictionary'))):
                try:
                    with path.open('rb') as handle:
                        raw_doc = handle.read(4096)
                    decoded = raw_doc.decode('utf-8-sig', errors='replace')
                    doc = decoded[:min(3000, document_budget)]
                    documentation.append({'path': path.relative_to(root).as_posix(), 'text': doc,
                                          'truncated': len(doc) < len(decoded) or entry.stat().st_size > len(raw_doc)})
                    document_budget -= len(doc)
                except OSError:
                    errors += 1
            if path.suffix.lower() not in {'.csv', '.tsv'}:
                continue
            tables += 1
            relative = path.relative_to(root).as_posix()
            try:
                with path.open('rb') as handle:
                    raw = handle.read(32768)
                try:
                    text = raw.decode('utf-8-sig')
                    encoding = 'utf-8-sig'
                except UnicodeDecodeError:
                    text = raw.decode('latin-1')
                    encoding = 'latin-1-fallback'
                delimiter = '\t' if path.suffix.lower() == '.tsv' else ','
                try:
                    delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',;\t').delimiter
                except csv.Error:
                    pass
                reader = csv.reader(io.StringIO(text), delimiter=delimiter)
                header = next(reader, [])
                if not header:
                    continue
                # A bounded prefix is not a complete header when no newline fits.
                header_limited = len(raw) == 32768 and '\n' not in text
                key = tuple(header) if not header_limited else (*header, relative)
                if key not in groups:
                    groups[key] = {'columns': header[:80], 'column_count': len(header), 'files': 0,
                                   'header_limited': header_limited, 'examples': [], 'path_families': Counter(),
                                   'categorical_samples': {}, 'delimiter': delimiter, 'encodings': Counter()}
                group = groups[key]
                group['files'] += 1
                group['encodings'][encoding] += 1
                parts = Path(relative).parts
                group['path_families']['/'.join(parts[:min(2, len(parts)-1)]) or '.'] += 1
                rows = []
                for _ in range(2):
                    row = next(reader, None)
                    if row is None:
                        break
                    rows.append([cell[:160] for cell in row[:80]])
                    for name, value in zip(header[:80], row[:80]):
                        if not value or len(value) > 80:
                            continue
                        try:
                            float(value)
                        except ValueError:
                            values = group['categorical_samples'].setdefault(name, [])
                            if value not in values and len(values) < 8:
                                values.append(value)
                group['examples'].append({'path': relative, 'rows': rows, 'sample_complete_file': len(raw) >= entry.stat().st_size})
            except (OSError, csv.Error):
                errors += 1
    result = []
    budget = 23000
    details_limited = False
    for group in sorted(groups.values(), key=lambda item: item['files'], reverse=True)[:max_groups]:
        examples = group['examples']
        group['examples'] = [examples[i] for i in sorted({0, len(examples)//2, len(examples)-1})]
        group['path_families'] = dict(group['path_families'].most_common(12))
        group['encodings'] = dict(group['encodings'])
        if len(json.dumps(group, ensure_ascii=False)) > budget:
            details_limited = True
            group['examples'] = [{**item, 'rows': [row[:16] for row in item['rows'][:1]], 'row_columns_limited': True}
                                 for item in group['examples']]
            group['categorical_samples'] = {name: values[:2] for name, values in group['categorical_samples'].items()}
        size = len(json.dumps(group, ensure_ascii=False))
        if size > budget:
            continue
        budget -= size
        result.append(group)
    return {'table_files_seen': tables, 'schema_groups_seen': len(groups), 'schema_groups': result,
            'documentation': documentation,
            'groups_omitted': len(groups)-len(result), 'details_limited': details_limited,
            'scan_truncated': truncated or bool(pending),
            'entries_scanned': min(scanned, max_entries), 'unreadable_tables': errors,
            'guidance': 'These are original headers, values and paths, not inferred scientific roles. '
                        'Map each schema separately before combining tables. Region, method or other metadata '
                        'may be encoded in paths or adjacent documentation. Keep input and derived-result lineage '
                        'distinct. Preserve fractional interval boundaries and units; do not infer that all changes '
                        'are annual rates, or that missing inline metadata means observations are absent. '
                        'When documentation defines interval totals, derive rates using the actual fractional '
                        'duration before calendar aggregation. Check interval-total conservation, partial-year '
                        'coverage and within-source temporal aggregation before pooling independent sources; '
                        'rounding dates or treating monthly changes as annual rates breaks the units.'}
