"""Replace named Python constructs using original AST boundaries, never guessed lines."""
from __future__ import annotations

import ast
import hashlib
import textwrap
from pathlib import Path
from typing import Any

from researchclaw.orchestration.compact_contracts import ContractError


def _binding(node: ast.AST) -> tuple[str, str] | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name, 'function'
    if isinstance(node, ast.ClassDef):
        return node.name, 'class'
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id, 'assignment'
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, 'assignment'
    return None


def symbols(text: str) -> dict[str, list[ast.AST]]:
    found: dict[str, list[ast.AST]] = {}
    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        binding = _binding(node)
        child_scope = scope
        if binding:
            name, kind = binding
            found.setdefault('.'.join((*scope, name)), []).append(node)
            if kind in {'function', 'class'}:
                child_scope = (*scope, name)
        for child in ast.iter_child_nodes(node):
            visit(child, child_scope)
    visit(ast.parse(text), ())
    return found


def span(node: ast.AST) -> tuple[int, int]:
    decorators = getattr(node, 'decorator_list', [])
    return min([node.lineno, *[d.lineno for d in decorators]]), node.end_lineno


def expand_symbol_edits(originals: dict[str, str], edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded = []
    indexes = {}
    for item in edits:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ('path', 'sha256', 'symbol', 'new')):
            raise ContractError('Each symbol edit needs string path, sha256, symbol and new')
        name = item['path']
        if name not in originals or item['sha256'] != hashlib.sha256(originals[name].encode()).hexdigest():
            raise ContractError(f'Stale or out-of-scope symbol edit: {name}')
        if not name.endswith('.py'):
            raise ContractError('Named symbol edits are supported only for listed Python source files')
        if name not in indexes:
            indexes[name] = symbols(originals[name])
        qualified = item['symbol']
        module_prefix = Path(name).stem + '.'
        # Python commonly names a module-qualified function main.load_inputs,
        # whereas our AST index is relative to main.py. Exact lexical scope
        # always wins; strip only this known file's module prefix, and only
        # when no such exact scope exists. Ambiguous bindings still fail.
        if qualified not in indexes[name] and qualified.startswith(module_prefix):
            qualified = qualified[len(module_prefix):]
        matches = indexes[name].get(qualified, [])
        if len(matches) != 1:
            raise ContractError(f'Python symbol {item["symbol"]!r} must identify exactly one original construct in {name}')
        node = matches[0]
        replacement = textwrap.dedent(item['new']).strip('\r\n')
        try:
            body = ast.parse(replacement).body
        except SyntaxError as exc:
            raise ContractError(f'Invalid replacement for Python symbol {item["symbol"]}: {exc}') from exc
        if len(body) != 1 or _binding(body[0]) != _binding(node):
            raise ContractError(f'Replacement must contain exactly the same named function/class/assignment: {item["symbol"]}')
        start, end = span(node)
        lines = originals[name].splitlines(keepends=True)
        indentation = lines[start-1][:len(lines[start-1]) - len(lines[start-1].lstrip(' \t'))]
        newline = '\r\n' if lines[end-1].endswith('\r\n') else '\n'
        new = newline.join(indentation + line if line else '' for line in replacement.splitlines()) + newline
        expanded.append({'path': name, 'sha256': item['sha256'], 'start_line': start, 'end_line': end, 'new': new})
    return expanded


def symbol_packet(originals: dict[str, str], visible: dict[str, list[tuple[int, int]]], limit: int = 12000) -> str:
    parts = ['\nUnambiguous Python symbols with complete original source visible (construct boundaries are controller-owned). '
             'Names are relative to the listed file; an optional current-file module prefix is accepted only '
             'when no exact lexical scope matches:\n']
    for name, text in originals.items():
        if not name.endswith('.py'):
            continue
        lines = text.splitlines(keepends=True)
        for qualified, nodes in symbols(text).items():
            if len(nodes) != 1:
                continue
            node = nodes[0]
            start, end = span(node)
            if (not any(a <= start <= end <= b for a, b in visible.get(name, []))
                    or len(''.join(lines[start-1:end])) > 12000):
                continue
            kind = _binding(node)[1]
            if kind == 'assignment' and end-start < 2:
                continue
            line = f'{name}: {qualified} ({kind}, original lines {start}-{end})\n'
            if sum(map(len, parts)) + len(line) > limit:
                return ''.join(parts) + '[Further symbols omitted; use only displayed original source.]\n'
            parts.append(line)
    return ''.join(parts)
