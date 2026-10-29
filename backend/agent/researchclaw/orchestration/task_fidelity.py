"""Bind requested methods to their evidence without adding model checkpoints.

Adapted from AutoSciRub's instruction quotations, evidence routes and core
completion checks. These checks detect contradictory declarations; they do
not establish scientific truth without the independent artifact review.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from researchclaw.orchestration.compact_contracts import ContractError


SCHEMA = 'compact-task-contract-v3'
SCHEMAS = {'compact-task-contract-v2', SCHEMA}
ROUTES = {'direct', 'transparent_surrogate', 'context_only', 'unavailable'}
MECHANISMS = {'yes', 'partial', 'no', 'not_applicable'}
ARTIFACTS = {'complete', 'partial', 'missing'}


def instruction_sources(topic: str) -> dict[str, str]:
    """Controller-authored, bounded exact source fragments; no model summaries."""
    result = {}
    for line in topic.splitlines():
        remainder = line.strip()
        while remainder:
            end = min(1200, len(remainder))
            if end < len(remainder):
                boundary = remainder.rfind(' ', 600, end)
                if boundary > 0:
                    end = boundary
            part = remainder[:end].strip()
            result[f'T{len(result)+1}'] = part
            remainder = remainder[end:].strip()
    return result


def induction_context(topic: str) -> str:
    return ('Extract the scientific task contract from the RAW ORIGINAL INSTRUCTION below. '
            'Only these source fragments define required work. Source IDs are exact raw-text anchors, '
            'not suggested methods. Use instruction_sources to cite IDs; the controller fills the original '
            'quotation. Data and literature may explain feasibility but cannot add a requested experiment, '
            'comparison, ablation, seed policy or certificate. Keep necessary scientific preconditions in '
            'constraints, and optional improvements out of requirements. Preserve every explicit input, '
            'output, named method, condition, numerical setting and deliverable.\n'
            'RAW ORIGINAL INSTRUCTION SOURCE FRAGMENTS:\n'
            + json.dumps(instruction_sources(topic), ensure_ascii=False))


INSTRUCTION_CONTRACT_SYSTEM = (
    'You extract a faithful task contract from the raw instruction. '
    'This is a scoped transcription task, not the research solution. '
    'Do not solve equations, prove convergence, design experiments, review papers, or choose algorithms here. '
    'Record requested work as requirements; leave scientific decisions to the subsequent planning phase.'
)


def instruction_contract_prompt(topic: str) -> str:
    """Keep the initial contract independent of data findings and source analysis.

    AutoSciRub extracts its initial rubric from the raw instruction alone;
    InnoClaw confines each worker to its assigned subtask. Data and complete
    source evidence remain available to planning, not to task-scope induction.
    """
    return (induction_context(topic)
        + '\nReturn {"requirements":[{"id":"R1","requirement":"explicit obligation",'
          '"acceptance":"observable completion condition","kind":"experiment|proof|report|data",'
          '"instruction_sources":["T1"]}],'
          '"constraints":["shared restriction explicitly stated in the instruction"],'
          '"unresolved_details":["detail not specified; resolve during planning"],'
          '"source_queries":["short public scientific search query"]}. '
          'Use one criterion per independent scientific question or deliverable, not per repeated implementation detail. '
          'Enumerate EVERY explicit scientific and delivery requirement. Combine repeated wording only; preserve '
          'each requested comparison, named method, condition, proof, numerical setting, input, output and figure. '
          'Anchor each requirement to existing instruction_sources IDs from the RAW fragments above. '
          'The controller inserts exact original quotations; do not retype them. '
          'Do not answer the research question, derive a method, evaluate feasibility, inspect literature, '
          'or fill unspecified scientific assumptions in this extraction pass. '
          'A request to derive or prove requires a complete rigorous written argument later; a proof assistant '
          'or symbolic certificate is required only if the original instruction explicitly requests one. '
          'The controller has separately saved input profiles and reference availability for planning. '
          'Do not claim missing inputs or unavailable sources from their absence in this extraction prompt. '
          'Do not add optional datasets, baselines, ablations, seed policies, innovation tournaments or other '
          'research obligations. Keep unstated choices in unresolved_details, not in required work. '
          'For a named framework or underspecified scientific theory, suggest up to two public scientific '
          'source queries of at most 96 characters using the exact task names/acronyms and complementary '
          'background terms. Queries are retrieval clues, not new requirements. Never include workspace paths '
          'or benchmark/evaluation names. Use [] for self-contained tasks or when external sources are forbidden. '
          'Return the complete task contract as JSON, not a plan or a result.')


def guidance() -> str:
    return ('\nTask fidelity: the RAW original instruction is the authority for what must be accomplished. '
            'Method guidance and background sources do not add unrelated mandatory comparisons, ablations, '
            'datasets or certificates. Preserve the requested input-to-method-to-output transformation, '
            'named mechanism, conditions, units and artifact forms. When the task asks to derive or reconcile '
            'source observations, supplied published/combined outputs are comparison references; copying or '
            'reformatting them does not establish the requested derivation. When the task explicitly asks to '
            'analyze an existing published product, that product may be the primary input. Record its role. '
            'An adjacent algorithm with a new label, a pooled result replacing requested comparisons, '
            'simulated observations replacing supplied measurements, or a limitation paragraph replacing '
            'feasible required work does not close the original obligation. A transparent reconstruction '
            'can close it only when the required mechanism, conditions, metrics and scientific question '
            'are preserved. Use public source code and documented protocols to understand the actual method '
            'before proposing a generic adjacent substitute; record versions and deviations, and verify the '
            'executed mechanism. Published values are diagnostics, never targets for tuning filters or parameters.\n')


def validate_instruction_quotes(value: dict[str, Any], topic: str) -> None:
    sources = instruction_sources(topic)
    for row in value['requirements']:
        if 'instruction_sources' in row:
            ids = row['instruction_sources']
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 32
                    or any(not isinstance(key, str) or key not in sources for key in ids)):
                raise ContractError(f'{row["id"]} instruction_sources must cite existing RAW source IDs: {list(sources)}')
            continue
        quotes = row.get('instruction_quotes')
        if (not isinstance(quotes, list) or not 1 <= len(quotes) <= 8
                or any(not isinstance(quote, str) or not quote.strip() or len(quote) > 1600 for quote in quotes)):
            raise ContractError(f'{row["id"]} needs instruction_quotes containing exact short clauses from the RAW original task')
        if any(quote not in topic for quote in quotes):
            raise ContractError(f'{row["id"]} instruction_quotes must be literal RAW original task text; '
                                'do not quote method guidance, data findings or paraphrases')


def seal_contract(value: dict[str, Any], topic: str) -> dict[str, Any]:
    validate_instruction_quotes(value, topic)
    value = deepcopy(value)
    sources = instruction_sources(topic)
    for row in value['requirements']:
        if 'instruction_sources' in row:
            row['instruction_quotes'] = [sources[key] for key in row['instruction_sources']]
    value.update(schema_version=SCHEMA, raw_task_sha256=hashlib.sha256(topic.encode()).hexdigest())
    return value


def validate_task_identity(contract: dict[str, Any], topic: str) -> None:
    if contract.get('schema_version') not in SCHEMAS:
        return
    if contract.get('raw_task_sha256') != hashlib.sha256(topic.encode()).hexdigest():
        raise ContractError('The task contract is bound to a different RAW original instruction')
    validate_instruction_quotes(contract, topic)
    sources = instruction_sources(topic)
    for row in contract['requirements']:
        if 'instruction_sources' in row and row.get('instruction_quotes') != [sources[key] for key in row['instruction_sources']]:
            raise ContractError(f'{row["id"]} source quotation changed after the instruction contract was sealed')


def validate_plan_routes(value: dict[str, Any], contract: dict[str, Any]) -> None:
    if contract.get('schema_version') not in SCHEMAS:
        return
    for requirement in contract['requirements']:
        row = value['requirement_map'][requirement['id']]
        if row.get('evidence_route') not in ROUTES:
            raise ContractError(f'{requirement["id"]} needs evidence_route: direct, transparent_surrogate, context_only or unavailable')
        signature = row.get('must_preserve')
        if not isinstance(signature, str) or not signature.strip() or len(signature) > 800:
            raise ContractError(f'{requirement["id"]} needs a concise must_preserve scientific method/condition/artifact signature')


def validate_review_routes(value: dict[str, Any], contract: dict[str, Any]) -> None:
    if contract.get('schema_version') not in SCHEMAS:
        return
    for row in value['criteria']:
        if (row.get('evidence_route') not in ROUTES or row.get('mechanism_match') not in MECHANISMS
                or row.get('artifact_match') not in ARTIFACTS):
            raise ContractError(f'{row["id"]} needs evidence_route, mechanism_match (yes/partial/no/not_applicable), '
                                'and artifact_match (complete/partial/missing) based on inspected evidence')


def enforce_review_consistency(value: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Keep a high score or optimistic verdict from overriding admitted gaps."""
    if contract.get('schema_version') not in SCHEMAS:
        return value
    validate_review_routes(value, contract)
    review = deepcopy(value)
    by_id = {row['id']: row for row in contract['requirements']}
    for row in review['criteria']:
        if row['status'] != 'met':
            continue
        gaps = []
        if row['evidence_route'] in {'context_only', 'unavailable'}:
            gaps.append(f'evidence route is {row["evidence_route"]}')
        if row['mechanism_match'] in {'partial', 'no'}:
            gaps.append(f'required scientific mechanism match is {row["mechanism_match"]}')
        if row['mechanism_match'] == 'not_applicable' and by_id[row['id']].get('kind') != 'report':
            gaps.append('the requested scientific/data/proof operation cannot be marked not_applicable')
        if row['artifact_match'] != 'complete':
            gaps.append(f'required artifact coverage is {row["artifact_match"]}')
        if gaps:
            row.update(model_status='met', status='unverified', controller_consistency_gaps=gaps)
            row['reason'] += ' Controller consistency check: ' + '; '.join(gaps) + '.'
            owner = 'report' if by_id[row['id']].get('kind') in {'proof', 'report'} else 'implement'
            row['owner_phase'] = owner
            row['action'] = 'Close the original obligation with the required method and complete evidence: ' + '; '.join(gaps)
    return review
