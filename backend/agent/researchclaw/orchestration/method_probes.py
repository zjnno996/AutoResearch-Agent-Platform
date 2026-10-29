"""Source-bound method witnesses held outside editable experiment source.

These small, reviewed witnesses check specific implementation behavior. Passing
them neither proves a theorem nor establishes an empirical research hypothesis.
The existing planner/design review owns their assumptions and independence.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.compact_state import file_hash, fingerprint

HELD_MANIFEST = "stage-09/method_probes/manifest.json"
VERSION = "held-method-probes-v1"
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_IMPORTS = {"math", "cmath", "fractions", "decimal", "statistics", "numpy", "scipy"}
_FORBIDDEN = {"target_study", "evaluation", "private", "secrets", "credentials", "checklist", "checklists"}


def schema_hint() -> str:
    return ('\nHeld scientific method witnesses: return method_steps (at most 3) and method_probes '
            '(at most 6; code at most 6000 characters each, 12000 total). Each step is '
            '{"id":"M1","requirement_ids":["R1"],"operation":"exact update/equation or estimator",'
            '"inputs":"quantities and units","outputs":"quantities and units","assumptions":[], '
            '"source":{"reference":"task OR exact cached source URL/alias/path","quote":"exact original passage"},'
            '"binding":{"module":"scientific_methods.py","callable":"top_level_function"}}. '
            'Use a separate import-safe module, never main.py; preserve its actual use by main.py. '
            'Use one listed reference identifier, not a combined title/path/section description. '
            'Quotations may reflow whitespace from PDF lines; their words, punctuation and equations must remain exact. '
            'Each step needs a probe {"id":"P1","method_step_id":"M1","kind":"analytic|metamorphic",'
            '"basis":"source-backed derivation/preconditions", "wrong_alternative":"specific incorrect variant distinguished",'
            '"code":"def check(invoke):\\n    actual = invoke(...)\\n    assert actual == independently_derived_value\\n"}. '
            'Use tiny analytic fixtures or applicable metamorphic properties, not published task answers, '
            'desired scores, self-reported metrics, or the tested function as its own oracle. Imports may use '
            'math/cmath/fractions/decimal/statistics/numpy/scipy. Invoke the actual bound callable and execute '
            'a meaningful assert. These scripts are frozen after design review outside editable source. '
            'A negative empirical hypothesis is not an implementation failure; numerical witnesses do not '
            'certify a general proof. If no suitable executable method applies, return both lists empty and '
            'a nonempty method_probe_note explaining the scientific reason; never invent data or a witness.\n')


def _text(value: Any, label: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f"Method witness needs nonempty {label} (at most {limit} characters)")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContractError(f"Invalid method witness {label}")
    return value


def _module(value: Any) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ContractError("Method binding needs a relative Python module path")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or path.suffix != ".py"
            or path.name in {"main.py", "__init__.py"}
            or any(not part.isidentifier() or part.startswith("_") for part in path.with_suffix("").parts)
            or set(path.parts) & {"data", "outputs", "related_work", "method_probes"}):
        raise ContractError("Method binding must name a separate import-safe Python module, never main.py")
    return value


def _probe_tree(code: str) -> ast.Module:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ContractError(f"Invalid method probe Python: {exc}") from exc
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "check"]
    if len(functions) != 1:
        raise ContractError("A method probe must define one check(invoke) function")
    function = functions[0]
    args = function.args
    if (len(args.posonlyargs) + len(args.args) != 1 or (args.posonlyargs + args.args)[0].arg != "invoke"
            or args.vararg or args.kwarg or args.kwonlyargs or args.defaults or function.decorator_list):
        raise ContractError("Method probe entrypoint must be check(invoke)")
    assertions = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    if not assertions:
        raise ContractError("Method probes must execute an assert about the bound method")
    # A small syntactic data-flow check excludes disconnected clone tests. It
    # does not claim semantic oracle independence; design review still owns that.
    tainted = set()
    def depends(node):
        return any((isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "invoke")
                   or (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in tainted)
                   for n in ast.walk(node))
    changed = True
    while changed:
        before = set(tainted)
        for node in ast.walk(function):
            value, targets = None, []
            if isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
                value, targets = node.value, [node.target]
            elif isinstance(node, ast.For):
                value, targets = node.iter, [node.target]
            if value is not None and depends(value):
                tainted.update(n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name))
        changed = tainted != before
    if not any(depends(node.test) for node in assertions):
        raise ContractError("A method assertion must depend on the actual invoke return value")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("__method_probe"):
            raise ContractError("Reserved method probe instrumentation name")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ContractError("Method probe introspection is not a scientific oracle")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            roots = [alias.name.split(".")[0] for alias in node.names] if isinstance(node, ast.Import) else [(node.module or "").split(".")[0]]
            if isinstance(node, ast.ImportFrom) and node.level or any(root not in _IMPORTS for root in roots):
                raise ContractError("Method probes may import only supported numerical libraries")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                "exec", "eval", "compile", "open", "__import__", "input", "globals", "locals", "vars"}:
            raise ContractError("Method probes cannot load code, files or external answers")
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant):
                raise ContractError("A constant assertion is not a method witness")
            if (isinstance(node.test, ast.Compare) and len(node.test.comparators) == 1
                    and isinstance(node.test.ops[0], (ast.Eq, ast.Is))
                    and ast.dump(node.test.left) == ast.dump(node.test.comparators[0])):
                raise ContractError("A value compared with itself is not a method witness")
    return tree


def validate_method_plan(plan: dict, requirement_ids: set[str], optionallegacy: bool = True) -> None:
    if "method_steps" not in plan and "method_probes" not in plan:
        if optionallegacy:
            return
        raise ContractError("New plans need method_steps and method_probes, or justified empty lists")
    steps, probes = plan.get("method_steps"), plan.get("method_probes")
    if not isinstance(steps, list) or not isinstance(probes, list) or len(steps) > 3 or len(probes) > 6:
        raise ContractError("Method witnesses allow at most 3 steps and 6 probes")
    if not steps or not probes:
        if steps or probes:
            raise ContractError("Each method step needs at least one held probe")
        _text(plan.get("method_probe_note"), "method_probe_note")
        return
    ids = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ContractError("Method steps must be objects")
        identifier = _identifier(step.get("id"), "step ID")
        if identifier in ids:
            raise ContractError("Duplicate method step ID")
        ids.add(identifier)
        requirements = step.get("requirement_ids")
        if (not isinstance(requirements, list) or not requirements
                or any(not isinstance(rid, str) or rid not in requirement_ids for rid in requirements)
                or len(set(requirements)) != len(requirements)):
            raise ContractError("Method steps must bind existing requirement IDs")
        for key in ("operation", "inputs", "outputs"):
            _text(step.get(key), key)
        assumptions = step.get("assumptions")
        if not isinstance(assumptions, list) or any(not isinstance(x, str) or not x.strip() for x in assumptions):
            raise ContractError("Method assumptions must be a list of explicit strings")
        source, binding = step.get("source"), step.get("binding")
        if not isinstance(source, dict) or not isinstance(binding, dict):
            raise ContractError("Method steps need source and implementation binding objects")
        _text(source.get("reference"), "source.reference")
        _text(source.get("quote"), "source.quote", 4000)
        _module(binding.get("module"))
        name = binding.get("callable")
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            raise ContractError("Bind an exact public top-level Python function")
    probe_ids, covered, total = set(), set(), 0
    for probe in probes:
        if not isinstance(probe, dict):
            raise ContractError("Method probes must be objects")
        identifier = _identifier(probe.get("id"), "probe ID")
        if identifier in probe_ids:
            raise ContractError("Duplicate method probe ID")
        probe_ids.add(identifier)
        if probe.get("method_step_id") not in ids or probe.get("kind") not in {"analytic", "metamorphic"}:
            raise ContractError("Method probes need an existing step and analytic/metamorphic kind")
        covered.add(probe["method_step_id"])
        _text(probe.get("basis"), "independent probe basis")
        _text(probe.get("wrong_alternative"), "wrong_alternative distinguished")
        code = _text(probe.get("code"), "probe code", 6000)
        total += len(code)
        _probe_tree(code)
    if covered != ids or total > 12000:
        raise ContractError("Every method step needs a witness; total probe code is limited to 12000 characters")


def _reference_file(value: str) -> Path:
    path = Path(value)
    if (not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024
            or any(part.casefold() in _FORBIDDEN or part.casefold().startswith("checklist")
                   or part.casefold().startswith("external_evaluation") or part.casefold().startswith(".env") for part in path.resolve().parts)):
        raise ContractError("Method source is not an allowed cached task-visible reference")
    return path


def _reference_rows(refs: dict | list) -> list[dict]:
    rows = refs.get("references", []) if isinstance(refs, dict) else refs
    candidates = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        aliases = [row.get("source"), row.get("text_path"), *row.get("alias_sources", [])]
        if row.get("text_path"):
            candidates.append({"names": aliases, "path": row["text_path"],
                               "sha256": row.get("text_sha256", row.get("sha256"))})
        code = row.get("public_code", {})
        for item in code.get("files", []) if isinstance(code, dict) else []:
            if isinstance(item, dict) and item.get("local_path"):
                candidates.append({"names": [item.get("path"), item["local_path"]],
                                   "path": item["local_path"], "sha256": item.get("raw_sha256")})
    return candidates


def method_source_identifiers(refs: dict | list) -> list[str]:
    """Expose only supplied source aliases, without opening new paths."""
    return ['task', *sorted({name for row in _reference_rows(refs) for name in row['names']
                           if isinstance(name, str) and name.strip() and len(name) <= 2000 and name != 'task'})]


def exact_quote_guidance(text: str, quote: str) -> str:
    """Offer original bytes for correction; never accept approximate science."""
    tokens = quote.split()
    for width in (12, 8, 5):
        if len(tokens) < width:
            continue
        for first in range(min(len(tokens)-width+1, 64)):
            pattern = re.compile(r'\s+'.join(re.escape(token) for token in tokens[first:first+width]))
            matches = []
            for match in pattern.finditer(text):
                matches.append(match)
                if len(matches) == 2:
                    break
            if len(matches) != 1:
                continue
            match = matches[0]
            start = max(0, match.start()-160)
            end = min(len(text), max(match.end()+900, start+1600))
            excerpt = text[start:end]
            return (' The following is an exact original source excerpt near a shared phrase, not '
                    'an accepted quotation or proof of relevance. Select and copy a scientifically relevant '
                    'contiguous span exactly, including equation typography; correct only the defective source '
                    'field and preserve the valid protocol. Do not paraphrase or replace the method. '
                    'Original excerpt (JSON-escaped for lossless copying): ' + json.dumps(excerpt, ensure_ascii=False))
    return ''


def validate_method_sources(plan: dict, task: str, refs: dict | list) -> list[dict]:
    """Resolve exact source quotations without publishing any file."""
    bound = []
    candidates = _reference_rows(refs)
    for step in plan.get("method_steps", []):
        source = step["source"]
        reference, quote = source["reference"], source["quote"]
        path = None
        if reference == "task":
            text, sha = task, hashlib.sha256(task.encode()).hexdigest()
        else:
            matches = [row for row in candidates if reference in row["names"]]
            unique = {(str(row["path"]), row["sha256"]) for row in matches}
            if len(unique) != 1:
                raise ContractError("Method source must identify exactly one supplied cached reference")
            path_string, sha = unique.pop()
            path = _reference_file(path_string)
            if not isinstance(sha, str) or file_hash(path) != sha:
                raise ContractError("Method source cache hash changed or was not recorded")
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise ContractError("Method source must be cached UTF-8 text") from exc
        original_quote = quote
        anchor_id = source.get('anchor_id')
        if anchor_id is not None:
            start, end = source.get('start_character'), source.get('end_character')
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                    or source.get('source_sha256') != sha or text[start:end] != quote):
                raise ContractError('Controller source anchor differs from its original hashed span')
            expected = 'A'+hashlib.sha256(json.dumps([reference, sha, start, end]).encode()).hexdigest()[:20]
            if anchor_id != expected:
                raise ContractError('Controller source anchor identity does not match its reference and original span')
            offset = start
        else:
            offset = text.find(quote)
        reflowed = False
        if offset < 0:
            # PDF extraction inserts line/page whitespace. Resolve only a
            # unique original span with identical non-whitespace characters;
            # never accept paraphrases, omitted words or changed equations.
            pattern = re.compile(r'\s+'.join(re.escape(token) for token in quote.split()))
            matches = []
            for match in pattern.finditer(text):
                matches.append(match)
                if len(matches) == 2:
                    break
            if len(matches) != 1 or matches[0].end() - matches[0].start() > 8000:
                raise ContractError("Method source quote must match exact original text or one unique whitespace-reflowed span"
                                    + exact_quote_guidance(text, quote))
            match = matches[0]
            offset, quote, reflowed = match.start(), text[match.start():match.end()], True
        record = {"reference": reference, "quote": quote, "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                  "source_sha256": sha, "start_character": offset, "end_character": offset + len(quote),
                  "start_line": text.count("\n", 0, offset) + 1}
        if anchor_id is not None:
            record.update(source_anchor_id=anchor_id, quote_resolution='controller_original_source_span')
        if reflowed:
            record.update(quote_resolution='unique_whitespace_reflow', provided_quote=original_quote)
        if path is not None:
            record["source_path"] = str(path.resolve())
        bound.append(record)
    return bound


def prepare_method_probes(root: Path, plan: dict, task: str, refs: dict | list, *, replace_approved: bool = False) -> tuple[dict, list[str]]:
    root = root.resolve()
    requirement_ids = {rid for step in plan.get("method_steps", []) for rid in step.get("requirement_ids", [])}
    validate_method_plan(plan, requirement_ids)
    sources = validate_method_sources(plan, task, refs)
    directory = root / Path(HELD_MANIFEST).parent
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ContractError("Held method files cannot use symlinked directories")
    directory.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": VERSION, "run_root": str(root), "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
              "status": "ready" if plan.get("method_steps") else "not_applicable" if "method_steps" in plan else "legacy",
              "method_probe_note": plan.get("method_probe_note", "Legacy plan predates held method witnesses."),
              "plan_sha256": fingerprint({k: plan.get(k) for k in ("method_steps", "method_probes", "method_probe_note")}),
              "method_steps": [{**step, "source": source} for step, source in zip(plan.get("method_steps", []), sources)],
              "method_probes": []}
    paths = []
    for probe in plan.get("method_probes", []):
        sha = hashlib.sha256(probe["code"].encode()).hexdigest()
        name = f"probe-{probe['id']}-{sha[:16]}.py"
        path = directory / name
        if path.exists() and (path.is_symlink() or file_hash(path) != sha):
            raise ContractError("A held method probe was modified; do not rewrite its oracle during repair")
        if not path.exists():
            path.write_text(probe["code"], encoding="utf-8")
        record["method_probes"].append({**{k: v for k, v in probe.items() if k != "code"}, "path": name, "sha256": sha})
        paths.append(path.relative_to(root).as_posix())
    manifest = root / HELD_MANIFEST
    if manifest.is_symlink():
        raise ContractError("Held method manifest cannot be symlinked")
    if manifest.exists() and read_json(manifest) != record and not replace_approved:
        raise ContractError("Held method design changed; replacement requires explicit new design approval")
    if not manifest.exists() or read_json(manifest) != record:
        write_json(manifest, record)
    paths.append(HELD_MANIFEST)
    return {**record, "manifest_path": str(manifest), "manifest_sha256": file_hash(manifest)}, paths


def runner_arguments(manifest_path: Path, work: Path, receipt: Path, mode: str = "pilot", arguments=(), *,
                     expected_manifest_sha256: str | None = None) -> list[str]:
    if mode not in {"pilot", "observe"}:
        raise ValueError("Unknown method witness runner mode")
    actual = file_hash(Path(manifest_path))
    if expected_manifest_sha256 is not None and actual != expected_manifest_sha256:
        raise ValueError("Held manifest no longer matches the approved controller hash")
    args = [str(Path(__file__).with_name("method_probe_runner.py").resolve()), "--manifest", str(Path(manifest_path).resolve()),
            "--manifest-sha256", expected_manifest_sha256 or actual, "--work", str(work.resolve()),
            "--receipt", str(receipt.resolve()), "--mode", mode]
    if mode == "observe":
        args.extend(["--entrypoint", "main.py", "--", *arguments])
    return args


def read_method_receipt(manifest_path: Path, work: Path, receipt: Path, mode: str, *,
                         evidence_origin_root: Path | None = None) -> dict:
    from researchclaw.orchestration.method_probe_runner import snapshot
    current = snapshot(Path(manifest_path), Path(work), evidence_origin_root=evidence_origin_root)
    expected_work = ((Path(evidence_origin_root).resolve()/'stage-11/experiment')
                     if evidence_origin_root is not None else Path(work).resolve())
    record = read_json(Path(receipt))
    if (record.get("schema_version") != "method-witness-receipt-v1" or record.get("mode") != mode
            or record.get("work") != str(expected_work) or record.get("passed") is not True
            or record.get("integrity_before") != current or record.get("integrity_after") != current):
        reason = record.get("error", "Missing, failed or stale method witness receipt")
        raise ValueError(f"Method witness unavailable: {reason}")
    steps = read_json(Path(manifest_path)).get("method_steps", [])
    counts = record.get("target_call_counts", {})
    if not steps or any(type(counts.get(step["id"])) is not int or counts[step["id"]] <= 0 for step in steps):
        raise ValueError("Method witness did not enter every bound implementation function")
    if mode == "pilot" and (not record.get("probes") or any(not p.get("passed") or p.get("assertions", 0) <= 0 for p in record["probes"])):
        raise ValueError("Method witness lacks executed successful assertions")
    return record
