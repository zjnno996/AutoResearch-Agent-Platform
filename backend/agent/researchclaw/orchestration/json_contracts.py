"""Generation schemas for complete research artifacts; scientific checks stay separate."""
from __future__ import annotations

from typing import Any


def protocol_schema(contract: dict, references: dict | list, *, formal: bool,
                    source_anchor_ids: list[str] | None = None) -> dict[str, Any]:
    from researchclaw.orchestration.method_probes import method_source_identifiers

    text = {'type': 'string', 'minLength': 1}
    texts = {'type': 'array', 'items': text, 'minItems': 1}
    ids = [row['id'] for row in contract['requirements']]
    route = {'type': 'object', 'required': ['approach', 'artifact', 'evidence_route', 'must_preserve'],
             'properties': {'approach': text, 'artifact': text,
                 'evidence_route': {'enum': ['direct', 'transparent_surrogate', 'context_only', 'unavailable']},
                 'must_preserve': {**text, 'maxLength': 800}}, 'additionalProperties': True}
    step = {'type': 'object', 'required': ['id', 'requirement_ids', 'operation', 'inputs', 'outputs',
                                          'assumptions', 'source', 'binding'],
        'properties': {'id': text, 'requirement_ids': {'type': 'array', 'minItems': 1, 'items': {'enum': ids}},
            'operation': text, 'inputs': text, 'outputs': text,
            'assumptions': {'type': 'array', 'items': text},
            'source': {'type': 'object', 'required': ['reference', 'quote'], 'properties': {
                'reference': {'enum': method_source_identifiers(references)},
                'quote': {**text, 'maxLength': 4000}}, 'additionalProperties': False},
            'binding': {'type': 'object', 'required': ['module', 'callable'], 'properties': {
                'module': text, 'callable': text}, 'additionalProperties': False}}, 'additionalProperties': True}
    probe = {'type': 'object', 'required': ['id', 'method_step_id', 'kind', 'basis', 'wrong_alternative', 'code'],
        'properties': {'id': text, 'method_step_id': text, 'kind': {'enum': ['analytic', 'metamorphic']},
            'basis': text, 'wrong_alternative': text,
            # Include the whole body in the generation grammar. A prefix-only
            # regex admitted the observed bare signature without any checks.
            # Python syntax and nonvacuous assertions are still validated by
            # the held-witness controller, independently of this schema.
            'code': {**text, 'maxLength': 6000, 'pattern': r'^def check\(invoke\):\n[ \t]+\S[\s\S]*$'}},
        'additionalProperties': True}
    if source_anchor_ids:
        step['properties']['source'] = {'type': 'object', 'required': ['anchor_id'],
            'properties': {'anchor_id': {'enum': source_anchor_ids}}, 'additionalProperties': False}
    properties = {'methods': texts, 'datasets': {'type': 'array', 'items': text},
        'metrics': texts, 'protocol': {'type': 'object', 'required': ['command'],
            'properties': {'command': text}, 'additionalProperties': True},
        'requirement_map': {'type': 'object', 'required': ids,
            'properties': {rid: route for rid in ids}, 'additionalProperties': False},
        'method_steps': {'type': 'array', 'items': step, 'maxItems': 3},
        'method_probes': {'type': 'array', 'items': probe, 'maxItems': 6},
        'method_probe_note': {'type': 'string'}, 'dataset_policy': {**text,
            'description': 'State the supplied, synthetic or theoretical input policy; an empty datasets list does not mean missing data.'},
        'scientific_analysis_markdown': {**text} if formal else {'type': 'string'}}
    required = ['methods', 'datasets', 'dataset_policy', 'metrics', 'protocol', 'requirement_map', 'method_steps', 'method_probes']
    if formal:
        required.append('scientific_analysis_markdown')
    return {'type': 'object', 'required': required, 'properties': properties, 'additionalProperties': True}
