"""Read-only checks of task-required coverage and measurements before reporting.

Checks belong to the reviewed plan, outside the generated source's repair scope.
They establish necessary output conditions, never scientific validity or a score.
"""
from __future__ import annotations

import csv
import json
import math
import zipfile
from pathlib import Path
from typing import Any

from researchclaw.orchestration.compact_contracts import ContractError


COMMON = {'path', 'requirement_id', 'notes'}
FILES = {'nonempty', 'min_size_bytes'}
TABLE = {'min_rows', 'min_non_null', 'allowed_values'}
METRIC_LISTS = {'finite_fields', 'positive_fields'}
METRICS = METRIC_LISTS | {'numeric_bounds'}
ARRAYS = {'required_arrays', 'nonempty_arrays', 'finite_arrays'}
IMAGES = {'image_format'}
MAX_CHECKS = 64


def predicates_for(path: Path) -> set[str]:
    return FILES | ({'.csv': TABLE, '.tsv': TABLE, '.json': METRICS,
                     '.npy': ARRAYS, '.npz': ARRAYS, '.png': IMAGES,
                     '.jpg': IMAGES, '.jpeg': IMAGES, '.tif': IMAGES, '.tiff': IMAGES}.get(path.suffix.lower(), set()))


def schema_hint() -> str:
    """The same vocabulary is supplied to generation, criticism and repair."""
    return ('\nController execution_checks contract: at most 64 read-only output checks, each with path '
            '(relative outputs/ file) and an existing requirement_id. Optional notes is a descriptive string, '
            'not an evaluated predicate, and never replaces an actual condition. Every file supports nonempty (boolean) '
            'and min_size_bytes (positive integer). CSV/TSV additionally support min_rows, min_non_null '
            '(column to positive count), allowed_values (column to exact string labels). JSON supports '
            'finite_fields and positive_fields (lists of JSON pointers to measured scalar numbers, '
            'never metadata objects or arrays), plus numeric_bounds '
            '(JSON pointer to an object with inclusive min and/or max numbers, for example '
            '{"/relative_residual":{"min":0,"max":1e-6}}). Bounds must come from the task or a justified '
            'method invariant, never a published answer or desired benchmark score. Finite alone does not '
            'enforce a tolerance, conservation law or required count. NPY/NPZ support required_arrays '
            '(list of exact array names), finite_arrays and nonempty_arrays (true, "all", or name lists); '
            'a plain NPY array may be named "array" or its filename stem. Images support image_format '
            '(e.g. "PNG"). These checks run before report generation and final export: do not check '
            'report/report.md or report/images here; keep report/figure delivery obligations in requirement_map '
            'for the later controller audit. Runtime figures use outputs/*.png. '
            'A file/shape/finite check never establishes a theorem or scientific correctness. '
            'Use only these predicate names. Put complex conditional/domain assertions in the protocol/source '
            'and keep them for independent scientific review. Do not silently delete an obligation because '
            'the controller has no predicate for it.\n')


