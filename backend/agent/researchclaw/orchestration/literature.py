"""Bounded source-text recovery and task-derived named scientific concepts."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any


def named_frameworks(text: str) -> list[tuple[str, str]]:
    pattern = (r'\b((?:[A-Z][A-Za-z0-9-]*(?:\s+(?:and|of|the|for|in|on|to))?\s+){1,7}'
               r'[A-Z][A-Za-z0-9-]*)\s*\(([A-Z][A-Za-z0-9]{1,11})\)')
    names = []
    connectors = {'and', 'of', 'the', 'for', 'in', 'on', 'to'}
    for name, acronym in re.findall(pattern, text):
        capitals = ''.join(char for char in acronym if char.isupper())
        if len(capitals) < 2:
            continue
        words = name.split()
        # A sentence-leading verb can also be capitalized. Use the supplied
        # acronym to identify the expansion's boundary, rather than searching
        # for an accidental phrase such as "Develop Variable ...".
        for start in range(len(words)):
            suffix = words[start:]
            initials = ''.join(word[0].upper() for word in suffix if word.lower() not in connectors)
            if initials == capitals and suffix[0].lower() not in connectors:
                name = ' '.join(suffix)
                break
        names.append((name, acronym))
    return list(dict.fromkeys(names))[:3]


def source_queries(topic: str, proposed: list[str], limit: int) -> list[str]:
    if not proposed:
        return []  # Preserve self-contained / no-external-source decisions.
    names = named_frameworks(topic)
    def already_specific(name: str) -> bool:
        words = set(re.findall(r'[a-z0-9]+', name.casefold())) - {'and', 'of', 'the', 'for', 'in', 'on', 'to'}
        return any(words.issubset(set(re.findall(r'[a-z0-9]+', query.casefold()))) for query in proposed)
    exact = [f'"{name}" {acronym}' for name, acronym in names if len(name) < 88 and not already_specific(name)]
    result = []
    for query in [*exact, *proposed]:
        if not any(query.casefold() == old.casefold() for old in result):
            result.append(query)
    return result[:limit]


def extract_pdf(source: Path, cache: Path, *, max_ocr_pages: int = 12,
                ocr_budget_sec: float = 120) -> tuple[str, dict[str, Any]]:
    import fitz
    tool = shutil.which('tesseract')
    cache.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {'reader': 'pymupdf', 'ocr_engine': tool,
                                'ocr_language': 'eng', 'pages': [], 'ocr_pages': [],
                                'unreadable_pages': [], 'limits': {'ocr_pages': max_ocr_pages,
                                                                 'ocr_seconds': ocr_budget_sec}}
    started = time.monotonic()
    env = {key: value for key, value in os.environ.items()
           if not any(part in key.upper() for part in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'AUTH'))}
    env['OMP_THREAD_LIMIT'] = '1'
    with fitz.open(source) as document, tempfile.TemporaryDirectory(prefix='source-ocr-', dir=cache) as temp:
        metadata['page_count'] = len(document)
        parts, meaningful = [], 0
        for number, page in enumerate(document):
            text = page.get_text()
            method = 'pdf_text'
            row = {'page': number + 1, 'selectable_chars': len(text.strip())}
            if len(text.strip()) < 80 and page.get_images():
                remaining = ocr_budget_sec - (time.monotonic() - started)
                if tool and len(metadata['ocr_pages']) < max_ocr_pages and remaining > 0:
                    image = Path(temp) / f'page-{number+1:04d}.png'
                    scale = min(2.25, 2400 / max(page.rect.width, page.rect.height))
                    page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(image)
                    metadata['ocr_pages'].append(number+1)
                    try:
                        outcome = subprocess.run([tool, str(image), 'stdout', '-l', 'eng', '--psm', '3'],
                            capture_output=True, text=True, timeout=min(30, remaining), env=env)
                        if outcome.returncode == 0 and len(outcome.stdout.strip()) > len(text.strip()):
                            text = outcome.stdout
                            method = 'local_ocr'
                        else:
                            row['ocr_status'] = f'failed_or_empty:{outcome.returncode}'
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        row['ocr_status'] = type(exc).__name__
                else:
                    row['ocr_status'] = 'tool_unavailable_or_budget_exhausted'
            if len(text.strip()) < 80:
                metadata['unreadable_pages'].append(number+1)
            row.update(method=method, recovered_chars=len(text.strip()))
            metadata['pages'].append(row)
            meaningful += len(text.strip())
            parts.append(f'\n[Original PDF page {number+1}; {method}]\n' + text)
        metadata['meaningful_chars'] = meaningful
        metadata['usable_text'] = meaningful >= 120
        metadata['complete_page_coverage'] = not metadata['unreadable_pages']
        if metadata['ocr_pages']:
            metadata['reader'] = 'pymupdf+local_tesseract'
            metadata['precision_note'] = 'OCR may misread mathematical symbols; this text is not an algebraic or proof-checker certificate.'
        metadata['duration_sec'] = round(time.monotonic() - started, 3)
        return '\n'.join(parts), metadata
