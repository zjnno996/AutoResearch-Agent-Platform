"""Retain whole reviewed evidence when a scientific revision regresses."""
from __future__ import annotations

import copy
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact_state import PHASES, file_hash, fingerprint


ERROR = "Scientific review has unresolved requirements; see compact_review.json"


def _path(root: Path, relative: str) -> Path:
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise ValueError(f"Invalid retained artifact path: {relative}")
    path = root / part
    if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise ValueError(f"Symlinked retained artifact path: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Retained artifact leaves its run")
    return path


def managed_inventory(root: Path) -> set[str]:
    """Owned implementation/output/report files; never data or caches."""
    found = set()
    for directory in ("stage-11/experiment", "stage-25"):
        base = root / directory
        for path in base.rglob("*") if base.is_dir() else ():
            relative = path.relative_to(base)
            if relative.parts[0] == "data" or ("__pycache__" in relative.parts and relative.parts[0] != "outputs"):
                continue
            if path.is_symlink():
                raise ValueError(f"Symlinked scientific artifact: {path}")
            if path.is_file():
                found.add(path.relative_to(root).as_posix())
    return found


def check_inputs(observed: dict[str, Any]) -> None:
    for name, expected in observed.items():
        path = Path(name)
        if not path.is_file():
            raise ValueError("Original research input disappeared")
        stat = path.stat()
        actual = file_hash(path) if stat.st_size <= 128 * 1024 * 1024 else [stat.st_size, stat.st_mtime_ns]
        if actual != expected:
            raise ValueError("Original research input changed; retention cannot hide it")


def bound_review(root: Path, audit: dict | None = None) -> tuple[Path, dict]:
    """Resolve either the latest assessment or an explicit historical selection."""
    from researchclaw.orchestration.compact_generation import source_files
    from researchclaw.orchestration.compact_verification import verify_deliverables
    current = read_json(root / "compact_review.json")
    selected = read_json(root / "compact_retention.json")
    if selected.get("status") == "retained":
        manifest = _path(root, selected["baseline_manifest"])
        if file_hash(manifest) != selected["baseline_manifest_sha256"]:
            raise ValueError("Historical bundle manifest changed")
        baseline = read_json(manifest)
        for key in ("origin_review_archive", "origin_review_sha256", "contract_sha256",
                    "input_fingerprints", "managed_inventory"):
            if selected.get(key) != baseline.get(key):
                raise ValueError("Historical bundle pointer disagrees with its immutable manifest")
        archive = _path(root, selected["origin_review_archive"])
        if (not archive.is_relative_to(root / "research_reviews")
                or file_hash(archive) != selected["origin_review_sha256"]):
            raise ValueError("Historical review identity changed")
        if selected.get("contract_sha256") != file_hash(root / "compact_contract.json"):
            raise ValueError("Historical review belongs to another task contract")
        check_inputs(selected.get("input_fingerprints", {}))
        if sorted(managed_inventory(root)) != selected.get("managed_inventory"):
            raise ValueError("Retained scientific inventory changed")
        for relative in baseline["managed_inventory"]:
            if file_hash(_path(root, relative)) != baseline["files"][relative]:
                raise ValueError(f"Retained scientific artifact changed: {relative}")
        assessment = selected.get('decision', {}).get('fresh_baseline_assessment')
        if assessment:
            fresh = _path(root, assessment)
            if (not fresh.is_file() or file_hash(fresh) != selected['decision']['fresh_baseline_assessment_sha256']
                    or read_json(fresh).get('baseline_manifest_sha256') != selected['baseline_manifest_sha256']):
                raise ValueError('Fresh historical-bundle assessment changed')
    else:
        archives = sorted((root / "research_reviews").glob("*/review.json"), key=lambda p: p.stat().st_mtime_ns)
        if not archives:
            raise ValueError("No immutable scientific review archive")
        archive = archives[-1]
    record = read_json(archive)
    if record.get("review") != current:
        raise ValueError("Current assessment differs from its bound archived review")
    local = audit or verify_deliverables(root, read_json(root / "compact_contract.json"))
    if not local["passed"]:
        raise ValueError("Current deliverables do not pass execution/artifact checks")
    receipt = read_json(root / "stage-16/experiment_provenance.json")
    actual_outputs = {name.removeprefix("stage-11/experiment/") for name in managed_inventory(root)
                      if name.startswith("stage-11/experiment/outputs/")}
    if actual_outputs != set(receipt.get("output_hashes", {})):
        raise ValueError("Execution output inventory contains missing or unbound files")
    required = {"stage-25/paper_final.md", "stage-09/exp_plan.yaml", "compact_contract.json",
                "stage-16/experiment_provenance.json", "stage-11/experiment/outputs/metrics.json",
                "stage-11/experiment/outputs/analysis.md", *local.get("images", [])}
    required.update(p.relative_to(root).as_posix() for p in source_files(root / "stage-11/experiment"))
    if (root / "stage-25/scientific_analysis.md").is_file():
        required.add("stage-25/scientific_analysis.md")
    hashes = record.get("artifact_hashes", {})
    if not required.issubset(hashes):
        raise ValueError("Archived review does not bind every required scientific input")
    for relative, sha in hashes.items():
        path = _path(root, relative)
        if not path.is_file() or file_hash(path) != sha:
            raise ValueError(f"Reviewed artifact changed or disappeared: {relative}")
    return archive, record