def validate_checks(checks: Any, requirement_ids: set[str] | None = None) -> None:
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ContractError('execution_checks must be a list of at most 64 checks')
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('path'), str):
            raise ContractError('Each execution check needs a path and requirement_id')
        path = Path(check['path'])
        if (path.is_absolute() or len(path.parts) < 2 or path.parts[0] != 'outputs'
                or any(part.startswith('.') for part in path.parts)):
            raise ContractError(f'Execution check path {check["path"]!r} must be relative outputs/ files. '
                                'These checks run before report/export; preserve later delivery obligations '
                                'in requirement_map, and use outputs/*.png for runtime figures.')
        rid = check.get('requirement_id')
        if not isinstance(rid, str) or not rid or (requirement_ids is not None and rid not in requirement_ids):
            raise ContractError('Execution checks must refer to an existing requirement_id')
        predicates = predicates_for(path)
        unknown = set(check) - COMMON - predicates
        if unknown:
            raise ContractError(f'Unsupported execution check predicates for {path}: {sorted(unknown)}. '
                                f'Allowed keys: {sorted(COMMON | predicates)}. '
                                'Preserve complex conditional obligations in protocol/source assertions, not invented check keys.')
        if not (set(check) & predicates):
            raise ContractError('Execution check needs at least one output predicate')
        if 'notes' in check and (not isinstance(check['notes'], str) or len(check['notes']) > 2000):
            raise ContractError('Execution check notes must be a descriptive string of at most 2000 characters')
        if 'nonempty' in check and type(check['nonempty']) is not bool:
            raise ContractError('nonempty must be a boolean')
        if 'min_size_bytes' in check and (type(check['min_size_bytes']) is not int or check['min_size_bytes'] < 1):
            raise ContractError('min_size_bytes must be a positive integer')
        if 'image_format' in check and (not isinstance(check['image_format'], str) or not check['image_format'].strip()):
            raise ContractError('image_format must be a nonempty format name such as PNG')
        for field in ARRAYS:
            if field not in check:
                continue
            value = check[field]
            if field != 'required_arrays' and (value is True or value == 'all'):
                continue
            if not isinstance(value, list) or not 0 < len(value) <= 100 or any(not isinstance(n, str) or not n for n in value):
                raise ContractError(f'{field} needs exact array names; finite/nonempty arrays also accept true or "all"')
        if 'min_rows' in check and (type(check['min_rows']) is not int or check['min_rows'] < 1):
            raise ContractError('min_rows must be a positive integer')
        for field in ('min_non_null', 'allowed_values'):
            if field not in check:
                continue
            values = check[field]
            if not isinstance(values, dict) or not values or len(values) > 40:
                raise ContractError(f'{field} must map column names to constraints')
            for column, value in values.items():
                if not isinstance(column, str) or not column:
                    raise ContractError('Execution check column names must be nonempty strings')
                if field == 'min_non_null' and (type(value) is not int or value < 1):
                    raise ContractError('min_non_null counts must be positive integers')
                if field == 'allowed_values' and (not isinstance(value, list) or not 0 < len(value) <= 100
                                                  or any(not isinstance(item, str) for item in value)):
                    raise ContractError('allowed_values must contain bounded lists of exact string labels')
        for field in METRIC_LISTS:
            if field in check and (not isinstance(check[field], list) or not 0 < len(check[field]) <= 40
                    or any(not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 512
                           for pointer in check[field])):
                raise ContractError(f'{field} must contain JSON pointers such as /global_mean')
        if 'numeric_bounds' in check:
            bounds = check['numeric_bounds']
            if not isinstance(bounds, dict) or not 0 < len(bounds) <= 40:
                raise ContractError('numeric_bounds must map at most 40 JSON pointers to min/max constraints')
            for pointer, bound in bounds.items():
                if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 512:
                    raise ContractError('numeric_bounds keys must be JSON pointers such as /relative_residual')
                if not isinstance(bound, dict) or not bound or set(bound) - {'min', 'max'}:
                    raise ContractError('numeric_bounds needs inclusive min and/or max constraints only')
                if any(not _finite_number(number) for number in bound.values()):
                    raise ContractError('numeric_bounds limits must be finite numbers, not booleans or strings')
                if 'min' in bound and 'max' in bound and bound['min'] > bound['max']:
                    raise ContractError('numeric_bounds min cannot exceed max')


def _finite_number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _present(value: Any) -> bool:
    return value is not None and str(value).strip().casefold() not in {'', 'nan', 'none', 'null', '<na>'}


