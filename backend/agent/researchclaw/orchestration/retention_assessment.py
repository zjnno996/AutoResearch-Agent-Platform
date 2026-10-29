"""Fresh scientific review before restoring a historically preferred bundle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from researchclaw.orchestration.artifacts import project_document, read_json, write_json
from researchclaw.orchestration.compact_contracts import ContractError, validate_review
from researchclaw.orchestration.compact_state import file_hash, fingerprint


def reassess_snapshot(root: Path, manifest_path: Path, task: str, contract: dict,
                      sources: str, ask: Callable[..., dict]) -> tuple[dict[str, Any], str]:
    """Assess immutable old evidence without inheriting its old verdicts.

    This is an independent model judgment, never an executed proof. It is
    requested only when a candidate appears to regress and restoration is
    contemplated; the ordinary six-phase success path makes no extra call.
    """
    from researchclaw.orchestration.compact_generation import canonical_review_references, source_packet
    from researchclaw.orchestration.compact_retention import _path, check_inputs
    from researchclaw.orchestration.compact_verification import verify_deliverables
    from researchclaw.orchestration.executed_evidence import executed_packet
    from researchclaw.orchestration.task_fidelity import enforce_review_consistency, guidance

    manifest_hash = file_hash(manifest_path)
    baseline = read_json(manifest_path)
    snapshot = manifest_path.parent / 'artifacts'
    def intact() -> None:
        if file_hash(manifest_path) != manifest_hash:
            raise ContractError('Retained baseline manifest changed during fresh assessment')
        check_inputs(baseline['input_fingerprints'])
        for relative, sha in baseline['files'].items():
            path = _path(snapshot, relative)
            if not path.is_file() or file_hash(path) != sha:
                raise ContractError(f'Retained scientific snapshot changed: {relative}')
    intact()
    if read_json(snapshot/'compact_contract.json') != contract:
        raise ContractError('Fresh baseline assessment belongs to another task contract')
    local = verify_deliverables(snapshot, contract, evidence_origin_root=Path(baseline.get('origin_run_root', root)))
    if not local['passed']:
        raise ContractError('Retained baseline no longer passes deterministic artifact checks')
    packet, _ = executed_packet(snapshot)
    work = snapshot/'stage-11/experiment'
    prompt = ('RAW original task:\n' + task + guidance()
        + '\nUnchanged task contract:\n' + json.dumps(contract, ensure_ascii=False)
        + '\nIndependently assess only this immutable earlier scientific bundle. Its previous verdict '
          'and saved success labels are not proof and are deliberately not supplied. Recompute your '
          'scientific judgment from the argument, actual source, measurements and receipt below. '
          'Do not infer validity from a historical status or from a later theorem with the same value. '
          'A numerical trend or tiny executable witness cannot establish an unrestricted theorem. '
          'Mark unsupported claims unmet/unverified; retain explicit assumptions and genuine limitations. '
          'Do not solve the original research task afresh or introduce optional proof assistants. '
          'Partial excerpts establish only the coverage they expose.\n'
        + '\nCOMPLETE stage-25/paper_final.md:\n' + (snapshot/'stage-25/paper_final.md').read_text(encoding='utf-8')
        + '\nActual source:\n' + source_packet(work, 90000)
        + '\nExecuted measurements:\n' + project_document(work/'outputs/metrics.json', 16000) + packet
        + '\nNumerical analysis:\n' + project_document(work/'outputs/analysis.md', 24000)
        + '\nCurrent scientific analysis of this earlier bundle:\n'
        + (project_document(snapshot/'stage-25/scientific_analysis.md', 32000)
           if (snapshot/'stage-25/scientific_analysis.md').is_file() else 'No separate scientific analysis.')
        + '\nExecutable protocol and initial unverified argument:\n' + project_document(snapshot/'stage-09/exp_plan.yaml', 48000)
        + '\nController-owned execution receipt:\n' + project_document(snapshot/'stage-16/experiment_provenance.json', 20000)
        + '\nHeld method checks, finite in scope:\n' + project_document(snapshot/'stage-16/method_probe_results.json', 12000)
        + '\nOriginal scientific sources and their coverage:\n' + sources
        + '\nDeterministic artifact audit:\n' + json.dumps(local)
        + '\nReturn a complete review with criteria covering every unchanged requirement exactly once. '
          'Each criterion needs id, status (met/unmet/unverified), reason, evidence (exact run-relative '
          'paths within this supplied bundle), evidence_route (direct/transparent_surrogate/context_only/unavailable), '
          'mechanism_match (yes/partial/no/not_applicable), artifact_match (complete/partial/missing), '
          'owner_phase (implement/execute/report), action. Return missing_requirements, quality_score '
          '(0 to 10), summary and dimensions. Dimensions task_fidelity, method_validity, source_grounding, '
          'real_execution, measurement_validity, figure_semantics, claim_integrity, reproducibility each '
          'need status (pass/gap/unverified/not_applicable) and reason. If figures are omitted, do not '
          'invent their contents. This assessment does not modify or certify the historical artifacts.')
    def validate(value: dict) -> None:
        validate_review(value, contract)
        canonical_review_references(value, snapshot, local['images'])
    review = ask(prompt, validate, system='Independently reassess immutable earlier research evidence before any restoration. '
        'Historical positive labels must be earned again; truthful withdrawal of an unsupported claim is not a regression.',
        scientific=True, images=[snapshot/name for name in local['images'][:4]])
    intact()
    review = enforce_review_consistency(review, contract)
    identity = fingerprint({'manifest': manifest_hash, 'prompt': hashlib.sha256(prompt.encode()).hexdigest(), 'review': review})
    path = root/'retention_reassessments'/(identity+'.json')
    write_json(path, {'type': 'fresh_scientific_judgment_not_proof',
        'baseline_manifest': manifest_path.relative_to(root).as_posix(), 'baseline_manifest_sha256': manifest_hash,
        'artifact_hashes': baseline['files'], 'review': review,
        'visual_review_input': {'included': local['images'][:4], 'omitted': local['images'][4:]}})
    return review, path.relative_to(root).as_posix()
