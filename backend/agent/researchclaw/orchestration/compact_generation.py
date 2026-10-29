"""Complete artifact generation without a mandatory per-file agent conversation."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import project_document, read_json, write_json
from researchclaw.orchestration.compact_contracts import ContractError


def validate_files(value: dict[str, Any]) -> None:
    files = value.get("files")
    if not isinstance(files, list) or not files or len(files) > 20:
        raise ContractError("Return one complete files list with at most 20 source files")
    names = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            raise ContractError("Each file needs a string path and content")
        path = Path(item["path"])
        if path.is_absolute() or any(part.startswith(".") or part in {"outputs", "data", "related_work"} for part in path.parts):
            raise ContractError(f"Invalid generated source path: {path}")
        if str(path) in names or not item["content"].strip():
            raise ContractError("Empty or duplicate generated file")
        names.add(str(path))
        if path.suffix == ".py":
            try:
                ast.parse(item["content"])
            except SyntaxError as exc:
                raise ContractError(f"Invalid Python in {path}: {exc}") from exc
    if "main.py" not in names:
        raise ContractError("Generated source bundle must include main.py")


def source_files(work: Path) -> list[Path]:
    manifest = work / '.source_manifest.json'
    if manifest.is_file():
        names = read_json(manifest).get('files')
        if not isinstance(names, list) or 'main.py' not in names:
            raise ContractError('Invalid implementation source manifest')
        paths = []
        for name in names:
            if not isinstance(name, str):
                raise ContractError('Source manifest paths must be strings')
            relative = Path(name)
            path = work / relative
            if (relative.is_absolute() or any(part.startswith('.') or part in {'data', 'outputs', 'related_work'} for part in relative.parts)
                    or path.is_symlink() or not path.resolve().is_relative_to(work.resolve())):
                raise ContractError(f'Invalid implementation source path: {name}')
            if not path.is_file():
                raise ContractError(f'Declared implementation source is missing: {name}')
            paths.append(path)
        return sorted(set(paths)) + [manifest]
    # Compatibility for captured old runs: generated reports and binary figures
    # are not authored source. New runs use the explicit bundle manifest above.
    return [path for path in sorted(work.rglob('*')) if path.is_file() and not path.is_symlink()
            and path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.gif', '.pdf', '.npy', '.npz', '.pyc'}
            and not any(part.startswith('.') or part in {'data', 'outputs', '__pycache__', 'report', 'reports',
                                                        'images', 'figures', 'plots', 'charts', 'scratch'}
                        for part in path.relative_to(work).parts)]


def record_sources(work: Path, added: list[str]) -> None:
    names = {path.relative_to(work).as_posix() for path in source_files(work) if path.name != '.source_manifest.json'}
    names.update(added)
    write_json(work / '.source_manifest.json', {'schema_version': 'implementation-source-v1', 'files': sorted(names)})
    source_files(work)  # Validate ownership immediately, before any execution.


def source_texts(work: Path) -> dict[str, str]:
    texts = {}
    for path in source_files(work):
        if path.name == '.source_manifest.json':
            continue
        try:
            texts[path.relative_to(work).as_posix()] = path.read_bytes().decode('utf-8')
        except UnicodeDecodeError:
            continue  # Binary assets can be retained and hashed, never sent as text edits.
    return texts


def source_packet(work: Path, limit: int = 36000) -> str:
    files = [path for path in source_files(work) if path.suffix == ".py"]
    parts = []
    remaining = limit
    for index, path in enumerate(files):
        if remaining < 400:
            break
        reserve = min(1500, remaining // max(1, len(files) - index)) * (len(files) - index - 1)
        block = f"\n### {path.relative_to(work)}\n" + project_document(path, max(300, remaining - reserve - 100))
        parts.append(block)
        remaining -= len(block)
    return "".join(parts)


def scientific_excerpt(text: str, query: str, limit: int = 8000) -> str:
    """Keep source identity and relevant methods/proofs, with exact source offsets."""
    if len(text) <= limit:
        return text
    terms = set(re.findall(r"[a-z][a-z0-9-]{3,}", query.casefold())) - {
        "with", "from", "that", "this", "research", "report", "using", "task", "data", "must", "have"}
    terms |= {"theorem", "lyapunov", "algorithm", "convergence", "assumption", "baseline", "protocol"}
    acronyms = {word.casefold() for word in re.findall(r'\b[A-Z][A-Za-z0-9]{1,11}\b', query)
                if sum(char.isupper() for char in word) >= 2}
    terms |= acronyms
    frequencies = Counter(re.findall(r'[a-z0-9]+', text.casefold()))
    lead_size = min(700, max(100, limit // 4))
    room = max(200, limit - lead_size - 250)
    count = max(1, min(4, room // 1500))
    width = max(200, room // count - 60)
    chunks = [(offset, text[offset:offset + width]) for offset in range(lead_size, len(text), max(200, width * 3 // 4))]
    def relevance(row: tuple[int, str]) -> float:
        counts = Counter(re.findall(r'[a-z0-9]+', row[1].casefold()))
        return sum(min(counts[term], 5) * (4 if term in acronyms else 1)
                   / max(1, frequencies[term])**0.5 for term in terms)
    ranked = sorted(chunks, key=relevance, reverse=True)
    chosen = [(0, text[:lead_size])]
    for row in ranked:
        if all(abs(row[0] - previous[0]) >= width * 0.7 for previous in chosen[1:]):
            chosen.append(row)
        if len(chosen) > count:
            break
    return ("[PARTIAL source: selected original excerpts, not a generated summary. Omitted text is not evidence of absence.]\n"
            + "\n".join(f"[source characters {offset}:{offset + len(chunk)}]\n{chunk}" for offset, chunk in sorted(chosen)))[:limit]


def reference_packet(value: dict[str, Any], limit: int = 20000, *, query: str = '',
                     audit: list[dict[str, Any]] | None = None) -> str:
    from researchclaw.orchestration.bibliography import deduplicate_references
    references = deduplicate_references(list(value.get("references", [])))
    for row in list(references):
        code = row.get("public_code", {})
        if code.get("packet_path") and code.get("status") in {"available", "partial"}:
            references.append({"source": code.get("repository_url", "") + "/tree/" + code.get("commit_sha", ""),
                "text_path": code["packet_path"], "context": code.get("source_scope", ""),
                "source_scope": str(code.get("source_scope", "")) + " Public author code is source evidence, not executed experimental evidence.",
                "named_framework_matches": row.get("named_framework_matches", []),
                "task_identity_matches": row.get("task_identity_matches", []),
                "origin": "paper_linked_author_code", "code_evidence": code})
    if not references:
        return "No supplied sources. Do not invent citations."
    rows = []
    for row in references:
        text = str(row.get('context', row.get('status', 'unavailable')))
        path = Path(row['text_path']) if isinstance(row.get('text_path'), str) else None
        if (query and path is not None and path.suffix.lower() in {'.md', '.txt'} and not path.is_symlink()
                and not set(path.resolve().parts) & {'target_study', 'evaluation', 'private', 'secrets'}):
            try:
                if path.stat().st_size <= 8 * 1024 * 1024:
                    text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                pass  # Keep the explicitly partial cached context if full text is unavailable.
        extraction = row.get('extraction', {})
        notes = {key: extraction[key] for key in ('reader', 'usable_text', 'complete_page_coverage', 'precision_note') if key in extraction}
        if extraction.get('unreadable_pages'):
            notes['unreadable_page_count'] = len(extraction['unreadable_pages'])
        header = f"### {row.get('source')}\nCached source text: {row.get('text_path', 'unavailable')}\n"
        if notes or row.get('source_scope'):
            header += 'Reader scope: ' + json.dumps(notes or row['source_scope'], ensure_ascii=False) + '\n'
        if row.get("alias_sources"):
            header += "Other URLs for this same source: " + json.dumps(row["alias_sources"], ensure_ascii=False) + "\n"
        code_urls = row.get('public_code_urls')
        if isinstance(code_urls, list) and code_urls:
            label = ('Public code links (acquisition status: ' + row["public_code"].get("status", "unavailable") + '): '
                     if row.get("public_code") else 'Public code links (not fetched): ')
            header += label + json.dumps(code_urls[:8], ensure_ascii=False) + '\n'
        weight = max(2, len(references)-1) if query and (row.get('named_framework_matches') or row.get('task_identity_matches')) else 1
        rows.append((header, text, weight))
    intro = ('Each source is independently represented. Full cached text is preferred for named methods when it fits; '
             'cached coverage is not a certification of full-paper acquisition. Omitted text is not evidence of absence.\n')
    budget = max(0, limit - len(intro) - sum(len(header)+1 for header, _, _ in rows))
    remaining = budget
    allocations = [0] * len(rows)
    # Keep the complete set of focal methods when it fits with a small,
    # independently identified excerpt of every background source. A fixed
    # 1,500-character reservation per background paper otherwise creates a
    # cliff: a nearly fitting focal source loses half its method text.
    # Consider focal sources together so ordering cannot starve a second one.
    focal = [index for index, (_, _, weight) in enumerate(rows) if weight > 1]
    background = [index for index in range(len(rows)) if index not in focal]
    if focal:
        for index in background:
            allocations[index] = min(256, len(rows[index][1]), remaining)
            remaining -= allocations[index]
    prepared_code = [i for i, row in enumerate(references) if row.get("origin") == "paper_linked_author_code"]
    # A prepared code view contains coherent original statements and their
    # prerequisites. Keep it intact when it fits; never text-slice those
    # statements again merely because several publisher aliases filled space.
    for i in prepared_code:
        reserve = sum(min(1000, len(rows[j][1])) for j in focal if j != i)
        amount = len(rows[i][1]) if len(rows[i][1]) <= remaining - reserve else remaining // 2
        allocations[i] = max(0, min(amount, remaining))
        remaining -= allocations[i]
    def distribute(indices: list[int], room: int) -> int:
        active = {i for i in indices if allocations[i] < len(rows[i][1])}
        while active and room:
            total_weight = sum(rows[i][2] for i in active)
            fitting = [i for i in sorted(active)
                       if len(rows[i][1]) - allocations[i] <= room * rows[i][2] // total_weight]
            if fitting:
                for i in fitting:
                    amount = len(rows[i][1]) - allocations[i]
                    allocations[i] += amount
                    room -= amount
                    active.remove(i)
            else:
                ordered = sorted(active)
                amounts = {i: room * rows[i][2] // total_weight for i in ordered[:-1]}
                amounts[ordered[-1]] = room - sum(amounts.values())
                for i, amount in amounts.items():
                    allocations[i] += amount
                return 0
        return room
    remaining = distribute([i for i in focal if i not in prepared_code], remaining)
    distribute(background, remaining)
    parts = []
    for index, (header, text, _) in enumerate(rows):
        if references[index].get("origin") == "paper_linked_author_code" and len(text) > allocations[index]:
            evidence = references[index].get("code_evidence", {})
            if evidence.get("files"):
                from researchclaw.orchestration.public_code import render_code_context
                fragment = render_code_context(evidence, query, allocations[index])
            else:
                fragment = "[PARTIAL: original code files unavailable for coherent repacking; code statements omitted.]"[:allocations[index]]
        else:
            fragment = scientific_excerpt(text, query, allocations[index]) if allocations[index] else ''
        parts.append(header + fragment)
        if audit is not None:
            audit.append({'source': references[index].get('source'), 'cached_chars': len(text),
                          'cached_text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                          'allocated_chars': allocations[index], 'included_chars': len(fragment),
                          'full_cached_text': fragment == text})
    return (intro + '\n'.join(parts))[:limit]


def canonical_review_references(value: dict[str, Any], root: Path, images: list[str]) -> None:
    """Resolve known context labels and sections; never turn syntax into a science gap."""
    aliases = {"COMPLETE report": "stage-25/paper_final.md", "report": "stage-25/paper_final.md",
               "Executed measurements": "stage-11/experiment/outputs/metrics.json",
               "Analysis and derivations": "stage-11/experiment/outputs/analysis.md",
               "Actual main.py": "stage-11/experiment/main.py", "Protocol": "stage-09/exp_plan.yaml"}
    for row in value.get("criteria", []):
        if row.get("status") != "met":
            continue
        normalized, unresolved = [], []
        for raw in row.get("evidence", []):
            text = raw.get("path", "") if isinstance(raw, dict) else raw
            if not isinstance(text, str):
                unresolved.append(str(raw))
                continue
            text = re.split(r"\s+§|#", text.strip().strip("`"), maxsplit=1)[0]
            if text in {"artifact audit images", "Artifact audit images"}:
                normalized.extend(images)
                continue
            text = aliases.get(text, text)
            path = (root / text).resolve()
            if path.is_relative_to(root.resolve()) and path.is_file():
                normalized.append(path.relative_to(root).as_posix())
            else:
                unresolved.append(text)
        if not normalized:
            raise ContractError(f"Evidence citation format for {row.get('id')} has no valid file. "
                                "Use exact relative artifact paths; put section names in reason, not evidence. "
                                "Preserve the scientific judgment; repair reference syntax only.")
        row["evidence"] = list(dict.fromkeys(normalized))
        if unresolved:
            row["unresolved_reference_labels"] = unresolved


def validate_report(value: dict[str, Any]) -> None:
    text = value.get("report_markdown")
    if not isinstance(text, str) or len(text.strip()) < 300:
        raise ContractError("Return report_markdown containing the complete scientific report")
    if not re.search(r"!\[[^\]]*\]\([^)]+\)", text):
        raise ContractError("Report must embed the supplied actual figure paths")


def bind_report_images(text: str, report: Path, figures: list[Path]) -> str:
    """Resolve export aliases only to unique, real figures from this execution."""
    available = {path.resolve() for path in figures if path.is_file() and path.suffix.lower() == '.png'
                 and path.resolve().is_relative_to(report.parent.parent.resolve())}
    def bind(match: re.Match[str]) -> str:
        raw = match.group(2).strip()
        target, separator, title = raw.partition(' "')
        target = target.strip().strip('<>')
        if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or Path(target).is_absolute():
            raise ContractError('Report figures must use the listed executed local PNGs')
        direct = (report.parent / target).resolve()
        if direct in available:
            path = direct
        else:
            alias = target.removeprefix('./')
            for prefix in ('report/images/', 'images/', 'outputs/', 'figures/'):
                if alias.startswith(prefix):
                    alias = alias[len(prefix):]
                    break
            if '..' in Path(alias).parts:
                raise ContractError(f'Unresolvable report image: {target[:180]}')
            matches = [path for path in available if path.as_posix().endswith('/' + alias)]
            if not matches:
                def structural_name(name: str) -> str:
                    stem = Path(name).stem.casefold()
                    stem = re.sub(r'^(?:fig(?:ure)?|plot)[ _-]*(?:\d+[ _-]*)?', '', stem)
                    return re.sub(r'^\d+[ _-]+', '', stem)
                # Only naming prefixes are normalized; no semantic/fuzzy image matching.
                name = structural_name(alias)
                if name and len(Path(alias).parts) == 1:
                    matches = [path for path in available if structural_name(path.name) == name]
            if len(matches) != 1:
                raise ContractError(f'Report image must identify one actual executed PNG: {target[:180]} ({len(matches)} matches)')
            path = matches[0]
        relative = Path(os.path.relpath(path, report.parent)).as_posix()
        if any(char.isspace() for char in relative):
            relative = '<' + relative + '>'
        return f'![{match.group(1)}]({relative}' + (separator + title if separator else '') + ')'
    return re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', bind, text)
