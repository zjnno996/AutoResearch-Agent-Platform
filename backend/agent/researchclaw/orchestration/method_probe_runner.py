"""Bounded subprocess driver for held method witnesses and one full-run trace.

Use the experiment's configured Python, not an isolated interpreter that loses
its environment. This runner is a correctness witness, not a security sandbox.
The caller's existing ToolExecutor enforces the shared runtime and <=30s pilot.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import importlib
from importlib.machinery import SourceFileLoader
import io
import json
import os
from pathlib import Path
import runpy
import sys
import time
import traceback

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact_generation import source_files
from researchclaw.orchestration.compact_state import file_hash
from researchclaw.orchestration.method_probes import VERSION, _module, _probe_tree, _reference_file


def _contained(root: Path, name: str) -> Path:
    path = root / name
    if (not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
            or not path.resolve().is_relative_to(root.resolve())
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise ValueError("Invalid or symlinked method witness path")
    return path


def _has_call(value: ast.AST | None) -> bool:
    return value is not None and any(isinstance(n, (ast.Call, ast.Lambda, ast.Await, ast.Yield, ast.NamedExpr)) for n in ast.walk(value))


def _import_safe(tree: ast.Module) -> None:
    def check(nodes):
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(name.split(".")[-1] == "main" for name in names):
                    raise ValueError("A method module cannot import the execution entrypoint")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.decorator_list or any(_has_call(x) for x in [*node.args.defaults, *node.args.kw_defaults,
                     node.returns, *[a.annotation for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]]]):
                    raise ValueError("Method modules need import-safe function definitions")
            elif isinstance(node, ast.ClassDef):
                if node.decorator_list or any(_has_call(x) for x in node.bases):
                    raise ValueError("Method modules need import-safe class definitions")
                check(node.body)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                if _has_call(node.value):
                    raise ValueError("Method module executes a call at import; move work into functions")
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            elif isinstance(node, ast.Pass):
                continue
            elif (isinstance(node, ast.If) and not node.orelse and isinstance(node.test, ast.Compare)
                  and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
                  and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
                  and len(node.test.comparators) == 1 and isinstance(node.test.comparators[0], ast.Constant)
                  and node.test.comparators[0].value == "__main__"):
                continue
            else:
                raise ValueError("Method binding module is not import-safe")
    check(tree.body)


def _load(manifest: Path, work: Path, *, evidence_origin_root: Path | None = None) -> tuple[dict, dict]:
    manifest, work = manifest.resolve(), work.resolve()
    record = read_json(manifest)
    if record.get("schema_version") != VERSION or record.get("status") != "ready" or not record.get("method_steps"):
        raise ValueError("No applicable held method witnesses in this manifest")
    root = Path(record["run_root"]).resolve()
    evidence_work = work
    if evidence_origin_root is not None:
        # Read-only audit of copied historical bytes. Execution never supplies
        # this argument. Keep the original receipt identity while reading and
        # hashing only the copied source, held probes and receipts.
        origin = Path(evidence_origin_root).resolve()
        copied_root = manifest.parents[2]
        if (root != origin or manifest.relative_to(copied_root).as_posix() != 'stage-09/method_probes/manifest.json'
                or work != copied_root/'stage-11/experiment'):
            raise ValueError('Invalid historical method-evidence relocation')
        root = copied_root
        evidence_work = origin/'stage-11/experiment'
    if not manifest.is_relative_to(root) or not work.is_relative_to(root) or manifest.is_relative_to(work):
        raise ValueError("Held probes must stay outside editable implementation source")
    sources = {p.relative_to(work).as_posix(): file_hash(p) for p in source_files(work)}
    bindings = {}
    for step in record["method_steps"]:
        name = _module(step["binding"]["module"])
        path = _contained(work, name)
        if name not in sources:
            raise ValueError("Method binding is not declared implementation source")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _import_safe(tree)
        function = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == step["binding"]["callable"]]
        if len(function) != 1:
            raise ValueError("Method binding must resolve to exactly one top-level synchronous Python function")
        for directory in path.parents:
            if directory == work:
                break
            init = directory / "__init__.py"
            if init.is_file():
                _import_safe(ast.parse(init.read_text(encoding="utf-8")))
        bindings[step["id"]] = {"module": name, "callable": function[0].name,
                                "filename": str(evidence_work/name), "first_line": function[0].lineno,
                                "source_sha256": sources[name]}
    for probe in record["method_probes"]:
        path = _contained(manifest.parent, probe["path"])
        if file_hash(path) != probe["sha256"]:
            raise ValueError("Held method probe was changed")
        _probe_tree(path.read_text(encoding="utf-8"))
    return record, {"source_hashes": sources, "bindings": bindings}


def snapshot(manifest: Path, work: Path, *, evidence_origin_root: Path | None = None) -> dict:
    record, state = _load(manifest, work, evidence_origin_root=evidence_origin_root)
    references = {}
    for step in record["method_steps"]:
        source = step["source"]
        if source.get("source_path"):
            path = _reference_file(source["source_path"])
            current = file_hash(path)
            if current != source["source_sha256"]:
                raise ValueError("Bound method reference changed")
            references[str(path.resolve())] = current
    return {"manifest_sha256": file_hash(manifest), "probe_hashes": {p["id"]: p["sha256"] for p in record["method_probes"]},
            "reference_hashes": references, **state}


class _AssertInstrument(ast.NodeTransformer):
    def visit_Assert(self, node):
        return ast.copy_location(ast.Expr(ast.Call(ast.Name("__method_probe_assert", ast.Load()),
                                                  [node.test, ast.Constant(node.lineno)], [])), node)


class _BoundedLog(io.TextIOBase):
    def __init__(self):
        self.chunks, self.used, self.dropped = [], 0, 0

    def write(self, text):
        kept = text[:max(0, 4000 - self.used)]
        if kept:
            self.chunks.append(kept)
        self.used += len(kept)
        self.dropped += len(text) - len(kept)
        return len(text)

    def value(self):
        return "".join(self.chunks) + (f"\n[{self.dropped} further characters omitted]" if self.dropped else "")


def _profile(bindings, counts):
    keys = {}
    for identifier, binding in bindings.items():
        key = (binding["filename"], binding["callable"], binding["first_line"])
        keys.setdefault(key, []).append(identifier)
    def profile(frame, event, arg):
        if event == "call":
            code = frame.f_code
            for identifier in keys.get((code.co_filename, getattr(code, "co_qualname", code.co_name), code.co_firstlineno), []):
                counts[identifier] += 1
    return profile


def _pilot(manifest: Path, work: Path, record: dict, bindings: dict, counts: dict) -> list[dict]:
    results = []
    for probe in record["method_probes"]:
        binding = bindings[probe["method_step_id"]]
        module_name = binding["module"][:-3].replace("/", ".")
        module = importlib.import_module(module_name)
        if str(Path(module.__file__).resolve()) != binding["filename"]:
            raise ValueError("Method module resolves outside the bound source file")
        function = getattr(module, binding["callable"])
        if not callable(function) or not hasattr(function, "__code__") or function.__code__.co_filename != binding["filename"]:
            raise ValueError("Method binding is not the declared Python implementation")
        stats = {"assertions": 0, "failed_assertions": 0, "invocations": 0}
        def invoke(*args, **kwargs):
            stats["invocations"] += 1
            return function(*args, **kwargs)
        def witness_assert(value, line):
            stats["assertions"] += 1
            try:
                okay = bool(value)
            except BaseException:
                stats["failed_assertions"] += 1
                raise
            if not okay:
                stats["failed_assertions"] += 1
                raise AssertionError(f"Method witness assertion failed at line {line}")
        tree = _AssertInstrument().visit(_probe_tree((manifest.parent / probe["path"]).read_text(encoding="utf-8")))
        ast.fix_missing_locations(tree)
        namespace = {"__name__": "held_method_witness", "__method_probe_assert": witness_assert}
        exec(compile(tree, str(manifest.parent / probe["path"]), "exec"), namespace)
        before = counts[probe["method_step_id"]]
        entry = {"id": probe["id"], "method_step_id": probe["method_step_id"], "passed": False}
        try:
            namespace["check"](invoke)
            if not stats["assertions"] or stats["failed_assertions"] or not stats["invocations"] or counts[probe["method_step_id"]] <= before:
                raise ValueError("Method witness needs a real target call and executed successful assertion; caught failures remain failures")
            entry["passed"] = True
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        entry.update(stats)
        entry["target_calls"] = counts[probe["method_step_id"]] - before
        results.append(entry)
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--mode", choices=("pilot", "observe"), required=True)
    parser.add_argument("--entrypoint", default="main.py")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    manifest, work, receipt = args.manifest.resolve(), args.work.resolve(), args.receipt.resolve()
    if receipt == manifest or receipt.is_relative_to(work) or receipt.is_relative_to(manifest.parent):
        raise ValueError("Controller method receipts must be outside held probes and implementation source")
    receipt.unlink(missing_ok=True)
    result = {"schema_version": "method-witness-receipt-v1", "mode": args.mode, "work": str(work), "passed": False,
              "scientific_scope": "Local method witnesses and actual call presence only; not a theorem proof or empirical success verdict."}
    begun, old_profile = time.monotonic(), sys.getprofile()
    old_get_code = SourceFileLoader.get_code
    old_path, old_argv, old_cwd = sys.path[:], sys.argv[:], Path.cwd()
    stdout, stderr = _BoundedLog(), _BoundedLog()
    try:
        if file_hash(manifest) != args.manifest_sha256:
            raise ValueError("Held manifest hash differs from the controller invocation")
        record, state = _load(manifest, work)
        result["integrity_before"] = snapshot(manifest, work)
        counts = {name: 0 for name in state["bindings"]}
        result["target_call_counts"] = counts
        sys.path.insert(0, str(work))
        os.chdir(work)
        # Timestamp-valid .pyc files can survive a same-size edit in one tick.
        # Execute the exact source whose digest is recorded, including imports.
        def fresh_local_code(loader, fullname):
            path = Path(loader.path)
            if path.suffix == '.py' and path.resolve().is_relative_to(work):
                return compile(loader.get_data(loader.path), str(path), 'exec', dont_inherit=True)
            return old_get_code(loader, fullname)
        SourceFileLoader.get_code = fresh_local_code
        sys.setprofile(_profile(state["bindings"], counts))
        if args.mode == "pilot":
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result["probes"] = _pilot(manifest, work, record, state["bindings"], counts)
            if not result["probes"] or not all(p["passed"] for p in result["probes"]):
                raise ValueError("One or more held method witnesses failed")
        else:
            entrypoint = _contained(work, args.entrypoint)
            if entrypoint.name != "main.py":
                raise ValueError("Observe only the actual main.py entrypoint")
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            sys.argv = [str(entrypoint), *arguments]
            try:
                runpy.run_path(str(entrypoint), run_name="__main__")
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    raise
            if any(count <= 0 for count in counts.values()):
                raise ValueError("A declared bound method was not called by the actual full program")
        sys.setprofile(old_profile)
        result["integrity_after"] = snapshot(manifest, work)
        if result["integrity_before"] != result["integrity_after"]:
            raise ValueError("Implementation source or held witness changed during execution")
        result["passed"] = True
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        result["traceback"] = traceback.format_exc()[-4000:]
    finally:
        SourceFileLoader.get_code = old_get_code
        sys.setprofile(old_profile)
        if "integrity_before" in result and "integrity_after" not in result:
            try:
                result["integrity_after"] = snapshot(manifest, work)
                if result["integrity_after"] != result["integrity_before"]:
                    result["passed"] = False
                    result["integrity_error"] = "Implementation source or held witness changed during execution"
            except BaseException as exc:
                result["passed"] = False
                result["integrity_error"] = f"{type(exc).__name__}: {exc}"[:1500]
        sys.path[:] = old_path
        sys.argv[:] = old_argv
        os.chdir(old_cwd)
        result.update(duration_sec=round(time.monotonic() - begun, 4), stdout=stdout.value(), stderr=stderr.value())
        write_json(receipt, result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
