"""Installed skills are pinned, task-routed and bounded during real loading."""
from researchclaw.skills import SkillRegistry


def test_curated_library_has_all_five_versioned_sources():
    registry = SkillRegistry()
    assert len(registry.skills) == 210
    assert set(registry.sources) == {'scientific', 'autoscirub', 'anthropic', 'superpowers', 'trailofbits'}
    assert len({skill.id for skill in registry.skills}) == 210
    assert registry.verify()['ok']


def test_scientific_coding_and_runtime_failures_route_different_guidance():
    registry = SkillRegistry()
    coding = {skill.id for skill in registry.select(13, 'Implement a numerical operator algorithm with invariant tests')}
    failure = {skill.id for skill in registry.select(13, 'Traceback error and regression after fixing a Python numerical solver')}
    assert 'trailofbits/property-based-testing' in coding
    assert 'superpowers/systematic-debugging' in failure
    assert 'anthropic/frontend-design' not in coding
    assert not registry.select(13, 'numerical operator', limit=0)


def test_formal_tasks_do_not_inherit_unrequested_statistical_or_lean_workflows():
    registry = SkillRegistry()
    selected = {skill.name for skill in registry.select(9, 'Prove deterministic convergence of an operator algorithm')}
    assert 'statistical-analysis' not in selected and 'writing-lean-proofs' not in selected
    assert 'property-based-testing' in selected


def test_relevant_supporting_guides_are_loaded_and_recorded_within_the_budget():
    registry = SkillRegistry()
    prompt, records = registry.prompt(13, 'Implement a numerical operator algorithm with invariant tests', max_chars=7000)
    assert prompt and len(prompt) <= 7000
    assert records and all(row['id'] and row['revision'] and row['sha256'] for row in records)
    assert any(row['resources'] for row in records)
    assert registry.prompt(13, 'Python', max_chars=0) == ('', [])