def compare_reviews(previous: dict, candidate: dict) -> dict:
    old = {row["id"] for row in previous["criteria"]}
    new = {row["id"] for row in candidate["criteria"]}
    comparable = old == new and previous.get("missing_requirements", []) == candidate.get("missing_requirements", [])
    before = {row["id"] for row in previous["criteria"] if row["status"] == "met"}
    after = {row["id"] for row in candidate["criteria"] if row["status"] == "met"}
    lost_dimensions = [name for name, row in previous.get("dimensions", {}).items()
                       if row.get("status") in {"pass", "passed"}
                       and candidate.get("dimensions", {}).get(name, {}).get("status") not in {"pass", "passed"}]
    lost = sorted(before - after)
    lower_quality = before == after and candidate["quality_score"] < previous["quality_score"]
    return {"comparable": comparable, "regressed": comparable and bool(lost or lost_dimensions or lower_quality),
            "lost_criteria": lost, "lost_dimensions": lost_dimensions, "lower_quality_at_equal_coverage": lower_quality,
            "previous_met": sorted(before), "candidate_met": sorted(after),
            "previous_quality_score": previous["quality_score"], "candidate_quality_score": candidate["quality_score"]}


def capture_baseline(root: Path, state: dict, task: str, observed: dict) -> None:
    """Snapshot the complete currently valid bundle immediately before repair."""
    from researchclaw.orchestration.compact_contracts import validate_review
    check_inputs(observed)
    contract = read_json(root / "compact_contract.json")
    review = read_json(root / "compact_review.json")
    validate_review(review, contract)
    if read_json(root / "research_verification.json").get("all_satisfied") is not False:
        raise ValueError("Only an explicitly incomplete reviewed bundle can precede repair")
    archive, archive_record = bound_review(root)
    paths = managed_inventory(root)
    expected = dict(archive_record["artifact_hashes"])
    phases = {name: copy.deepcopy(state["phases"][name]) for name, _, _ in PHASES if name != "verify"}
    for name, phase in phases.items():
        if phase.get("status") != "done" or not phase.get("outputs"):
            raise ValueError(f"Cannot retain incomplete predecessor: {name}")
        for relative, sha in phase["outputs"].items():
            path = _path(root, relative)
            if not path.is_file() or file_hash(path) != sha:
                raise ValueError(f"Phase artifact changed before retention: {relative}")
            paths.add(relative)
            if relative in expected and expected[relative] != sha:
                raise ValueError("Phase and reviewed artifact hashes disagree")
            expected[relative] = sha
    receipt = read_json(root / "stage-16/experiment_provenance.json")
    for field in ("source_hashes", "output_hashes"):
        for relative, sha in receipt[field].items():
            name = "stage-11/experiment/" + relative
            if file_hash(_path(root, name)) != sha:
                raise ValueError("Execution receipt changed before retention")
            paths.add(name)
            if name in expected and expected[name] != sha:
                raise ValueError("Execution and reviewed artifact hashes disagree")
            expected[name] = sha
    paths.update({"compact_review.json", "research_verification.json", "compact_delivery_audit.json"})
    for name in ("stage-26/research_verification.json",):
        if (root / name).is_file():
            paths.add(name)
    destination = root / "research_retention" / ("baseline-" + uuid.uuid4().hex[:12])
    expected.update({name: file_hash(_path(root, name)) for name in paths if name not in expected})
    hashes = {}
    for relative in sorted(paths):
        source = _path(root, relative)
        target = destination / "artifacts" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes[relative] = file_hash(target)
        if hashes[relative] != expected[relative] or file_hash(source) != expected[relative]:
            raise ValueError("Scientific artifact changed while capturing retention bundle")
    bound_review(root)
    for relative, sha in expected.items():
        if file_hash(_path(root, relative)) != sha:
            raise ValueError("Scientific artifact changed during retention capture")
    from researchclaw.orchestration.compact_verification import verify_deliverables
    if not verify_deliverables(destination / "artifacts", contract, evidence_origin_root=root)["passed"]:
        raise ValueError("Captured scientific bundle failed deterministic verification")
    record = {"schema_version": "compact-retention-v1", "status": "ready",
              "origin_run_root": str(root.resolve()),
              "identity": state["identity"], "task_sha256": fingerprint(task),
              "contract_sha256": file_hash(root / "compact_contract.json"),
              "origin_review_archive": archive.relative_to(root).as_posix(),
              "origin_review_sha256": file_hash(archive), "review": review,
              "input_fingerprints": observed, "phases": phases, "files": hashes,
              "managed_inventory": sorted(managed_inventory(root))}
    write_json(destination / "manifest.json", record)
    write_json(root / "compact_repair_baseline.json",
               {"status": "ready", "manifest": (destination / "manifest.json").relative_to(root).as_posix(),
                "manifest_sha256": file_hash(destination / "manifest.json")})


