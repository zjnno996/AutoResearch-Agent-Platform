"""Built-in stage implementations, registered independently of lifecycle."""

from typing import Callable

from researchclaw.pipeline.ideation.generation import _execute_hypothesis_gen
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.registry import StageRegistry
from researchclaw.pipeline.stages import Stage
from researchclaw.pipeline.steps.analysis import _execute_result_analysis
from researchclaw.pipeline.steps.archive import _execute_knowledge_archive
from researchclaw.pipeline.steps.citations import _execute_citation_verify
from researchclaw.pipeline.steps.decision import (
    _execute_knowledge_summary,
    _execute_research_decision,
)
from researchclaw.pipeline.steps.design import _execute_codebase_search, _execute_experiment_design
from researchclaw.pipeline.steps.execution import (
    _execute_code_generation,
    _execute_experiment_run,
    _execute_iterative_refine,
    _execute_resource_planning,
    _execute_sanity_check,
)
from researchclaw.pipeline.steps.export import _execute_export_publish
from researchclaw.pipeline.steps.literature import (
    _execute_knowledge_extract,
    _execute_literature_collect,
    _execute_literature_screen,
    _execute_search_strategy,
    _execute_synthesis,
)
from researchclaw.pipeline.steps.quality_gate import _execute_quality_gate
from researchclaw.pipeline.steps.scoping import _execute_problem_decompose, _execute_topic_init
from researchclaw.pipeline.writing.draft import _execute_paper_draft
from researchclaw.pipeline.writing.outline import _execute_paper_outline
from researchclaw.pipeline.writing.review import _execute_paper_revision, _execute_peer_review

STAGE_HANDLERS: dict[Stage, Callable[..., StageResult]] = {
    Stage.TOPIC_INIT: _execute_topic_init,
    Stage.PROBLEM_DECOMPOSE: _execute_problem_decompose,
    Stage.SEARCH_STRATEGY: _execute_search_strategy,
    Stage.LITERATURE_COLLECT: _execute_literature_collect,
    Stage.LITERATURE_SCREEN: _execute_literature_screen,
    Stage.KNOWLEDGE_EXTRACT: _execute_knowledge_extract,
    Stage.SYNTHESIS: _execute_synthesis,
    Stage.HYPOTHESIS_GEN: _execute_hypothesis_gen,
    Stage.EXPERIMENT_DESIGN: _execute_experiment_design,
    Stage.CODEBASE_SEARCH: _execute_codebase_search,
    Stage.CODE_GENERATION: _execute_code_generation,
    Stage.SANITY_CHECK: _execute_sanity_check,
    Stage.RESOURCE_PLANNING: _execute_resource_planning,
    Stage.EXPERIMENT_RUN: _execute_experiment_run,
    Stage.ITERATIVE_REFINE: _execute_iterative_refine,
    Stage.RESULT_ANALYSIS: _execute_result_analysis,
    Stage.RESEARCH_DECISION: _execute_research_decision,
    Stage.KNOWLEDGE_SUMMARY: _execute_knowledge_summary,
    Stage.PAPER_OUTLINE: _execute_paper_outline,
    Stage.PAPER_DRAFT: _execute_paper_draft,
    Stage.PEER_REVIEW: _execute_peer_review,
    Stage.PAPER_REVISION: _execute_paper_revision,
    Stage.QUALITY_GATE: _execute_quality_gate,
    Stage.KNOWLEDGE_ARCHIVE: _execute_knowledge_archive,
    Stage.EXPORT_PUBLISH: _execute_export_publish,
    Stage.CITATION_VERIFY: _execute_citation_verify,
}


def default_registry() -> StageRegistry:
    """Build an invocation's registry over the built-in implementation mapping."""
    return StageRegistry(STAGE_HANDLERS)
