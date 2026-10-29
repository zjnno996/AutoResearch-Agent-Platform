"""Read current research artifacts without consulting archived attempts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def parse_object(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def recover_object_members(text: str) -> dict[str, Any]:
    """Repair one premature root brace; retain and parse every trailing member.

    Never discard trailers, merge multiple objects, or accept incomplete JSON.
    The normal schema validator and provider truncation check still apply.
    """
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    try:
        value, end = json.JSONDecoder().raw_decode(text)
        if not isinstance(value, dict) or not text[end:].lstrip().startswith(',') or text[end-1] != '}':
            return {}
        repaired = json.loads(text[:end-1] + text[end:])
        return repaired if isinstance(repaired, dict) else {}
    except (ValueError, TypeError):
        return {}


def recover_string_whitespace(text: str) -> dict[str, Any]:
    """Escape literal LF/CR/TAB inside complete JSON strings, changing no value.

    Never infer a delimiter, drop a field, repair an unknown escape or accept
    other raw controls. Callers must still reject provider-truncated output.
    """
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    result, quoted, escaped, changed = [], False, False, False
    replacements = {'\n': r'\n', '\r': r'\r', '\t': r'\t'}
    for char in raw:
        if escaped:
            if ord(char) < 32:
                return {}
            result.append(char)
            escaped = False
        elif char == '"':
            quoted = not quoted
            result.append(char)
        elif quoted and char == '\\':
            escaped = True
            result.append(char)
        elif quoted and char in replacements:
            result.append(replacements[char])
            changed = True
        elif ord(char) < 32 and char not in replacements:
            return {}
        else:
            result.append(char)
    if quoted or escaped or not changed:
        return {}
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate JSON field')
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError('Nonfinite JSON constant')
    try:
        value = json.loads(''.join(result), object_pairs_hook=unique, parse_constant=invalid_constant)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def recover_missing_root_brace(text: str) -> dict[str, Any]:
    """Close exactly one omitted object delimiter on a provider-complete response.

    Callers must first reject provider truncation. No values, trailers or
    scientific statements may be removed; the full original schema still applies.
    """
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    if not text.startswith('{'):
        return {}
    try:
        json.loads(text)
        return {}
    except json.JSONDecodeError as exc:
        if exc.pos != len(text):
            return {}
    try:
        value = json.loads(text + '}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def recover_plan_mapping_boundary(text: str) -> dict[str, Any]:
    """Restore one requirement-map delimiter before declared top-level fields.

    Only insert one closing brace. Do not move or rewrite any original value,
    overwrite a field, or infer missing scientific content. The complete plan
    and actual requirement IDs must still pass their caller's validator.
    """
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    closed = recover_missing_root_brace(raw)
    mapping = closed.get('requirement_map')
    if not isinstance(mapping, dict) or not all(key in closed for key in ('methods', 'datasets', 'metrics', 'protocol')):
        return {}
    reserved = {'scientific_analysis_markdown', 'risks'} & mapping.keys()
    if not reserved or any(key in closed for key in reserved):
        return {}
    remaining = {key: value for key, value in mapping.items() if key not in reserved}
    if not remaining or any(not isinstance(row, dict) or not {'approach', 'artifact'}.issubset(row) for row in remaining.values()):
        return {}
    candidates = []
    for match in re.finditer(r',(?=\s*"(?:scientific_analysis_markdown|risks)"\s*:)', raw):
        try:
            value = json.loads(raw[:match.start()] + '}' + raw[match.start():])
            if (all(key in value and value[key] == mapping[key] for key in reserved)
                    and value.get('requirement_map') == remaining):
                candidates.append(value)
        except (ValueError, TypeError):
            pass
    return candidates[0] if len(candidates) == 1 else {}


class Artifacts:
    def __init__(self, root: Path, through_stage: int = 26) -> None:
        self.root = root.resolve()
        self.through_stage = through_stage

    def path(self, relative: str) -> Path | None:
        """Only active stage artifacts are eligible scientific evidence."""
        match = re.match(r"^stage-(\d{2})/", relative)
        if not match or not 1 <= int(match[1]) <= self.through_stage:
            return None
        if ".." in Path(relative).parts:
            return None
        path = (self.root / relative).resolve()
        stage_root = self.root / Path(relative).parts[0]
        if stage_root.is_symlink() or not path.is_relative_to(stage_root):
            return None
        return path

    def exists(self, relative: str) -> bool:
        path = self.path(relative)
        if path is None:
            return False
        if path.is_dir():
            return any(p.is_file() and p.stat().st_size for p in path.rglob("*")
                       if p.resolve().is_relative_to(self.root))
        return path.is_file() and path.stat().st_size > 0

    def text(self, relative: str, limit: int = 16000) -> str:
        path = self.path(relative)
        if path is None or not path.is_file():
            return ""
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        except OSError:
            return ""

    def json(self, relative: str) -> dict[str, Any]:
        path = self.path(relative)
        return read_json(path) if path else {}

    def latest(self, name: str) -> str:
        for stage in range(self.through_stage, 0, -1):
            relative = f"stage-{stage:02d}/{name}"
            if self.exists(relative):
                return relative
        return ""

    def reference(self, relative: str) -> dict[str, Any]:
        path = self.path(relative)
        if path is None or not path.is_file():
            return {}
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return {"path": relative, "sha256": digest.hexdigest(), "bytes": path.stat().st_size}

    def packet(self, limit: int = 48000) -> str:
        """Include methods, raw summaries and audits before manuscript prose."""
        names = (
            "goal.md", "task_data_profile.json", "exp_plan.yaml", "experiment_provenance.json",
            "experiment_summary.json", "evaluation_protocol_audit.json",
            "research_readiness.json", "sanity_report.json",
            "final_claim_integrity_report.json", "claim_integrity_report.json",
            "verification_report.json", "analysis.md", "synthesis.md",
            "paper_final_verified.md", "paper_final.md", "paper_revised.md", "paper_draft.md",
        )
        parts: list[str] = []
        remaining = limit
        for name in names:
            relative = self.latest(name)
            if relative and remaining > 300:
                path = self.path(relative)
                text = project_document(path, min(12000, remaining - len(relative) - 180)) if path else ""
                block = f"\n### {relative}\n{text}\n"
                parts.append(block)
                remaining -= len(block)
        for relative_dir in ("stage-15/experiment_final", "stage-11/experiment"):
            directory = self.path(relative_dir)
            if directory and directory.is_dir():
                for file in sorted(directory.rglob("*.py"))[:6]:
                    relative = file.relative_to(self.root).as_posix()
                    if remaining < 300:
                        break
                    block = f"\n### {relative}\n{self.text(relative, min(4000, remaining - len(relative) - 20))}\n"
                    parts.append(block)
                    remaining -= len(block)
                break
        return "".join(parts)


def project_document(path: Path, limit: int = 12000) -> str:
    """Bounded review context that exposes every structured top-level field.

    A prefix of a sorted YAML plan can contain only ablations. Reviewers must
    distinguish an absent field from a field omitted by the context budget.
    The original file, byte length and digest remain the source of truth.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    import yaml
    try:
        value = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text) if path.suffix in {".yaml", ".yml"} else None
    except (ValueError, yaml.YAMLError):
        value = None
    header = (f"[PARTIAL CONTEXT: {len(text)} characters; sha256={hashlib.sha256(path.read_bytes()).hexdigest()}. "
              "Omitted content is NOT evidence of absence. Read the original before making an absence claim.]\n")
    if isinstance(value, dict) and value:
        keys = list(value)
        inventory = "Top-level fields present: " + ", ".join(map(str, keys)) + "\n"
        budget = max(0, limit - len(header) - len(inventory))
        share = max(0, budget // len(keys) - 60)
        blocks = []
        for key, child in value.items():
            rendered = json.dumps(child, ensure_ascii=False, default=str)
            excerpt = rendered if len(rendered) <= share else rendered[:share] + " [FIELD CONTENT OMITTED]"
            blocks.append(f"{key}: {excerpt}\n")
        return (header + inventory + "".join(blocks))[:limit]
    available = max(0, limit - len(header) - 35)
    half = available // 2
    return (header + text[:half] + "\n[... middle omitted ...]\n" + (text[-half:] if half else ""))[:limit]


def finite_numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)] if math.isfinite(value) else []
    if isinstance(value, dict):
        return [number for key, child in value.items()
                if key not in {"count", "n", "n_seeds", "seed", "returncode", "total_runs"}
                for number in finite_numbers(child)]
    if isinstance(value, list):
        return [number for child in value for number in finite_numbers(child)]
    return []


def has_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(has_nonfinite(child) for child in value.values())
    if isinstance(value, list):
        return any(has_nonfinite(child) for child in value)
    return False