def restore_regression(root: Path, state: dict, task: str, observed: dict, *,
                       reassess: Callable[[Path], tuple[dict, str]]) -> bool:
    """Restore an entire comparable predecessor, retaining the rejected history."""
    pointer = read_json(root / "compact_repair_baseline.json")
    if pointer.get("status") != "ready":
        return False
    manifest_path = _path(root, pointer["manifest"])
    if file_hash(manifest_path) != pointer["manifest_sha256"]:
        raise ValueError("Retained bundle manifest changed")
    baseline = read_json(manifest_path)
    check_inputs(observed)
    if baseline["identity"] != state["identity"] or baseline["input_fingerprints"] != observed:
        raise ValueError("Retained bundle research identity changed")
    if baseline["task_sha256"] != fingerprint(task) or baseline["contract_sha256"] != file_hash(root / "compact_contract.json"):
        return False
    candidate = read_json(root / "compact_review.json")
    decision = compare_reviews(baseline["review"], candidate)
    if not decision["regressed"]:
        write_json(root / "compact_repair_baseline.json", {**pointer, "status": "evaluated", "comparison": decision})
        return False
    candidate_archive, _ = bound_review(root)
    origin = _path(root, baseline["origin_review_archive"])
    if file_hash(origin) != baseline["origin_review_sha256"]:
        raise ValueError("Original retained assessment changed")
    files = {}
    for relative, sha in baseline["files"].items():
        source = manifest_path.parent / "artifacts" / relative
        if file_hash(_path(root, source.relative_to(root).as_posix())) != sha:
            raise ValueError(f"Retained snapshot changed: {relative}")
        files[relative] = source
    if reassess is not None:
        fresh, assessment_path = reassess(manifest_path)
        from researchclaw.orchestration.compact_contracts import validate_review
        validate_review(fresh, read_json(root/'compact_contract.json'))
        check_inputs(observed)
        bound_review(root)
        if file_hash(manifest_path) != pointer['manifest_sha256'] or file_hash(origin) != baseline['origin_review_sha256']:
            raise ValueError('Retained assessment changed during fresh review')
        for relative, sha in baseline['files'].items():
            if file_hash(files[relative]) != sha:
                raise ValueError(f'Retained snapshot changed during fresh review: {relative}')
        assessment_file = _path(root, assessment_path)
        assessment = read_json(assessment_file)
        if (assessment.get('review') != fresh or assessment.get('baseline_manifest_sha256') != pointer['manifest_sha256']
                or assessment.get('artifact_hashes') != baseline['files']):
            raise ValueError('Fresh assessment is not bound to this retained scientific snapshot')
        revalidation = compare_reviews(baseline['review'], fresh)
        decision['fresh_baseline_assessment'] = assessment_path
        decision['fresh_baseline_assessment_sha256'] = file_hash(_path(root, assessment_path))
        # A whole old bundle cannot be reinstated if any historical positive
        # claim fails fresh review. Keep the honest current gaps available for
        # targeted repair, even if other old findings could still be useful.
        if not revalidation['comparable'] or revalidation['lost_criteria'] or revalidation['lost_dimensions']:
            write_json(root/'compact_repair_baseline.json', {**pointer, 'status': 'not_revalidated',
                'reason': 'historical_positive_findings_not_revalidated', 'comparison': decision,
                'baseline_reassessment': revalidation})
            return False
        comparison = compare_reviews(fresh, candidate)
        decision.update(comparison)
        if not comparison['regressed']:
            write_json(root/'compact_repair_baseline.json', {**pointer, 'status': 'evaluated',
                'reason': 'no_regression_against_fresh_baseline_review', 'comparison': decision})
            return False
    for name in managed_inventory(root) - set(baseline["files"]):
        files[name] = None
    if "stage-26/paper_final_verified.md" not in baseline["files"]:
        files["stage-26/paper_final_verified.md"] = None
    retained = {key: baseline[key] for key in ("contract_sha256", "input_fingerprints",
                "origin_review_archive", "origin_review_sha256", "managed_inventory")}
    retained.update(status="retained", historical_assessment=True, scientific_status="gaps",
                    internal_verification=False, rejected_review_archive=candidate_archive.relative_to(root).as_posix(),
                    decision=decision, baseline_manifest=pointer["manifest"],
                    baseline_manifest_sha256=pointer["manifest_sha256"])
    current = copy.deepcopy(state)
    current["phases"].update(baseline["phases"])
    current["phases"]["verify"].update(status="failed", error="ContractError: " + ERROR,
                                      failure_kind="scientific_regression", historical_assessment=True)
    current["retained_scientific_bundle"] = retained
    current["attempts"][-1].update(current["phases"]["verify"], scientific_candidate_retained=False)
    stage = manifest_path.parent / ("publication-" + uuid.uuid4().hex[:10])
    verification = read_json(files["research_verification.json"])
    verification.update(all_satisfied=False, historical_assessment=True,
                        retained_from_review=baseline["origin_review_archive"])
    records = {"compact_state.json": current, "compact_retention.json": retained,
               "compact_repair_baseline.json": {**pointer, "status": "retained"},
               "research_verification.json": verification,
               "stage-26/research_verification.json": verification,
               "compact_repair_stop.json": {"reason": "scientific_revision_regressed", **retained},
               "pipeline_summary.json": {"workflow": "compact", "total_phases": 6, "last_phase": "verify",
                                         "status": "failed", "phases": current["phases"]},
               "checkpoint.json": {"run_id": current.get("run_id", read_json(root / "checkpoint.json").get("run_id")),
                                    "workflow": "compact", "last_completed_stage": 25, "last_completed_name": "report",
                                    "timestamp": time.time(), "completed_phases": [p[0] for p in PHASES[:-1]],
                                    "completed_stages": [p[2] for p in PHASES[:-1]]}}
    for relative, value in records.items():
        source = stage / relative
        write_json(source, value)
        files[relative] = source
    from researchclaw.orchestration.bundle_transaction import publish_bundle
    publish_bundle(root, files, root / "bundle_publications", metadata={"reason": "scientific_regression", **decision})
    from researchclaw.orchestration.compact_verification import verify_deliverables
    if not verify_deliverables(root, read_json(root / "compact_contract.json"))["passed"]:
        raise ValueError("Restored scientific bundle failed deterministic audit")
    bound_review(root)
    return True
