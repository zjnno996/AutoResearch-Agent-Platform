"""Read-only product view of the installed research engine and skill routing."""
from pathlib import Path
import os
import sys


def public_run_health(record: dict) -> dict:
    """Whitelist progress metadata; provider bodies and reasoning never enter UI."""
    return {key: record[key] for key in ('phase', 'status', 'observed_at', 'phase_elapsed_sec',
            'request_elapsed_sec', 'request_active', 'provider_silence_sec', 'semantic_chars', 'model') if key in record}


def capabilities(topic: str = '', stage: int = 9) -> dict:
    if type(stage) is not int or not 1 <= stage <= 26:
        raise ValueError('Skill preview stage must be in 1..26')
    if not isinstance(topic, str) or len(topic) > 8000:
        raise ValueError('Skill preview topic must be at most 8000 characters')
    project = Path(__file__).resolve().parents[2]
    agent = project / 'backend/agent'
    if str(agent) not in sys.path:
        sys.path.insert(0, str(agent))
    from researchclaw.skills import SkillRegistry
    from researchclaw.orchestration.compact_state import PHASES
    import yaml
    config = yaml.safe_load((project / 'config.arc.yaml').read_text()) or {}
    registry = SkillRegistry()
    # Catalogues expose provenance, not server filesystem or private settings.
    catalog = [{key: value for key, value in row.items() if key != 'path'} for row in registry.catalog()]
    return {'schema_version': 'emnets-capabilities-v1', 'workflow': 'compact',
            'phases': [{'id': name, 'first_stage': first, 'stage': last} for name, first, last in PHASES],
            'model': os.environ.get('EMNETS_MODEL') or config.get('llm', {}).get('primary_model', ''),
            'model_locked': os.environ.get('EMNETS_PROVIDER_LOCK', '').lower() in {'1', 'true', 'yes'},
            'max_skills_per_stage': config.get('research_engine', {}).get('max_skills_per_stage', 3),
            'skill_count': len(catalog), 'skills': catalog, 'sources': registry.lock.get('sources', []),
            'preview': [skill.id for skill in registry.select(stage, topic)], 'preview_stage': stage,
            'resource_loading': 'bounded_relevant_references', 'scripts_auto_executed': False}
