"""Contracts for the six-phase workflow; invalid candidates never replace files."""
from __future__ import annotations

import ast
import json
import shlex
import math
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import has_nonfinite


class ContractError(ValueError):
    pass


def execution_arguments(plan: dict[str, Any]) -> tuple[list[str], str]:
    """Keep scientific CLI settings; reporting is owned by the next phase."""
    raw = plan.get("protocol", {}).get("command", "python main.py")
    if not isinstance(raw, str):
        raise ContractError("protocol.command must be one python main.py invocation")
    parts = raw.split("&&", 1)
    try:
        tokens = shlex.split(parts[0])
    except ValueError as exc:
        raise ContractError(f"Invalid execution arguments: {exc}") from exc
    while tokens and tokens[0] in {"OPENBLAS_NUM_THREADS=1", "OMP_NUM_THREADS=1", "MKL_NUM_THREADS=1"}:
        tokens.pop(0)
    if len(tokens) < 2 or not Path(tokens[0]).name.startswith("python") or Path(tokens[1]).name != "main.py":
        raise ContractError("protocol.command must start with python main.py; put other scientific steps inside main.py")
    if any(token in {";", "|", "||", ">", "<", "&"} for token in tokens):
        raise ContractError("Use one scientific main.py entrypoint without shell operators")
    deferred = parts[1].strip() if len(parts) > 1 else ""
    if deferred and "report" not in deferred.casefold():
        raise ContractError("Put all scientific computations inside main.py; only reporting may be deferred")
    return tokens[2:], deferred


def validate_requirements(value: dict[str, Any]) -> None:
    rows = value.get("requirements")
    if not isinstance(rows, list) or not rows:
        raise ContractError("Task contract needs explicit requirements")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
                                              for k in ("id", "requirement", "acceptance")):
            raise ContractError("Every requirement needs id, requirement, acceptance")
        if row["id"] in ids:
            raise ContractError("Duplicate requirement ID")
        ids.add(row["id"])


def validate_plan(value: dict[str, Any], contract: dict[str, Any]) -> None:
    # Keep the scientific plan's existing bound; separately bounded executable
    # witnesses should not displace derivations or force lossy compression.
    scientific_fields = {key: item for key, item in value.items() if key != 'method_probes'}
    from researchclaw.llm.provider_policy import unlimited_tokens_enabled
    plan_limit = 256000 if unlimited_tokens_enabled() else 40000
    if len(json.dumps(scientific_fields)) > plan_limit:
        raise ContractError(f"Plan exceeds {plan_limit:,} characters; summarize repeated details without dropping obligations")
    for key in ("methods", "metrics"):
        if not isinstance(value.get(key), list) or not value[key]:
            raise ContractError(f"Plan needs nonempty {key}")
    if not isinstance(value.get('datasets'), list):
        raise ContractError('Plan datasets must be a list')
    if not value['datasets'] and not (isinstance(value.get('dataset_policy'), str)
                                      and value['dataset_policy'].strip()):
        raise ContractError('Missing top-level dataset_policy: explain the theoretical or synthetic inputs alongside datasets, not inside protocol')
    if not isinstance(value.get("protocol"), dict) or not value["protocol"]:
        raise ContractError("Plan needs an executable protocol")
    execution_arguments(value)
    coverage = value.get("requirement_map")
    if not isinstance(coverage, dict):
        raise ContractError("Plan needs requirement_map")
    for row in contract["requirements"]:
        item = coverage.get(row["id"])
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip()
                                               for k in ("approach", "artifact")):
            raise ContractError(f"No approach and evidence artifact for {row['id']}")
    from researchclaw.orchestration.execution_checks import validate_checks
    validate_checks(value.get('execution_checks', []), {row['id'] for row in contract['requirements']})
    from researchclaw.orchestration.task_fidelity import validate_plan_routes
    validate_plan_routes(value, contract)
    from researchclaw.orchestration.method_probes import validate_method_plan
    validate_method_plan(value, {row['id'] for row in contract['requirements']})


def apply_plan_patch(plan: dict[str, Any], patch: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Merge named fields, including new proof obligations, without losing the contract."""
    fields = patch.get("replace_fields")
    if not isinstance(fields, dict) or not fields or any(not isinstance(key, str) or not key for key in fields):
        raise ContractError("Patch needs nonempty replace_fields with named top-level fields")
    candidate = {**plan, **fields}
    validate_plan(candidate, contract)
    return candidate


def code_gap(workspace: Path) -> str | None:
    main = workspace / "main.py"
    if not main.is_file() or main.stat().st_size == 0:
        return "Write a runnable main.py in the workspace."
    try:
        ast.parse(main.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeError) as exc:
        return f"main.py does not parse: {exc}"
    return None


def metrics_gap(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return "outputs/metrics.json must be valid JSON produced by main.py"
    if not isinstance(value, dict) or not value:
        return "Metrics must be a nonempty JSON object"
    if has_nonfinite(value):
        paths=[]
        def locate(item: Any, at: str) -> None:
            if len(paths)>=8:
                return
            if isinstance(item,float) and not math.isfinite(item):
                paths.append(at)
            elif isinstance(item,dict):
                for key,child in item.items():locate(child,at+'.'+str(key))
            elif isinstance(item,list):
                for index,child in enumerate(item):locate(child,f'{at}[{index}]')
        locate(value,'metrics')
        return ('Non-finite metrics at '+', '.join(paths)+'. Repair the computation; optional undefined statistics '
                'must be null with a reason. Do not conceal failed or diverged primary measurements.')
    def numbers(item: Any) -> int:
        if type(item) in (int, float):
            return 1
        if isinstance(item, dict):
            return sum(numbers(child) for child in item.values())
        if isinstance(item, list):
            return sum(numbers(child) for child in item)
        return 0
    if not numbers(value):
        return "Metrics contain no measured numbers"
    return None


def validate_review(value: dict[str, Any], contract: dict[str, Any]) -> None:
    rows = value.get("criteria")
    if not isinstance(rows, list):
        raise ContractError("Review needs per-requirement criteria")
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    expected = [row["id"] for row in contract["requirements"]]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ContractError("Review must enumerate every unchanged requirement exactly once")
    for row in rows:
        if row.get("status") not in {"met", "unmet", "unverified"}:
            raise ContractError("Invalid review status")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ContractError("Every review decision needs a reason")
        if row["status"] == "met" and (not isinstance(row.get("evidence"), list) or not row["evidence"]):
            raise ContractError("Met criteria need evidence paths")
    if not isinstance(value.get("missing_requirements"), list):
        raise ContractError("Review must independently re-enumerate omitted requirements")
    from researchclaw.orchestration.task_fidelity import validate_review_routes
    validate_review_routes(value, contract)
    score = value.get("quality_score")
    if type(score) not in (int, float) or not 0 <= score <= 10:
        raise ContractError("Review needs a quality_score between 0 and 10")
    dimensions = value.get("dimensions", {})
    if not isinstance(dimensions, dict):
        raise ContractError("Review dimensions must be an object")
    for name, row in dimensions.items():
        if (not isinstance(row, dict) or row.get("status") not in {"pass", "gap", "unverified", "not_applicable"}
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()):
            raise ContractError(f"Review dimension {name} needs status and reason")
