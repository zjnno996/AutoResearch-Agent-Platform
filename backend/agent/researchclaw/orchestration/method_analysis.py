"""A focused scientific method draft before compiling its executable protocol."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable
import json

from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.compact_state import fingerprint


def obligation_ids(contract: dict, formal: bool = False) -> list[str]:
    proof = [row['id'] for row in contract['requirements'] if row.get('kind') == 'proof']
    scientific = [row['id'] for row in contract['requirements'] if row.get('kind') != 'report']
    return (proof if formal and proof else scientific) or [row['id'] for row in contract['requirements']]


def draft_schema(contract: dict, formal: bool = False) -> dict:
    text = {'type': 'string', 'minLength': 1}
    obligation = {'type': 'object', 'required': ['status', 'statement', 'conditions', 'evidence', 'remaining_work'],
        'properties': {'status': {'enum': ['established', 'conditional', 'unresolved']},
            'statement': text, 'conditions': {'type': 'array', 'items': text}, 'evidence': text,
            'remaining_work': {'type': 'string'}}, 'additionalProperties': True}
    return {'type': 'object', 'required': ['scientific_analysis_markdown', 'obligations'],
        'properties': {'scientific_analysis_markdown': text,
            'obligations': {'type': 'object', 'required': obligation_ids(contract, formal),
                'properties': {rid: obligation for rid in obligation_ids(contract, formal)}, 'additionalProperties': False}},
        'additionalProperties': True}


def validate_draft(value: dict, contract: dict, formal: bool = False) -> None:
    if not isinstance(value.get('scientific_analysis_markdown'), str) or not value['scientific_analysis_markdown'].strip():
        raise ContractError('A formal draft needs the actual mathematical derivation, not an intent to derive')
    rows = value.get('obligations')
    if not isinstance(rows, dict) or set(rows) != set(obligation_ids(contract, formal)):
        raise ContractError('The formal draft must retain every required proof obligation')
    for rid, row in rows.items():
        if not isinstance(row, dict) or row.get('status') not in {'established', 'conditional', 'unresolved'}:
            raise ContractError(f'Formal obligation {rid} needs an explicit scientific status')
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in ('statement', 'evidence')):
            raise ContractError(f'Formal obligation {rid} needs its exact claim and supporting argument/source')
        conditions = row.get('conditions')
        if not isinstance(conditions, list) or any(not isinstance(item, str) or not item.strip() for item in conditions):
            raise ContractError(f'Formal obligation {rid} needs explicit conditions')
        if row['status'] == 'conditional' and not conditions:
            raise ContractError(f'Conditional obligation {rid} cannot omit its necessary assumptions')
        if not isinstance(row.get('remaining_work'), str) or (row['status'] == 'unresolved' and not row['remaining_work'].strip()):
            raise ContractError(f'Unresolved obligation {rid} must retain the missing argument')


def derive_method_draft(root: Path, topic: str, contract: dict, sources: str,
                        ask: Callable[..., dict], *, formal: bool = False,
                        observations: str = '', guidance: str = '', cache_scope: str = '') -> tuple[dict[str, Any], str]:
    """Cache only within this task and the exact current scientific-source packet.

    These are model-generated arguments and statuses, not verified theorems.
    The later independent review still reads the original task and sources.
    """
    from researchclaw.llm.provider_policy import quality_first_enabled
    draft_effort = 'xhigh' if quality_first_enabled() else 'medium'
    inputs = fingerprint({'task': topic, 'contract': contract, 'formal': formal, 'observations': observations,
                          'method_guidance': guidance,
                          'cache_scope': cache_scope, 'draft_effort': draft_effort,
                          'source_packet_sha256': hashlib.sha256(sources.encode()).hexdigest()})
    path = root / 'stage-09/method_drafts' / (inputs + '.json')
    cached = read_json(path)
    if cached:
        if cached.get('inputs') != inputs:
            raise ContractError('Formal draft cache belongs to different scientific inputs')
        validate_draft(cached.get('draft', {}), contract, formal)
        return cached['draft'], path.relative_to(root).as_posix()
    prompt = ('Original task:\n' + topic + '\nOriginal task contract:\n' + json.dumps(contract, ensure_ascii=False)
        + '\nScientific method guidance (not additional task obligations):\n' + guidance
        + '\nOriginal scientific sources and their scope:\n' + sources
        + '\nObserved supplied input schemas (not completed experiments):\n' + observations
        + '\nDeliver the complete scientific_analysis_markdown derivation and obligations keyed by the requested '
          'scientific requirement IDs. Focus only on definitions, actual operator/update or measurement equations, '
          'assumptions, proof steps, constants, quantities, units and the relation of each method to the research question. '
          'Preserve coupled signs, coordinates and normalizations. A limiting analogy is not a proved equivalence. '
          'Do not design code files, runtime commands, data schemas, numerical probes, figures or an experiment '
          'matrix in this pass; a subsequent compiler will translate this argument into that protocol. '
          'Distinguish established arguments, conditional results and unresolved claims. State necessary '
          'assumptions explicitly without pretending a conditional theorem resolves the original full scope. '
          'Give the real argument in the Markdown field, not a promise or source name alone. Numerical trends '
          'do not prove a theorem. Do not invent a proof assistant obligation or certificate. '
          'For empirical work specify estimators, data lineage, normalization and comparisons; do not claim '
          'a hypothesis is confirmed before execution. A source-defined method or analytic identity differs '
          'from an established empirical result. '
          'Each obligation has status, statement, conditions, evidence and remaining_work. Evidence points '
          'to the supplied sources and the actual argument above. These statuses are drafts for independent review.')
    value = ask(prompt, lambda value: validate_draft(value, contract, formal),
        system='Develop only the requested source-grounded scientific method and argument, preserving every scientific obligation.',
        scientific=True, response_schema=draft_schema(contract, formal), field_repair=True, native_effort=draft_effort)
    write_json(path, {'inputs': inputs, 'draft': value, 'assessment': 'unverified_scientific_draft'})
    return value, path.relative_to(root).as_posix()
