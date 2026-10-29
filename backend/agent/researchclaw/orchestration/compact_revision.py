"""Scoped, hash-checked edits with recoverable publication and retained evidence."""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.compact_state import file_hash


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def scoped_path(root: Path, name: str) -> Path:
    path = Path(name)
    if (not name or path.is_absolute() or any(part.startswith('.') or part in {
            'data', 'outputs', 'related_work', '__pycache__'} for part in path.parts)):
        raise ContractError(f"Invalid revision path: {name}")
    target = root / path
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ContractError(f"Revision path escapes its artifact scope: {name}")
    return target


def normalize_scope_aliases(value: dict[str, Any], originals: dict[str, str],
                            scope_prefix: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Resolve an exact run-relative prefix to an existing, listed scope file.

    Never guess by basename, normalize parent traversal, accept absolute paths,
    change source text/hashes/line ranges, or grant a new file an alias.
    """
    normalized = deepcopy(value)
    aliases = []
    prefix = scope_prefix.rstrip('/') + '/'
    for field in ('edits', 'line_edits', 'symbol_edits'):
        rows = normalized.get(field, [])
        if not isinstance(rows, list):
            continue  # Existing schema validation reports malformed edits.
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('path'), str):
                continue
            name = row['path']
            if name in originals or not name.startswith(prefix):
                continue
            candidate = name[len(prefix):]
            if (candidate in originals and not Path(name).is_absolute()
                    and not any(part.startswith('.') for part in Path(name).parts)):
                row['path'] = candidate
                aliases.append({'original': name, 'scope_relative': candidate})
    return normalized, aliases


def apply_edits(root: Path, originals: dict[str, str], value: dict[str, Any], *,
                allow_new_source: bool = False,
                visible_lines: dict[str, list[tuple[int, int]]] | None = None) -> dict[str, str]:
    edits, additions, line_edits, named = (value.get('edits', []), value.get('add_files', []),
                                         value.get('line_edits', []), value.get('symbol_edits', []))
    if (not all(isinstance(items, list) for items in (edits, additions, line_edits, named))
            or not 0 < len(edits) + len(additions) + len(line_edits) + len(named) <= 64 or len(additions) > 20):
        raise ContractError('Return 1–64 scoped edits/line_edits/symbol_edits/add_files, with at most 20 new source files')
    if named:
        from researchclaw.orchestration.symbol_edits import expand_symbol_edits
        line_edits = [*line_edits, *expand_symbol_edits(originals, named)]
    changed: dict[str, str] = {}
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or not all(isinstance(edit.get(k), str) for k in ('path', 'sha256', 'old', 'new')):
            raise ContractError('Each edit needs string path, sha256, old and new')
        name = edit['path']
        scoped_path(root, name)
        if name not in originals or edit['sha256'] != digest(originals[name]):
            raise ContractError(f'Stale or out-of-scope edit: {name}; use the supplied complete file hash')
        current = changed.get(name, originals[name])
        if not edit['old'] or current.count(edit['old']) != 1:
            raise ContractError(f'Edit in {name} (item {index + 1}) must match exactly one unchanged original text block; '
                                'use the displayed original line range instead of reconstructing old prose')
        if len(edit['old']) > 12000:
            raise ContractError(f'Edit in {name} is too broad; identify local blocks of at most 12,000 characters')
        changed[name] = current.replace(edit['old'], edit['new'], 1)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in line_edits:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('path', 'sha256', 'new')):
            raise ContractError('Each line edit needs path, sha256, start_line, end_line and new')
        name = item['path']
        scoped_path(root, name)
        if name not in originals or item['sha256'] != digest(originals[name]):
            raise ContractError(f'Stale or out-of-scope line edit: {name}')
        if name in changed:
            raise ContractError(f'Do not mix original-text and numbered-line edits in {name}')
        start, end = item.get('start_line'), item.get('end_line')
        lines = originals[name].splitlines(keepends=True)
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ContractError(f'Invalid inclusive original line range in {name}')
        if visible_lines is None or not any(a <= start <= end <= b for a, b in visible_lines.get(name, [])):
            raise ContractError(f'Line edit in {name} includes original lines omitted from the model context')
        if len(''.join(lines[start-1:end])) > 12000:
            raise ContractError(f'Line edit in {name} is too broad; replace local blocks of at most 12,000 characters')
        grouped.setdefault(name, []).append(item)
    for name, items in grouped.items():
        items.sort(key=lambda item: item['start_line'])
        if any(a['end_line'] >= b['start_line'] for a, b in zip(items, items[1:])):
            raise ContractError(f'Overlapping original line ranges in {name}')
        lines = originals[name].splitlines(keepends=True)
        for item in reversed(items):
            start, end, new = item['start_line'], item['end_line'], item['new']
            # Preserve the following untouched line's boundary when the model
            # omits a terminal newline in a nonempty replacement.
            if new and lines[end-1].endswith('\n') and not new.endswith('\n'):
                new += '\r\n' if lines[end-1].endswith('\r\n') else '\n'
            lines[start-1:end] = [new]
        changed[name] = ''.join(lines)
    for item in additions:
        if not allow_new_source or not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('path', 'content')):
            raise ContractError('New files are allowed only for source revisions and need path/content')
        name = item['path']
        if scoped_path(root, name).exists() or name in originals or name in changed:
            raise ContractError(f'New source already exists: {name}; edit it instead')
        changed[name] = item['content']
    changed = {name: text for name, text in changed.items() if text != originals.get(name)}
    if not changed or any(not text.strip() for text in changed.values()):
        raise ContractError('A revision must make a nonempty, substantive file edit')
    for name, text in changed.items():
        if name.endswith('.py'):
            try:
                ast.parse(text)
            except SyntaxError as exc:
                raise ContractError(f'Invalid patched Python in {name}: {exc}') from exc
    return changed


def file_packet(originals: dict[str, str], limit: int = 60000) -> str:
    parts = []
    remaining = limit
    for name, text in originals.items():
        header = f'\n### {name} sha256={digest(text)} characters={len(text)}\n'
        if remaining < len(header) + 100:
            break
        room = remaining - len(header)
        if len(text) > room:
            half = max(0, (room - 90) // 2)
            text = text[:half] + '\n[... original text omitted; edits must match visible exact blocks ...]\n' + text[-half:]
        block = header + text
        parts.append(block)
        remaining -= len(block)
    return ''.join(parts)


def numbered_packet(originals: dict[str, str], limit: int = 60000) -> tuple[str, dict[str, list[tuple[int, int]]]]:
    """Stable original line addresses; omitted lines cannot be edited by address."""
    parts, visible = [], {}
    remaining = limit
    for number, (name, text) in enumerate(originals.items()):
        lines = text.splitlines(keepends=True)
        header = f'\n### {name} sha256={digest(text)} characters={len(text)} lines={len(lines)}\n'
        reserve = min(1000, remaining // max(1, len(originals) - number)) * (len(originals) - number - 1)
        room = remaining - len(header) - reserve
        if room < 180:
            continue
        rendered = [f'L{index}: {line.rstrip(chr(10)).rstrip(chr(13))}\n' for index, line in enumerate(lines, 1)]
        if sum(map(len, rendered)) <= room:
            body = ''.join(rendered)
            visible[name] = [(1, len(lines))] if lines else []
        else:
            marker = '[... original lines omitted; those line ranges cannot be edited ...]\n'
            half = max(0, (room - len(marker)) // 2)
            first, last, used = 0, len(lines), 0
            while first < len(lines) and used + len(rendered[first]) <= half:
                used += len(rendered[first]); first += 1
            used = 0
            while last > first and used + len(rendered[last-1]) <= half:
                last -= 1; used += len(rendered[last])
            body = ''.join(rendered[:first]) + marker + ''.join(rendered[last:])
            visible[name] = ([(1, first)] if first else []) + ([(last+1, len(lines))] if last < len(lines) else [])
        block = header + body
        parts.append(block)
        remaining -= len(block)
    return ''.join(parts), visible


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        candidate.write_text(text, encoding='utf-8')
        candidate.replace(path)
    finally:
        candidate.unlink(missing_ok=True)


def publish(root: Path, originals: dict[str, str], changed: dict[str, str], *,
            journal_root: Path, kind: str, context: str) -> Path:
    # Validate every precondition before publishing the first edit.
    for name, text in originals.items():
        path = scoped_path(root, name)
        if not path.is_file() or file_hash(path) != digest(text):
            raise ContractError(f'Artifact changed while preparing revision: {name}')
    for name in changed:
        if name not in originals and scoped_path(root, name).exists():
            raise ContractError(f'New source appeared while preparing revision: {name}')
    journal = journal_root / ('revision-' + uuid.uuid4().hex)
    for name, text in changed.items():
        _atomic_text(journal / 'after' / name, text)
        if name in originals:
            _atomic_text(journal / 'before' / name, originals[name])
    record = {'root': str(root.resolve()), 'kind': kind, 'context': context, 'status': 'publishing',
              'before': {name: digest(originals[name]) if name in originals else None for name in changed},
              'after': {name: digest(text) for name, text in changed.items()},
              'retained': {name: digest(text) for name, text in originals.items() if name not in changed}}
    write_json(journal / 'manifest.json', record)
    try:
        for name, text in changed.items():
            _atomic_text(scoped_path(root, name), text)
        record['status'] = 'committed'
        write_json(journal / 'manifest.json', record)
    except BaseException:
        restore(journal)
        raise
    return journal


def restore(journal: Path) -> None:
    record = read_json(journal / 'manifest.json')
    root = Path(record['root'])
    # Do not roll back an unrelated external edit.
    for name, before in record['before'].items():
        path = scoped_path(root, name)
        current = file_hash(path) if path.is_file() else None
        if current not in {before, record['after'][name]}:
            raise ContractError(f'Interrupted revision conflicts with a newer external edit: {name}')
        if before is not None and file_hash(journal / 'before' / name) != before:
            raise ContractError(f'Interrupted revision has a corrupt backup: {name}')
    for name, before in record['before'].items():
        path = scoped_path(root, name)
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_text(path, (journal / 'before' / name).read_text(encoding='utf-8'))
    record['status'] = 'rolled_back'
    write_json(journal / 'manifest.json', record)


def recover(run_root: Path) -> None:
    for path in (run_root / 'research_revisions').glob('revision-*/manifest.json'):
        record = read_json(path)
        if not Path(record.get('root', '/')).resolve().is_relative_to(run_root.resolve()):
            raise ContractError('Revision journal has an invalid artifact root')
        if record.get('status') == 'publishing':
            restore(path.parent)


def committed(journal_root: Path, kind: str, context: str) -> list[str] | None:
    for path in sorted(journal_root.glob('revision-*/manifest.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        row = read_json(path)
        if row.get('status') != 'committed' or row.get('kind') != kind or row.get('context') != context:
            continue
        root = Path(row['root'])
        expected = {**row['retained'], **row['after']}
        if all(scoped_path(root, name).is_file() and file_hash(root / name) == sha for name, sha in expected.items()):
            return list(row['after'])
    return None
