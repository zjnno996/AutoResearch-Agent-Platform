"""Retain a reviewed partial report when a later revision candidate is rejected."""
from pathlib import Path

from researchclaw.orchestration.artifacts import read_json
from researchclaw.orchestration.compact_contracts import validate_review
from researchclaw.orchestration.compact_generation import source_files
from researchclaw.orchestration.compact_state import file_hash
from researchclaw.orchestration.compact_verification import verify_deliverables


def retained_review_audit(root: Path) -> dict:
    root = root.resolve()
    current = read_json(root / 'compact_review.json')
    contract = read_json(root / 'compact_contract.json')
    try:
        validate_review(current, contract)
        if read_json(root / 'research_verification.json').get('all_satisfied') is not False:
            raise ValueError('No completed scientific assessment with unresolved gaps')
        audit = verify_deliverables(root, contract)
        if not audit['passed']:
            raise ValueError('Current deliverables do not pass execution/artifact checks')
        from researchclaw.orchestration.compact_retention import bound_review
        archive, archived = bound_review(root, audit)
        hashes = archived.get('artifact_hashes', {})
        required = {'stage-25/paper_final.md', 'stage-09/exp_plan.yaml', 'compact_contract.json',
                    'stage-16/experiment_provenance.json', 'stage-11/experiment/outputs/metrics.json',
                    'stage-11/experiment/outputs/analysis.md', *audit.get('images', [])}
        required.update(str(p.relative_to(root)) for p in source_files(root / 'stage-11/experiment'))
        science = root / 'stage-25/scientific_analysis.md'
        if science.is_file():
            required.add(str(science.relative_to(root)))
        if not required.issubset(hashes):
            raise ValueError('Archived review does not bind every required scientific input')
        for relative, sha in hashes.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file() or file_hash(path) != sha:
                raise ValueError(f'Reviewed artifact changed or disappeared: {relative}')
        return {'eligible': True, 'review_archive': str(archive.relative_to(root)),
                'artifact_hashes_unchanged': True, 'scientific_status': 'gaps',
                'internal_verification': False,
                'reason': 'Rejected revision did not change the previously reviewed, valid partial deliverables'}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {'eligible': False, 'reason': str(exc)}
