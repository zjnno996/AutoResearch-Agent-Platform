"""Compatibility facade for the modular research pipeline.

New business logic lives in steps/, ideation/, writing/ and support/.
Stage lifecycle is owned by runtime.py. Existing callers may keep importing
execute_stage, StageResult and legacy helper aliases from this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.evaluation.protocol import audit_evaluation_protocol as _audit_evaluation_protocol
from researchclaw.llm import create_llm_client
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.builtins import STAGE_HANDLERS as _STAGE_EXECUTORS
from researchclaw.pipeline.ideation.branches import (
    _generate_idea_mutation_branches,
    _generate_single_mutation_branch,
    _idea_branch_specs,
    _run_parallel,
    _synthesize_idea_mutation_branches,
)
from researchclaw.pipeline.ideation.generation import (
    _add_reflection_checkpoint,
    _apply_idea_review,
    _challenge_tree_to_markdown,
    _collect_idea_evidence_pack,
    _distill_core_ideas,
    _execute_hypothesis_gen,
    _fallback_challenge_insight_tree,
    _format_paper_for_idea_evidence,
    _generate_candidate_ideas,
    _generate_challenge_insight_tree,
    _generate_idea_review,
    _generate_role_review,
    _maybe_literature_grounded_pivot,
    _paper_authors_short,
    _refine_core_ideas,
    _safe_stage_json_from_text,
    _structured_idea_reflection,
)
from researchclaw.pipeline.ideation.memory import _write_ideation_memory_update
from researchclaw.pipeline.ideation.ranking import (
    _run_idea_tournament,
    _score_idea_block,
    _write_idea_decision_table,
)
from researchclaw.pipeline.ideation.roles import (
    _multi_perspective_generate,
    _synthesize_perspectives,
)
from researchclaw.pipeline.ideation.schema import _idea_quality_rubric, _idea_schema_instruction
from researchclaw.pipeline.ideation.text import (
    _IDEA_LIKE_HEADING_RE,
    _add_yaml_frontmatter_to_core_ideas,
    _clamp_idea_count,
    _complete_idea_set,
    _configured_idea_count,
    _default_hypotheses,
    _extract_frontmatter_list,
    _fallback_hypotheses_from_synthesis,
    _idea_count_rule,
    _idea_like_blocks,
    _idea_like_count,
    _idea_text_similarity,
    _idea_text_tokens,
    _idea_title_slug,
    _renumber_idea_blocks,
)
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.steps.analysis import _execute_result_analysis
from researchclaw.pipeline.steps.archive import _execute_knowledge_archive
from researchclaw.pipeline.steps.citations import (
    _check_citation_relevance,
    _execute_citation_verify,
    _remove_bibtex_entries,
    _remove_citations_from_text,
)
from researchclaw.pipeline.steps.decision import (
    _execute_knowledge_summary,
    _execute_research_decision,
    _parse_decision,
)
from researchclaw.pipeline.steps.design import (
    _enforce_topic_experiment_constraints,
    _execute_codebase_search,
    _execute_experiment_design,
    _extract_selected_repos,
)
from researchclaw.pipeline.steps.execution import (
    _execute_code_generation,
    _execute_experiment_run,
    _execute_iterative_refine,
    _execute_resource_planning,
    _execute_sanity_check,
)
from researchclaw.pipeline.steps.export import _execute_export_publish
from researchclaw.pipeline.steps.literature import (
    _build_local_pdf_reference_entry,
    _check_literature_sites_reachable,
    _execute_knowledge_extract,
    _execute_literature_collect,
    _execute_literature_screen,
    _execute_search_strategy,
    _execute_synthesis,
    _expand_search_queries,
    _extract_full_pdf_text,
    _extract_local_pdf_reference,
    _literature_url_reachable,
    _resolve_user_reference,
)
from researchclaw.pipeline.steps.quality_gate import _execute_quality_gate
from researchclaw.pipeline.steps.scoping import _execute_problem_decompose, _execute_topic_init
from researchclaw.pipeline.support.artifacts import (
    _collect_json_context,
    _find_discussion_dir,
    _find_prior_file,
    _load_hardware_profile,
    _parse_jsonl_rows,
    _read_literature_bib,
    _read_prior_artifact,
    _safe_filename,
    _utcnow_iso,
    _write_jsonl,
    _write_stage_meta,
)
from researchclaw.pipeline.support.citations import (
    _CANONICAL_BASELINE_BIB,
    _augment_canonical_citations,
    _build_deterministic_citation_context,
    _sanitize_bibtex_for_latex,
)
from researchclaw.pipeline.support.claims import (
    _baseline_only_outline_violations,
    _build_claim_integrity_report,
    _build_research_readiness,
    _claim_boundary_instruction,
    _scope_locked_reproduction_can_proceed,
)
from researchclaw.pipeline.support.context import (
    _METACLAW_SKILLS_DIR,
    _build_context_preamble,
    _build_fallback_queries,
    _chat_with_prompt,
    _get_evolution_overlay,
    _load_human_feedback,
)
from researchclaw.pipeline.support.domain import (
    _CHINESE_ENGLISH_DOMAIN_MAP,
    _DOMAIN_KEYWORDS,
    _STOP_WORDS,
    _detect_domain,
    _extract_english_from_mixed,
    _extract_topic_keywords,
    _is_ml_domain,
    _topic_constraint_block,
)
from researchclaw.pipeline.support.experiment_evidence import (
    _build_experiment_fact_contract,
    _collect_experiment_evidence,
    _collect_experiment_results,
    _collect_raw_experiment_metrics,
    _detect_runtime_issues,
)
from researchclaw.pipeline.support.parsing import (
    _extract_code_block,
    _extract_multi_file_blocks,
    _extract_yaml_block,
    _parse_metrics_from_stdout,
    _parse_yaml_dict_from_llm,
    _safe_json_loads,
)
from researchclaw.pipeline.support.research_memory import (
    _load_experiment_memory,
    _load_ideation_memory,
    _resolve_shared_results_path,
    _write_experiment_memory_update,
)
from researchclaw.pipeline.support.settings import _create_idea_judge_llm, _load_root_arc_section
from researchclaw.pipeline.support.templates import (
    _default_paper_outline,
    _default_quality_report,
    _extract_paper_title,
    _generate_neurips_checklist,
)
from researchclaw.pipeline.support.visuals import _run_chart_generation
from researchclaw.pipeline.writing.draft import _execute_paper_draft, _paper_draft_timeout_sec
from researchclaw.pipeline.writing.fallbacks import (
    _build_deterministic_paper_draft,
    _build_deterministic_paper_revision,
)
from researchclaw.pipeline.writing.figures import (
    _ACADEMIC_STYLE_SUFFIX,
    _BALANCE_SECTIONS,
    _BULLET_LENIENT_SECTIONS,
    _FIGURE_PROMPT_RE,
    _FIGURE_TYPE_STYLE,
    _extract_figure_prompts,
    _generate_framework_diagram_prompt,
    _read_figure_manifest,
    _render_figure_prompts,
)
from researchclaw.pipeline.writing.latex import _generate_latex_package
from researchclaw.pipeline.writing.outline import _execute_paper_outline
from researchclaw.pipeline.writing.quality import (
    _check_ablation_effectiveness,
    _detect_result_contradictions,
    _review_compiled_pdf,
    _validate_draft_quality,
)
from researchclaw.pipeline.writing.review import _execute_paper_revision, _execute_peer_review
from researchclaw.pipeline.writing.sanitize import (
    _enforce_engineering_report_boundary,
    _remove_missing_markdown_figures,
    _repair_markdown_figure_references,
    _sanitize_fabricated_data,
)
from researchclaw.pipeline.writing.sections import _write_paper_sections
from researchclaw.prompts import PromptManager


def _runtime_bindings():
    from researchclaw.pipeline.registry import StageRegistry
    from researchclaw.pipeline.runtime import StageBindings
    return StageBindings(
        registry=StageRegistry(_STAGE_EXECUTORS), read_artifact=_read_prior_artifact,
        write_metadata=_write_stage_meta, load_feedback=_load_human_feedback,
        llm_client=LLMClient, client_factory=create_llm_client, audit_protocol=_audit_evaluation_protocol,
    )


def _execute_stage_impl(stage: Stage, **kwargs) -> StageResult:
    from researchclaw.pipeline.runtime import execute_stage_impl
    return execute_stage_impl(stage, bindings=_runtime_bindings(), **kwargs)


def execute_stage(
    stage: Stage, *, run_dir: Path, run_id: str, config: RCConfig,
    adapters: AdapterBundle, auto_approve_gates: bool = False,
) -> StageResult:
    """Compatibility entry point backed by the shared stage runtime."""
    from researchclaw.pipeline.runtime import execute_stage as run_stage
    return run_stage(stage, run_dir=run_dir, run_id=run_id, config=config, adapters=adapters,
                     auto_approve_gates=auto_approve_gates, bindings=_runtime_bindings())
