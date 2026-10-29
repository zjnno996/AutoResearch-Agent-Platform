"""Writing sections components."""

from __future__ import annotations

import logging
from pathlib import Path
import re

from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.support.claims import _baseline_only_outline_violations
from researchclaw.pipeline.support.context import _chat_with_prompt, _get_evolution_overlay
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _write_paper_sections(
    *,
    llm: LLMClient,
    pm: PromptManager,
    run_dir: Path | None = None,
    preamble: str,
    topic_constraint: str,
    exp_metrics_instruction: str,
    citation_instruction: str,
    outline: str,
    model_name: str = "",
    discussion_ablation: str = "",
    figure_prompt_instruction: str = "",
    target_conference: str = "",
    target_pages: int = 0,
) -> str:
    """Write a conference-grade paper in 3 sequential LLM calls.

    Call 1: Title + Abstract + Introduction + Related Work
    Call 2: Method + Experiments (with full experiment data)
    Call 3: Results + Discussion + Limitations + Conclusion

    Each call receives prior sections for coherence.
    """
    # Render writing_structure block for injection
    try:
        _writing_structure = pm.block("writing_structure")
    except (KeyError, Exception):  # noqa: BLE001
        _writing_structure = ""

    _overlay = _get_evolution_overlay(run_dir, "paper_draft")
    system = pm.for_stage(
        "paper_draft",
        evolution_overlay=_overlay,
        preamble=preamble,
        topic_constraint=topic_constraint,
        exp_metrics_instruction=exp_metrics_instruction,
        citation_instruction=citation_instruction,
        writing_structure=_writing_structure,
        outline=outline,
        discussion_ablation=discussion_ablation,
        figure_prompt_instruction=figure_prompt_instruction,
    ).system

    sections: list[str] = []
    _venue_name = "IEEE conference" if target_conference.lower().startswith("ieee") else "NeurIPS/ICML"
    _page_instruction = ""
    if target_pages > 0:
        _page_instruction = (
            f"MANUSCRIPT LENGTH TARGET: Produce a complete {_venue_name} manuscript that compiles "
            f"to approximately {target_pages} two-column pages including references. Aim for "
            "roughly 5,500-7,000 substantive words when figures and tables are limited. "
            "Do not pad with repetition: use the space for method derivation, implementation "
            "details, evaluation protocol, reproducibility, and threats to validity.\n\n"
        )

    # --- R4-3: Title guidelines and abstract structure ---
    try:
        title_guidelines = pm.block("title_guidelines")
    except (KeyError, Exception):  # noqa: BLE001
        title_guidelines = ""
    try:
        abstract_structure = pm.block("abstract_structure")
    except (KeyError, Exception):  # noqa: BLE001
        abstract_structure = ""

    # IMP-20/25/31/24: Academic style, narrative, anti-hedging, anti-repetition
    try:
        academic_style_guide = pm.block("academic_style_guide")
    except (KeyError, Exception):  # noqa: BLE001
        academic_style_guide = ""
    try:
        narrative_writing_rules = pm.block("narrative_writing_rules")
    except (KeyError, Exception):  # noqa: BLE001
        narrative_writing_rules = ""
    try:
        anti_hedging_rules = pm.block("anti_hedging_rules")
    except (KeyError, Exception):  # noqa: BLE001
        anti_hedging_rules = ""
    try:
        anti_repetition_rules = pm.block("anti_repetition_rules")
    except (KeyError, Exception):  # noqa: BLE001
        anti_repetition_rules = ""

    def _trim_to_expected_section(text: str, headings: tuple[str, ...]) -> str:
        """Remove prior sections that a continuation call echoed verbatim."""
        heading_group = "|".join(re.escape(name) for name in headings)
        match = re.search(
            rf"(?im)^##\s*(?:\d+[.、]?\s*)?(?:{heading_group})\b.*$",
            text,
        )
        return text[match.start():].strip() if match else text.strip()

    def _truncate_before_section(text: str, headings: tuple[str, ...]) -> str:
        """Remove later sections that a scoped generation call wrote prematurely.

        Qwen continuation endpoints occasionally return a complete paper for every
        call.  Keeping those unsolicited later sections makes the final manuscript
        contain two or three copies of Method, Results, and Discussion.
        """
        heading_group = "|".join(re.escape(name) for name in headings)
        match = re.search(
            rf"(?im)^##\s*(?:\d+[.、]?\s*)?(?:{heading_group})\b.*$",
            text,
        )
        return text[:match.start()].rstrip() if match else text.strip()

    _baseline_only = bool(
        run_dir is not None
        and _baseline_only_outline_violations(run_dir, "## Method Name Proposal")
    )
    _title_rule = (
        "1. **Title** (HARD RULE: MUST be 14 words or fewer. This is baseline-only "
        "reproduction work: use an unbranded descriptive title and do not create any "
        "method/protocol/framework name or acronym.)\n"
        if _baseline_only else
        "1. **Title** (HARD RULE: MUST be 14 words or fewer. Create a catchy method name "
        "first, then build the title: 'MethodName: Subtitle'. If your title exceeds 14 words, "
        "it will be automatically rejected. NEVER use 'Untitled Paper'.)\n"
    )

    # --- Call 1: Title + Abstract + Introduction + Related Work ---
    call1_user = (
        f"{preamble}\n\n"
        f"{topic_constraint}"
        f"{citation_instruction}\n\n"
        f"{title_guidelines}\n\n"
        f"{academic_style_guide}\n"
        f"{narrative_writing_rules}\n"
        f"{anti_hedging_rules}\n"
        f"{anti_repetition_rules}\n\n"
        f"{_page_instruction}"
        f"Write the following sections of a {_venue_name}-quality paper in markdown. "
        "Follow the LENGTH REQUIREMENTS strictly:\n\n"
        f"{_title_rule}"
        f"2. **Abstract** (150-220 words — HARD LIMIT. Do NOT exceed 220 words. "
        f"Do NOT include raw metric paths or 16-digit decimals.){abstract_structure}\n"
        "3. **Introduction** (800-1000 words): real-world motivation, problem statement, "
        "research gap analysis with citations, method overview, 3-4 contributions as bullet points, "
        "paper organization paragraph. MUST cite 8-12 references.\n"
        "   **TEASER FIGURE (MANDATORY)**: Include a teaser figure (Figure 1) right after the "
        "first introductory paragraph using a `<!-- FIGURE_PROMPT ... -->` block. This figure "
        "should be a high-level conceptual illustration showing the core idea/motivation at a "
        "glance — it conveys the key insight without requiring method details. Use figure_type: "
        "concept_illustration, section: Introduction.\n"
        "4. **Related Work** (600-800 words): organized into 3-4 thematic subsections, each discussing "
        "4-5 papers with proper citations. Compare approaches, identify limitations, position this work.\n\n"
        f"{figure_prompt_instruction}\n\n"
        f"Outline:\n{outline}\n\n"
        "Output markdown with ## headers. Do NOT include a References section.\n"
        "IMPORTANT: Start DIRECTLY with '## Title'. Do NOT include any preamble, "
        "data verification, condition listing, or metric enumeration before the title. "
        "The paper should read like a published manuscript, not a data report."
    )
    # R14-1: Higher token limit for reasoning models
    _paper_max_tokens = 12000
    if any(model_name.startswith(p) for p in ("gpt-5", "o3", "o4")):
        _paper_max_tokens = 24000

    # T3.5: Retry once on failure, use placeholder if still fails
    try:
        resp1 = _chat_with_prompt(llm, system, call1_user, max_tokens=_paper_max_tokens, retries=1)
        part1 = _truncate_before_section(
            resp1.content.strip(),
            ("Method", "Methodology", "Experiments", "Results", "Discussion", "Limitations", "Conclusion"),
        )
    except Exception:  # noqa: BLE001
        logger.error("Stage 17: Part 1 LLM call failed after retry — using placeholder")
        part1 = (
            "## Title\n[PLACEHOLDER — LLM call failed]\n\n"
            "## Abstract\n[This section could not be generated due to an LLM error. "
            "Please regenerate this stage.]\n\n"
            "## Introduction\n[PLACEHOLDER]\n\n"
            "## Related Work\n[PLACEHOLDER]"
        )
    sections.append(part1)
    logger.info("Stage 17: Part 1 (Title+Abstract+Intro+Related Work) — %d chars", len(part1))

    # --- Call 2: Method + Experiments ---
    call2_user = (
        f"{preamble}\n\n"
        f"{topic_constraint}"
        f"{_page_instruction}"
        f"{exp_metrics_instruction}\n\n"
        f"{narrative_writing_rules}\n"
        f"{anti_hedging_rules}\n\n"
        # IMP-21: Citation instruction for Method + Experiments
        "CITATION REQUIREMENT: The Method section MUST cite at least 3-5 related "
        "technical papers (foundations your method builds on). The Experiments section "
        "MUST cite baseline method papers. Use [cite_key] syntax.\n"
        f"{citation_instruction}\n\n"
        "You are continuing a paper. The sections written so far are:\n\n"
        f"---\n{part1}\n---\n\n"
        "Now write the next sections, maintaining consistency with the above:\n\n"
        "5. **Method** (1000-1500 words): formal problem definition with mathematical notation "
        "($x$, $\\theta$, etc.), detailed algorithm description with equations, step-by-step procedure, "
        "complexity analysis, design rationale for key choices. Include algorithm pseudocode if applicable. "
        "Write as FLOWING PROSE — do NOT use bullet-point lists for method components.\n"
        "   **METHOD FIGURES (MANDATORY)**: Include at least TWO `<!-- FIGURE_PROMPT ... -->` blocks:\n"
        "   (a) A **framework / architecture overview** (figure_type: architecture_diagram or "
        "pipeline_overview) showing the full system pipeline.\n"
        "   (b) A **method detail figure** (figure_type: method_flowchart or comparison_illustration) "
        "illustrating a key algorithmic step or component.\n"
        "6. **Experiments** (800-1200 words): detailed experimental setup, datasets with statistics "
        "(size, splits, features), all baselines and their implementations, hyperparameter settings "
        "in a markdown table, evaluation metrics with mathematical definitions, hardware and runtime info.\n"
        "METHOD NAMES IN TABLES: Use SHORT abbreviations (4-8 chars) for method names "
        "in tables. Define abbreviation mappings in a footnote. "
        "NEVER put method names longer than 20 characters in table cells.\n\n"
        f"{figure_prompt_instruction}\n\n"
        f"Outline:\n{outline}\n\n"
        "Output markdown with ## headers. Continue from where Part 1 ended."
    )
    try:
        resp2 = _chat_with_prompt(llm, system, call2_user, max_tokens=_paper_max_tokens, retries=1)
        part2 = _trim_to_expected_section(resp2.content, ("Method", "Methodology"))
        part2 = _truncate_before_section(part2, ("Results", "Discussion", "Limitations", "Conclusion"))
    except Exception:  # noqa: BLE001
        logger.error("Stage 17: Part 2 LLM call failed after retry — using placeholder")
        part2 = (
            "## Method\n[PLACEHOLDER — LLM call failed. Please regenerate this stage.]\n\n"
            "## Experiments\n[PLACEHOLDER]"
        )
    sections.append(part2)
    logger.info("Stage 17: Part 2 (Method+Experiments) — %d chars", len(part2))

    # --- Call 3: Results + Discussion + Limitations + Conclusion ---
    call3_user = (
        f"{preamble}\n\n"
        f"{topic_constraint}"
        f"{_page_instruction}"
        f"{exp_metrics_instruction}\n\n"
        f"{narrative_writing_rules}\n"
        f"{anti_hedging_rules}\n"
        f"{anti_repetition_rules}\n\n"
        # IMP-21: Citation instruction for Results + Discussion + Conclusion
        "CITATION REQUIREMENT: The Discussion section MUST cite at least 3-5 papers "
        "when comparing findings with prior work. The Conclusion may cite 1-2 "
        "foundational references.\n"
        f"{citation_instruction}\n\n"
        "You are completing a paper. The sections written so far are:\n\n"
        f"---\n{part1}\n\n{part2}\n---\n\n"
        "Now write the final sections, maintaining consistency:\n\n"
        "7. **Results** (600-800 words):\n"
        "   - START with an AGGREGATED results table (Table 1): rows = methods, columns = metrics.\n"
        "     Each cell = mean ± std across seeds. Bold the best value per column.\n"
        "     EVERY table MUST have a descriptive caption that allows understanding without "
        "     reading the main text. NEVER use just 'Table 1' as a caption.\n"
        "   - Follow with a PER-REGIME table (Table 2) breaking down by easy/hard regimes.\n"
        "   - Include a STATISTICAL COMPARISON table (Table 3): paired t-tests between key methods.\n"
        "   - NEVER dump raw per-seed numbers in the main text. Aggregate first, then discuss.\n"
        "   - MUST include at least 2 DATA figures using `![Caption](charts/filename.png)` syntax "
        "for pre-generated charts and plots. One MUST be a performance comparison chart.\n"
        "     Do NOT use `<!-- FIGURE_PROMPT -->` for data figures in Results/Experiments — "
        "only use `![Caption](charts/...)` here.\n"
        "     Figures MUST be referenced in text: 'As shown in Figure N, ...'\n"
        "8. **Discussion** (400-800 words): interpretation of key findings, unexpected results, "
        "comparison with prior work (CITE 3-5 papers here!), practical implications.\n"
        f"{discussion_ablation}"
        "9. **Limitations** (200-300 words): honest assessment of scope, dataset, methodology. "
        "ALL caveats consolidated HERE — nowhere else in the paper.\n"
        "10. **Conclusion** (100-200 words MAXIMUM — this is a HARD LIMIT): "
        "Summarize contributions in 2-3 sentences. State main finding in 1 sentence. "
        "Suggest 2-3 concrete future directions in 1-2 sentences. "
        "Do NOT repeat any specific numbers from Results. Do NOT restate the abstract. "
        "A good conclusion is SHORT and forward-looking.\n\n"
        "CRITICAL FORMATTING RULES FOR ALL SECTIONS:\n"
        "- Write as FLOWING PROSE paragraphs, NOT bullet-point lists\n"
        "- NEVER dump raw metric paths like 'config/method_name/seed_3/primary_metric'\n"
        "- All numbers must be rounded to 4 decimal places maximum\n"
        "- Every table MUST have a descriptive caption (not just 'Table 1')\n"
        "- Use \\begin{algorithm} or pseudocode notation, NOT \\begin{verbatim}\n\n"
        "Output markdown with ## headers. Do NOT include a References section."
    )
    try:
        resp3 = _chat_with_prompt(llm, system, call3_user, max_tokens=_paper_max_tokens, retries=1)
        part3 = _trim_to_expected_section(resp3.content, ("Results", "Result"))
    except Exception:  # noqa: BLE001
        logger.error("Stage 17: Part 3 LLM call failed after retry — using placeholder")
        part3 = (
            "## Results\n[PLACEHOLDER — LLM call failed. Please regenerate this stage.]\n\n"
            "## Discussion\n[PLACEHOLDER]\n\n"
            "## Limitations\n[PLACEHOLDER]\n\n"
            "## Conclusion\n[PLACEHOLDER]"
        )
    sections.append(part3)
    logger.info("Stage 17: Part 3 (Results+Discussion+Limitations+Conclusion) — %d chars", len(part3))

    # Combine all sections
    draft = "\n\n".join(sections)

    # R32: Strip data verification preamble that LLMs sometimes emit before
    # the actual paper.  The preamble typically starts with "## Tested Conditions"
    # or similar headings and ends before "## Title".
    import re as _re_strip
    _title_match = _re_strip.search(r"^## Title\b", draft, _re_strip.MULTILINE)
    if _title_match and _title_match.start() > 200:
        _stripped = draft[_title_match.start():]
        logger.info(
            "R32: Stripped %d-char preamble before '## Title'",
            _title_match.start(),
        )
        draft = _stripped

    total_words = len(draft.split())
    logger.info("Stage 17: Full draft — %d chars, ~%d words", len(draft), total_words)

    return draft
