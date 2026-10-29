"""Deterministic final checks supplement (and never replace) scientific review."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import read_json
from researchclaw.orchestration.compact_contracts import metrics_gap
from researchclaw.orchestration.compact_state import file_hash


def verify_deliverables(root: Path, contract: dict[str, Any], *,
                        evidence_origin_root: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    work = root / "stage-11/experiment"
    errors, images = [], []
    receipt = read_json(root / "stage-16/experiment_provenance.json")
    if not receipt.get("success") or receipt.get("returncode") != 0:
        errors.append("No successful controller-owned execution receipt")
    for field in ("source_hashes", "output_hashes"):
        if not receipt.get(field):
            errors.append(f"Execution receipt has no {field}")
        for relative, digest in receipt.get(field, {}).items():
            path = (work / relative).resolve()
            if not path.is_relative_to(work.resolve()) or not path.is_file() or file_hash(path) != digest:
                errors.append(f"Executed artifact changed or missing: {relative}")
    gap = metrics_gap(work / "outputs/metrics.json")
    if gap:
        errors.append(gap)
    checks: dict[str, Any] = {'passed': True, 'checks': []}
    plan_path = root / 'stage-09/exp_plan.yaml'
    if plan_path.is_file():
        import yaml
        from researchclaw.orchestration.execution_checks import evaluate_checks
        try:
            plan = yaml.safe_load(plan_path.read_text(encoding='utf-8'))
            from researchclaw.orchestration.method_execution import audit_method_execution
            method_checks = audit_method_execution(root, work, plan, receipt, evidence_origin_root=evidence_origin_root)
            if not method_checks['passed']:
                errors.append('Held method witnesses no longer match executed science: ' + str(method_checks['errors']))
            checks = evaluate_checks(work, plan.get('execution_checks', []))
            if not checks['passed']:
                errors.append('Required execution preconditions no longer pass: ' + str(checks['checks']))
        except (ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
            errors.append(f'Execution check definition is invalid: {exc}')
    report = root / "stage-25/paper_final.md"
    text = report.read_text(encoding="utf-8") if report.is_file() else ""
    if len(text) < 300:
        errors.append("Final report missing or too short")
    for original in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        relative = original.strip().strip("<>").split(' "', 1)[0]
        path = (report.parent / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append(f"Missing or invalid report figure: {relative}")
            continue
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
            images.append(str(path.relative_to(root)))
        except Exception:
            errors.append(f"Invalid report image: {relative}")
        if not path.is_relative_to(work) or str(path.relative_to(work)) not in receipt.get("output_hashes", {}):
            errors.append(f"Report figure lacks executed provenance: {relative}")
    if not images:
        errors.append("Report must embed at least one real executed figure")
    return {"passed": not errors, "errors": errors, "images": images,
            'execution_checks': checks,
            "execution_verified": bool(receipt.get("success")), "scientific_validity": "requires_independent_review"}