def _pointer(value: Any, pointer: str) -> Any:
    try:
        for token in pointer[1:].split('/'):
            token = token.replace('~1', '/').replace('~0', '~')
            value = value[int(token)] if isinstance(value, list) else value[token]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _table(path: Path, check: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    required = set(check.get('min_non_null', {})) | set(check.get('allowed_values', {}))
    counts = {column: 0 for column in check.get('min_non_null', {})}
    invalid: dict[str, list[str]] = {}
    n = 0
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')
        missing = required - set(reader.fieldnames or [])
        if missing:
            errors.append(f'Missing columns: {sorted(missing)}')
        for row in reader:
            n += 1
            if n > 1_000_000:
                errors.append('Table check exceeds the bounded 1,000,000-row scan; result unverified')
                break
            for column in counts:
                counts[column] += _present(row.get(column))
            for column, allowed in check.get('allowed_values', {}).items():
                value = row.get(column)
                if _present(value) and value not in allowed:
                    examples = invalid.setdefault(column, [])
                    if value not in examples and len(examples) < 5:
                        examples.append(value)
    if n < check.get('min_rows', 0):
        errors.append(f'Table has {n} rows; requires at least {check["min_rows"]}')
    for column, minimum in check.get('min_non_null', {}).items():
        if counts[column] < minimum:
            errors.append(f'{column} has {counts[column]} non-null values; requires at least {minimum}')
    if invalid:
        errors.append(f'Unsupported labels: {invalid}')
    return {'rows_seen': n, 'non_null_counts': counts, 'unsupported_labels': invalid}


def evaluate_checks(work: Path, checks: list[dict[str, Any]]) -> dict[str, Any]:
    validate_checks(checks)
    results = []
    output_root = work.resolve() / 'outputs'
    for check in checks:
        errors: list[str] = []
        row: dict[str, Any] = {'path': check['path'], 'requirement_id': check['requirement_id']}
        path = work / check['path']
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(output_root):
                raise ValueError('Checked output escapes the real execution output directory')
            if not path.is_file():
                raise ValueError('Checked output is not a regular file')
            if path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError('Output check exceeds the bounded 64-MiB read; result unverified')
            size = path.stat().st_size
            if check.get('nonempty') and size == 0:
                errors.append('Required output file is empty')
            if size < check.get('min_size_bytes', 0):
                errors.append(f'File has {size} bytes; requires at least {check["min_size_bytes"]}')
            if path.suffix.lower() in {'.csv', '.tsv'}:
                row.update(_table(path, check, errors))
            elif path.suffix.lower() == '.json':
                value = json.loads(path.read_text(encoding='utf-8'))
                observed = {}
                for field in METRIC_LISTS:
                    for pointer in check.get(field, []):
                        item = _pointer(value, pointer)
                        finite = type(item) in (int, float) and math.isfinite(item)
                        observed[pointer] = item if finite else None
                        if not finite or (field == 'positive_fields' and item <= 0):
                            errors.append(f'{pointer} must be a {"positive" if field == "positive_fields" else "finite"} measured number; '
                                          f'observed {str(item)[:80]}')
                for pointer, bound in check.get('numeric_bounds', {}).items():
                    item = _pointer(value, pointer)
                    finite = _finite_number(item)
                    observed[pointer] = item if finite else None
                    if (not finite or ('min' in bound and item < bound['min'])
                            or ('max' in bound and item > bound['max'])):
                        errors.append(f'{pointer} must be a measured number within inclusive bounds '
                                      f'{bound}; observed {str(item)[:80]}')
                if check.get('numeric_bounds'):
                    row['numeric_bounds'] = check['numeric_bounds']
                row['observed_fields'] = observed
            elif path.suffix.lower() in {'.npy', '.npz'}:
                from researchclaw.orchestration.artifact_predicates import array_checks
                row.update(array_checks(path, check, errors))
            elif path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.tif', '.tiff'}:
                from researchclaw.orchestration.artifact_predicates import image_checks
                row.update(image_checks(path, check, errors))
        except (OSError, ValueError, UnicodeError, csv.Error, OverflowError, EOFError, zipfile.BadZipFile) as exc:
            errors.append(str(exc))
        row.update(passed=not errors, errors=errors)
        results.append(row)
    return {'passed': all(row['passed'] for row in results), 'checks': results,
            'scientific_validity': 'requires_independent_review'}
