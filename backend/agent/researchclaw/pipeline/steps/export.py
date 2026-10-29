"""Final research export with claim, figure and compilation checks."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import re

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.steps.citations import (
    _remove_bibtex_entries,
    _remove_citations_from_text,
)
from researchclaw.pipeline.support.artifacts import (
    _load_hardware_profile,
    _read_literature_bib,
    _read_prior_artifact,
    _utcnow_iso,
    active_stage_glob,
)
from researchclaw.pipeline.support.citations import (
    _augment_canonical_citations,
    _sanitize_bibtex_for_latex,
)
from researchclaw.pipeline.support.claims import (
    _build_claim_integrity_report,
    _claim_boundary_instruction,
)
from researchclaw.pipeline.support.context import _chat_with_prompt, _get_evolution_overlay
from researchclaw.pipeline.support.parsing import _safe_json_loads
from researchclaw.pipeline.support.templates import (
    _extract_paper_title,
    _generate_neurips_checklist,
)
from researchclaw.pipeline.writing.figures import _generate_framework_diagram_prompt
from researchclaw.pipeline.writing.quality import _review_compiled_pdf
from researchclaw.pipeline.writing.sanitize import (
    _enforce_engineering_report_boundary,
    _remove_missing_markdown_figures,
    _repair_markdown_figure_references,
    _sanitize_fabricated_data,
)
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _execute_export_publish(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    # Export is also a hard scientific-quality boundary: a compiled PDF can
    # still be structurally unusable even when claim-integrity passes.
    _export_quality_degraded = False
    _export_quality_reasons: list[str] = []
    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    _deterministic_export = _paper_len_cfg in {"deterministic", "fallback"}
    _export_readiness = _safe_json_loads(
        _read_prior_artifact(run_dir, "research_readiness.json") or "{}", {}
    )
    _engineering_only_export = (
        isinstance(_export_readiness, dict)
        and str(_export_readiness.get("writing_policy", ""))
        in {"engineering_report_only", "no_empirical_claims"}
    )
    # A polishing call cannot turn smoke evidence into scientific evidence and
    # has repeatedly reintroduced unsupported claims.  For engineering-only
    # outputs, preserve the revised manuscript and apply the deterministic
    # evidence boundary directly.
    if llm is not None and not _deterministic_export and not _engineering_only_export:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "export_publish")
        sp = _pm.for_stage(
            "export_publish",
            evolution_overlay=_overlay,
            revised=revised + _claim_boundary_instruction(run_dir),
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        final_paper = resp.content
        # Content guard: reject LLM output that truncates the paper
        if revised and len(final_paper) < 0.6 * len(revised):
            logger.warning(
                "Stage 25: LLM output is %.0f%% of input length — using original",
                100 * len(final_paper) / max(len(revised), 1),
            )
            final_paper = revised
    else:
        final_paper = revised
    if not final_paper.strip():
        final_paper = "# Final Paper\n\nNo content generated."

    final_paper, _engineering_boundary_applied = _enforce_engineering_report_boundary(final_paper, run_dir)
    if _engineering_boundary_applied:
        logger.warning("Stage 25: Enforced engineering-report-only claim boundary")

    final_paper, _missing_figure_refs = _remove_missing_markdown_figures(final_paper, run_dir)
    if _missing_figure_refs:
        logger.warning("Stage 25: Removed %d missing figure reference(s): %s", len(_missing_figure_refs), _missing_figure_refs[:6])
    final_paper = _repair_markdown_figure_references(final_paper)

    # --- Graceful degradation: sanitize fabricated data ---
    _degradation_signal_path = run_dir / "degradation_signal.json"
    if _degradation_signal_path.exists():
        try:
            _deg_signal = json.loads(
                _degradation_signal_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            _deg_signal = {}

        # Back up pre-sanitized version
        (stage_dir / "paper_presanitized.md").write_text(
            final_paper, encoding="utf-8"
        )

        # Sanitize unverified data in tables
        final_paper, _san_report = _sanitize_fabricated_data(
            final_paper, run_dir
        )
        (stage_dir / "sanitization_report.json").write_text(
            json.dumps(_san_report, indent=2), encoding="utf-8"
        )

        # Insert a reason-specific notice after the abstract.  Do not claim
        # that the score was below threshold or that values were replaced
        # unless the current artifacts actually establish those facts.
        _deg_score = _deg_signal.get("score", "N/A")
        _deg_threshold = _deg_signal.get("threshold", "N/A")
        _deg_reason = str(_deg_signal.get("reason", "") or "")
        _numbers_replaced = int(_san_report.get("numbers_replaced", 0) or 0)
        if _engineering_boundary_applied:
            _deg_detail = (
                "This artifact is classified as an engineering smoke test; "
                "scientific performance conclusions are intentionally excluded."
            )
        elif _deg_reason == "quality_score_below_threshold":
            _deg_detail = (
                f"Quality gate score ({_deg_score}/{_deg_threshold}) was below "
                "the configured threshold."
            )
        elif _deg_reason == "evaluation_protocol_insufficient":
            _deg_detail = "The planned evaluation protocol was not fully satisfied."
        elif _deg_reason in {"claim_integrity_blocked", "final_claim_integrity_blocked"}:
            _deg_detail = "The claim-integrity audit found conclusions beyond the available evidence."
        elif _deg_reason == "export_quality_degraded":
            _deg_detail = "The PDF or compilation quality review reported unresolved findings."
        else:
            _deg_detail = "A current quality check reported unresolved evidence limitations."
        if _numbers_replaced:
            _deg_detail += (
                f" {_numbers_replaced} unverified numerical value(s) were replaced "
                "with `---`; consult the sanitization report."
            )
        _deg_notice = f"\n\n> **Evidence limitation:** {_deg_detail}\n\n"
        # Try to insert after ## Abstract section
        _abstract_markers = ["## Abstract\n", "# Abstract\n"]
        _notice_inserted = False
        for _marker in _abstract_markers:
            if _marker in final_paper:
                _marker_end = final_paper.index(_marker) + len(_marker)
                # Find the end of the abstract paragraph
                _next_section = final_paper.find("\n## ", _marker_end)
                _next_heading = final_paper.find("\n# ", _marker_end)
                _insert_pos = min(
                    p for p in (_next_section, _next_heading)
                    if p > 0
                ) if any(p > 0 for p in (_next_section, _next_heading)) else len(final_paper)
                final_paper = (
                    final_paper[:_insert_pos]
                    + _deg_notice
                    + final_paper[_insert_pos:]
                )
                _notice_inserted = True
                break
        if not _notice_inserted:
            # Fallback: prepend to paper
            final_paper = _deg_notice + final_paper

        logger.info(
            "Stage 25: Applied degraded-mode sanitization — "
            "%d numbers replaced, %d kept",
            _san_report.get("numbers_replaced", 0),
            _san_report.get("numbers_kept", 0),
        )

    # IMP-3: Deduplicate "due to computational constraints" — keep at most 1
    import re as _re_imp3
    _CONSTRAINT_PAT = _re_imp3.compile(
        r"[Dd]ue to computational constraints", _re_imp3.IGNORECASE
    )
    _matches = list(_CONSTRAINT_PAT.finditer(final_paper))
    if len(_matches) > 1:
        # Keep only the first occurrence; remove subsequent ones by
        # deleting the enclosing sentence.
        for m in reversed(_matches[1:]):
            # Find sentence boundaries around the match
            start = final_paper.rfind(".", 0, m.start())
            start = start + 1 if start >= 0 else m.start()
            end = final_paper.find(".", m.end())
            end = end + 1 if end >= 0 else m.end()
            sentence = final_paper[start:end].strip()
            if sentence:
                final_paper = final_paper[:start] + final_paper[end:]
        final_paper = re.sub(r"[^\S\n]{2,}", " ", final_paper)
        logger.info(
            "Stage 25: Removed %d duplicate 'computational constraints' "
            "disclaimers",
            len(_matches) - 1,
        )

    # IMP-19 Layer 2: Ensure at least figures are referenced in the paper
    import re as _re_fig
    chart_files = []
    for _chart_src_dir in [
        stage_dir / "charts",
        run_dir / "stage-16" / "charts",
        run_dir / "stage-14" / "charts",
    ]:
        if _chart_src_dir.is_dir():
            chart_files.extend(sorted(_chart_src_dir.glob("*.png")))
    if chart_files and "![" not in final_paper:
        # Distribute figures to relevant sections based on filename keywords
        _fig_placement: dict[str, list[str]] = {
            "method": [],       # architecture, method, model, pipeline diagrams
            "result": [],       # experiment, comparison, ablation charts
            "intro": [],        # concept, overview, illustration
        }
        _fig_counter = 0
        for cf in chart_files[:6]:
            _fig_counter += 1
            stem_lower = cf.stem.lower()
            label = cf.stem.replace("_", " ").title()
            fig_md = f"![Figure {_fig_counter}: {label}](charts/{cf.name})"
            if any(k in stem_lower for k in ("architecture", "model", "pipeline", "method", "flowchart")):
                _fig_placement["method"].append(fig_md)
            elif any(k in stem_lower for k in ("experiment", "comparison", "ablation", "result", "metric")):
                _fig_placement["result"].append(fig_md)
            elif any(k in stem_lower for k in ("concept", "overview", "illustration", "threat", "attack")):
                _fig_placement["intro"].append(fig_md)
            else:
                _fig_placement["result"].append(fig_md)  # default to results

        # Insert figures at relevant section boundaries
        _section_markers = {
            "method": ["## Method", "## Methodology", "## Approach", "## Framework",
                        "## 3. Method", "## 3 Method"],
            "result": ["## Results", "## Experiments", "## Evaluation",
                        "## 5. Results", "## 4. Experiments", "## 5 Results"],
            "intro": ["## Related Work", "## Background", "## 2. Related",
                       "## 2 Related Work"],
        }
        _total_inserted = 0
        for category, figs in _fig_placement.items():
            if not figs:
                continue
            fig_block = "\n\n" + "\n\n".join(figs) + "\n\n"
            inserted = False
            for marker in _section_markers.get(category, []):
                if marker in final_paper:
                    # Insert BEFORE the marker section (so figure appears at end of previous section)
                    final_paper = final_paper.replace(marker, fig_block + marker, 1)
                    inserted = True
                    _total_inserted += len(figs)
                    break
            if not inserted:
                # Fallback: insert before Conclusion/Limitations/Discussion
                for fallback in ["## Conclusion", "## Limitations", "## Discussion"]:
                    if fallback in final_paper:
                        final_paper = final_paper.replace(fallback, fig_block + fallback, 1)
                        inserted = True
                        _total_inserted += len(figs)
                        break
            if not inserted:
                final_paper += fig_block
                _total_inserted += len(figs)

        logger.info(
            "IMP-19: Injected %d figure references into paper_final.md (distributed across sections)",
            _total_inserted,
        )

    # IMP-24: Detect excessive number repetition
    _numbers_found = _re_fig.findall(r"\b\d+\.\d{2,}\b", final_paper)
    from collections import Counter as _Counter
    _num_counts = _Counter(_numbers_found)
    _repeated = {n: c for n, c in _num_counts.items() if c > 3}
    if _repeated:
        logger.warning(
            "IMP-24: Numbers repeated >3 times: %s",
            _repeated,
        )

    (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")

    # --- Fabrication sanitization: blank out unsupported numbers ---
    _fab_flags_text = _read_prior_artifact(run_dir, "fabrication_flags.json") or ""
    _fab_flags = _safe_json_loads(_fab_flags_text, {}) if _fab_flags_text else {}
    if isinstance(_fab_flags, dict) and _fab_flags.get("fabrication_suspected"):
        import re as _re_fab
        _real_vals = set()
        for rv in _fab_flags.get("real_metric_values", []):
            if isinstance(rv, (int, float)) and math.isfinite(rv):
                _real_vals.add(str(round(rv, 4)))
                _real_vals.add(str(round(rv, 2)))
                _real_vals.add(str(round(rv, 1)))
                _real_vals.add(str(int(rv)) if rv == int(rv) else "")

        def _sanitize_number(m: _re_fab.Match) -> str:  # type: ignore[name-defined]
            """Replace fabricated numbers with '--' but keep real ones."""
            num_str = m.group(0)
            # Keep the number if it matches any known real metric value
            try:
                num_val = float(num_str)
                if not math.isfinite(num_val):
                    return "--"
                rounded_strs = {
                    str(round(num_val, 4)),
                    str(round(num_val, 2)),
                    str(round(num_val, 1)),
                    str(int(num_val)) if num_val == int(num_val) else "",
                }
                if rounded_strs & _real_vals:
                    return num_str  # real value — keep it
            except (ValueError, OverflowError):
                return num_str
            return "--"

        # Only sanitize numbers in Results/Experiments/Evaluation/Ablation sections
        _result_section_pat = _re_fab.compile(
            r"(##\s*(?:\d+\.?\s*)?(?:Results|Experiments|Evaluation|Ablation"
            r"|Experimental Results|Quantitative).*?)(?=\n##\s|\Z)",
            _re_fab.DOTALL | _re_fab.IGNORECASE,
        )
        _sanitized_count = 0

        def _sanitize_section(sec_match: _re_fab.Match) -> str:  # type: ignore[name-defined]
            nonlocal _sanitized_count
            section_text = sec_match.group(0)
            # Replace decimal numbers (e.g., 73.42, 0.891) but NOT integers
            # that are likely structural (year, section number, figure number)
            def _replace_in_section(m: _re_fab.Match) -> str:  # type: ignore[name-defined]
                nonlocal _sanitized_count
                result = _sanitize_number(m)
                if result == "--":
                    _sanitized_count += 1
                return result
            return _re_fab.sub(
                r"\b\d+\.\d{1,6}\b", _replace_in_section, section_text
            )

        final_paper = _result_section_pat.sub(_sanitize_section, final_paper)

        if _sanitized_count > 0:
            logger.warning(
                "Stage 25: Fabrication sanitization — blanked %d unsupported "
                "numbers in Results sections (experiment had no real metrics)",
                _sanitized_count,
            )
            # Rewrite the sanitized paper
            (stage_dir / "paper_final.md").write_text(
                final_paper, encoding="utf-8"
            )

    # Initialize artifacts list
    artifacts = ["paper_final.md"]
    final_paper_latex = final_paper  # default: no citation conversion
    # F2.7: Post-process citations — [cite_key] → \cite{cite_key}
    # and copy final references.bib to export stage
    _ay_map: dict[str, str] = {}  # BUG-102: author-year → cite_key map
    bib_text = _read_literature_bib(run_dir)
    bib_text = _augment_canonical_citations(bib_text, final_paper)
    bib_text = _sanitize_bibtex_for_latex(bib_text)
    if bib_text:
        # Replace [cite_key] patterns in the final paper with \cite{cite_key}
        # Collect all valid cite_keys from the bib file
        import re as _re

        valid_keys = set(_re.findall(r"@\w+\{([^,]+),", bib_text))

        # BUG-102: Recover author-year citations → [cite_key] format.
        # When Stage 19 (paper_revision) converts [cite_key] to [Author et al., 2024],
        # the downstream regex can't match them. Build a reverse map from bib entries.
        def _build_author_year_map(bib: str, keys: set[str]) -> dict[str, str]:
            """Build mapping from author-year patterns to cite_keys.

            Returns dict like:
              "Raissi et al., 2019" → "raissi2019physicsinformed"
              "Tavella and Randall, 2000" → "tavella2000pricing"
            """
            mapping: dict[str, str] = {}
            # Parse each bib entry for author + year
            entry_pat = _re.compile(
                r"@\w+\{([^,]+),\s*(.*?)\n\}", _re.DOTALL
            )
            for m in entry_pat.finditer(bib):
                key = m.group(1).strip()
                if key not in keys:
                    continue
                body = m.group(2)
                # Extract author field
                author_m = _re.search(
                    r"author\s*=\s*[\{\"](.*?)[\}\"]", body, _re.IGNORECASE
                )
                year_m = _re.search(
                    r"year\s*=\s*[\{\"]?(\d{4})[\}\"]?", body, _re.IGNORECASE
                )
                if not author_m or not year_m:
                    continue
                author_raw = author_m.group(1).strip()
                year = year_m.group(1)
                # Parse author names (split on " and ")
                authors = [a.strip() for a in _re.split(r"\s+and\s+", author_raw)]
                # Extract last names
                last_names = []
                for a in authors:
                    if "," in a:
                        last_names.append(a.split(",")[0].strip())
                    else:
                        parts = a.split()
                        last_names.append(parts[-1] if parts else a)
                if not last_names:
                    continue
                # Generate author-year patterns:
                # 1 author: "Smith, 2024"
                # 2 authors: "Smith and Jones, 2024"
                # 3+ authors: "Smith et al., 2024"
                if len(last_names) == 1:
                    patterns = [f"{last_names[0]}, {year}"]
                elif len(last_names) == 2:
                    patterns = [
                        f"{last_names[0]} and {last_names[1]}, {year}",
                        f"{last_names[0]} \\& {last_names[1]}, {year}",
                    ]
                else:
                    patterns = [
                        f"{last_names[0]} et al., {year}",
                        f"{last_names[0]} et al. {year}",
                    ]
                    # Also add "Smith and Jones, 2024" for first two authors
                    patterns.append(
                        f"{last_names[0]} and {last_names[1]}, {year}"
                    )
                for pat in patterns:
                    mapping[pat] = key
            return mapping

        _ay_map = _build_author_year_map(bib_text, valid_keys)
        if _ay_map:
            # Count how many author-year citations exist in the paper
            _ay_found = 0
            for _ay_pat in _ay_map:
                if _ay_pat in final_paper:
                    _ay_found += 1
            if _ay_found > 0:
                logger.info(
                    "Stage 25: Found %d author-year citation patterns — "
                    "converting back to [cite_key] format.",
                    _ay_found,
                )
                # Sort by longest pattern first to avoid partial matches
                for _ay_pat in sorted(_ay_map, key=len, reverse=True):
                    _ay_key = _ay_map[_ay_pat]
                    # Match [Author et al., 2024] or [Author and Jones, 2024; ...]
                    # Handle single-citation brackets
                    final_paper = final_paper.replace(
                        f"[{_ay_pat}]", f"[{_ay_key}]"
                    )
                    # Handle within multi-citation brackets [A et al., 2020; B et al., 2021]
                    # Replace the author-year segment inside brackets
                    final_paper = final_paper.replace(_ay_pat, _ay_key)
                # Fix multi-key brackets: [key1; key2] → [key1, key2]
                # (author-year uses semicolons, cite-keys use commas)
                def _fix_semicolon_cites(m_sc: _re.Match[str]) -> str:
                    inner = m_sc.group(1)
                    # Only convert if ALL segments look like cite keys
                    parts = [p.strip() for p in inner.split(";")]
                    _ck = r"[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*"
                    if all(_re.fullmatch(_ck, p) for p in parts):
                        return "[" + ", ".join(parts) + "]"
                    return m_sc.group(0)
                final_paper = _re.sub(
                    r"\[([^\]]+;[^\]]+)\]", _fix_semicolon_cites, final_paper
                )
                (stage_dir / "paper_final.md").write_text(
                    final_paper, encoding="utf-8"
                )

        # R10-Fix4: Citation cross-validation
        cited_keys_in_paper: set[str] = set()
        _citation_key_re = _re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*$")
        for _bracket in _re.findall(r"\[([^\]]+)\]", final_paper):
            for _candidate in _bracket.split(","):
                _candidate = _candidate.strip()
                if _citation_key_re.fullmatch(_candidate):
                    cited_keys_in_paper.add(_candidate)
        if valid_keys and cited_keys_in_paper:
            invalid_keys = cited_keys_in_paper - valid_keys
            if invalid_keys:
                logger.warning(
                    "Stage 25: Found %d citation keys in paper not in references.bib: %s",
                    len(invalid_keys),
                    ", ".join(sorted(invalid_keys)[:20]),
                )
                # IMP-29: Silently remove invalid citations instead of
                # leaving ugly [?key:NOT_IN_BIB] markers in the output.
                final_paper = _remove_citations_from_text(final_paper, invalid_keys)
                # Clean up whitespace artifacts from removed citations
                import re as _re_imp29
                final_paper = _re_imp29.sub(r"  +", " ", final_paper)
                final_paper = _re_imp29.sub(r" ([.,;:)])", r"\1", final_paper)
                (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")
                (stage_dir / "invalid_citations.json").write_text(
                    json.dumps(sorted(invalid_keys), indent=2), encoding="utf-8"
                )
                artifacts.append("invalid_citations.json")

        if valid_keys:
            _CITE_KEY_PAT = r"[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9]*"

            # Step 1: Convert multi-key brackets [key1, key2] → \cite{key1, key2}
            def _replace_multi_cite(m: _re.Match[str]) -> str:
                keys = [k.strip() for k in m.group(1).split(",")]
                matched = [k for k in keys if k in valid_keys]
                if matched:
                    return "\\cite{" + ", ".join(matched) + "}"
                return m.group(0)

            final_paper_latex = _re.sub(
                rf"\[({_CITE_KEY_PAT}(?:\s*,\s*{_CITE_KEY_PAT})+)\]",
                _replace_multi_cite,
                final_paper,
            )

            # Step 2: Convert single-key brackets [key] → \cite{key}
            def _replace_cite(m: _re.Match[str]) -> str:
                key = m.group(1)
                if key in valid_keys:
                    return f"\\cite{{{key}}}"
                return m.group(0)

            final_paper_latex = _re.sub(
                rf"\[({_CITE_KEY_PAT})\]", _replace_cite, final_paper_latex
            )

            # Step 3: Merge adjacent \cite{a} \cite{b} → \cite{a, b}
            def _merge_adjacent_cites(m: _re.Match[str]) -> str:
                keys = _re.findall(r"\\cite\{([^}]+)\}", m.group(0))
                return "\\cite{" + ", ".join(keys) + "}"

            final_paper_latex = _re.sub(
                r"\\cite\{[^}]+\}(?:\s*\\cite\{[^}]+\})+",
                _merge_adjacent_cites,
                final_paper_latex,
            )

            (stage_dir / "paper_final_latex.md").write_text(
                final_paper_latex, encoding="utf-8"
            )
            artifacts.append("paper_final_latex.md")
        # IMP-1: Prune uncited bibliography entries — keep only keys
        # that actually appear in the paper text (bracket or \cite form).
        if valid_keys:
            _all_cited: set[str] = set()
            # Bracket-format citations [key]
            _all_cited.update(
                _re.findall(r"\[([a-z]+\d{4}[a-z]*)\]", final_paper)
            )
            # \cite{key, key2} format (original + latex-converted)
            for _src in (
                final_paper,
                final_paper_latex,
            ):
                for _cm in _re.finditer(r"\\cite\{([^}]+)\}", _src):
                    _all_cited.update(
                        k.strip() for k in _cm.group(1).split(",")
                    )
            uncited_keys = valid_keys - _all_cited
            if uncited_keys:
                bib_text = _remove_bibtex_entries(bib_text, uncited_keys)
                logger.info(
                    "Stage 25: Pruned %d uncited bibliography entries "
                    "(kept %d)",
                    len(uncited_keys),
                    len(valid_keys) - len(uncited_keys),
                )

        # Write final references.bib
        (stage_dir / "references.bib").write_text(bib_text, encoding="utf-8")
        artifacts.append("references.bib")
        logger.info(
            "Stage 25: Exported references.bib with %d entries",
            len(valid_keys) if valid_keys else 0,
        )
    if "references.bib" not in artifacts:
        # With no verified bibliography, remove any remaining model-generated
        # bracket placeholders instead of exposing them as literal paper text.
        final_paper = re.sub(r"\s*\[cite_key:[^\]]+\]", "", final_paper, flags=re.IGNORECASE)
        final_paper_latex = final_paper
        (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")
        (stage_dir / "references.bib").write_text("", encoding="utf-8")
        artifacts.append("references.bib")

    # Conference template: generate .tex file
    try:
        from researchclaw.templates import get_template, markdown_to_latex

        tpl = get_template(config.export.target_conference)
        # Use the latex-citation-processed version if available
        tex_source = final_paper_latex
        # Submission checklists are separate form/appendix artifacts.  Do not
        # place them in the manuscript body, where they create an invalid PDF
        # for conferences that collect the checklist separately.
        if tpl.name in ("neurips_2024", "neurips_2025", "icml_2025", "icml_2026",
                         "iclr_2025", "iclr_2026"):
            _has_exp = bool(_read_prior_artifact(run_dir, "experiment_summary.json"))
            _checklist = _generate_neurips_checklist(
                has_experiments=_has_exp,
                has_code=True,
            )
            (stage_dir / "submission_checklist.md").write_text(
                _checklist, encoding="utf-8"
            )
            artifacts.append("submission_checklist.md")
        tex_content = markdown_to_latex(
            tex_source,
            tpl,
            title=_extract_paper_title(tex_source),
            authors=config.export.authors,
            bib_file=config.export.bib_file,
            bib_entries=_ay_map or None,
        )
        if not re.search(r"@\w+\{[^,]+,", bib_text):
            tex_content = re.sub(
                r"\n?\\bibliographystyle\{[^}]+\}\s*\n?\\bibliography\{[^}]+\}\s*",
                "\n",
                tex_content,
            )
        (stage_dir / "paper.tex").write_text(tex_content, encoding="utf-8")
        artifacts.append("paper.tex")
        logger.info(
            "Stage 25: Generated paper.tex for %s (%d chars)",
            tpl.display_name,
            len(tex_content),
        )
        # Copy bundled style files alongside paper.tex
        for sf in tpl.get_style_files():
            import shutil as _shutil_sty
            _shutil_sty.copy2(sf, stage_dir / sf.name)
        # Compile verification
        try:
            from researchclaw.templates.compiler import compile_latex
            _compile_result = compile_latex(stage_dir / "paper.tex", max_attempts=2)
            if _compile_result.success:
                logger.info("Stage 25: LaTeX compilation verification PASSED")
                artifacts.append("paper.pdf")
                # PDF-as-reviewer: LLM-based visual review of compiled PDF
                _pdf_path = stage_dir / "paper.pdf"
                if _pdf_path.exists() and llm is not None and not _engineering_only_export:
                    try:
                        _pdf_review = _review_compiled_pdf(
                            _pdf_path, llm, config.research.topic
                        )
                        if _pdf_review:
                            (stage_dir / "pdf_review.json").write_text(
                                json.dumps(_pdf_review, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                            artifacts.append("pdf_review.json")
                            _pdf_score = _pdf_review.get("overall_score", 0)
                            if _pdf_score < 5:
                                _export_quality_degraded = True
                                _export_quality_reasons.append(
                                    f"pdf_review_score_below_5:{_pdf_score}"
                                )
                                logger.warning(
                                    "Stage 25: PDF visual review score %d/10 — %s",
                                    _pdf_score,
                                    _pdf_review.get("summary", ""),
                                )
                            else:
                                logger.info(
                                    "Stage 25: PDF visual review score %d/10",
                                    _pdf_score,
                                )
                    except Exception as _pdf_exc:  # noqa: BLE001
                        logger.debug("Stage 25: PDF review skipped: %s", _pdf_exc)
                # Post-compilation quality checks
                try:
                    from researchclaw.templates.compiler import check_compiled_quality
                    _qc = check_compiled_quality(stage_dir / "paper.tex")
                    if _qc.warnings_summary:
                        logger.warning(
                            "Stage 25: Quality checks: %s",
                            "; ".join(_qc.warnings_summary),
                        )
                    _target_pages = int(getattr(config.export, "target_pages", 0) or 0)
                    _min_pages = int(getattr(config.export, "min_pages", 0) or 0)
                    _max_pages = int(getattr(config.export, "max_pages", 0) or 0)
                    _page_status = "not_configured"
                    if _target_pages:
                        _page_status = "on_target" if _qc.page_count == _target_pages else "below_target" if _qc.page_count < _target_pages else "above_target"
                    _page_warning = ""
                    if not _engineering_only_export and _min_pages and _qc.page_count < _min_pages:
                        _page_warning = f"Page count {_qc.page_count} is below the configured minimum {_min_pages}"
                        _export_quality_degraded = True
                        _export_quality_reasons.append(f"page_count_below_minimum:{_qc.page_count}<{_min_pages}")
                    elif not _engineering_only_export and _max_pages and _qc.page_count > _max_pages:
                        _page_warning = f"Page count {_qc.page_count} exceeds the configured maximum {_max_pages}"
                        _export_quality_degraded = True
                        _export_quality_reasons.append(f"page_count_above_maximum:{_qc.page_count}>{_max_pages}")
                    _quality_warnings = list(_qc.warnings_summary)
                    if _page_warning:
                        _quality_warnings.append(_page_warning)
                    (stage_dir / "compilation_quality.json").write_text(
                        json.dumps({
                            "page_count": _qc.page_count,
                            "target_pages": _target_pages,
                            "min_pages": _min_pages,
                            "max_pages": _max_pages,
                            "page_target_status": _page_status,
                            "unresolved_refs": _qc.unresolved_refs,
                            "unresolved_cites": _qc.unresolved_cites,
                            "overfull_hboxes": len(_qc.overfull_hboxes),
                            "orphan_figures": _qc.orphan_figures,
                            "orphan_labels": _qc.orphan_labels,
                            "warnings": _quality_warnings,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    if len(_qc.warnings_summary) >= 3:
                        _export_quality_degraded = True
                        _export_quality_reasons.append(
                            f"compilation_warnings:{len(_qc.warnings_summary)}"
                        )
                    artifacts.append("compilation_quality.json")
                    # BUG-27: Warn if page count exceeds limit
                    _page_limit = 10
                    if _qc.page_count and _qc.page_count > _page_limit:
                        logger.warning(
                            "BUG-27: Paper is %d pages (limit %d). "
                            "Consider tightening content in revision.",
                            _qc.page_count, _page_limit,
                        )
                except Exception as _qc_exc:  # noqa: BLE001
                    logger.debug("Stage 25: Quality checks skipped: %s", _qc_exc)
            else:
                logger.warning("Stage 25: LaTeX compilation verification FAILED: %s", _compile_result.errors[:3])
                # Add compilation failure comment to .tex
                _tex_path = stage_dir / "paper.tex"
                if _tex_path.exists():
                    _tex_content = _tex_path.read_text(encoding="utf-8")
                    if "% WARNING: Compilation failed" not in _tex_content:
                        _tex_content = (
                            "% WARNING: Compilation failed. Errors:\n"
                            + "".join(f"% {e}\n" for e in _compile_result.errors[:5])
                            + _tex_content
                        )
                        _tex_path.write_text(_tex_content, encoding="utf-8")
        except Exception as _compile_exc:  # noqa: BLE001
            logger.debug("Stage 25: Compile verification skipped: %s", _compile_exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LaTeX generation skipped: %s", exc)

    # WS-5.4: Generate result visualizations
    # Priority: FigureAgent charts (stage-16 RESULT_ANALYSIS) > stage-14 legacy > fallback
    try:
        chart_dir = stage_dir / "charts"
        chart_dir.mkdir(parents=True, exist_ok=True)
        charts: list[Path] = []

        # Check if FigureAgent produced charts in stage-16 or stage-14
        _fa_charts_found = False
        for _fa_pattern in ("stage-16*/charts", "stage-14*/charts"):
            if _fa_charts_found:
                break
            for _fa_dir in sorted(active_stage_glob(run_dir, _fa_pattern), reverse=True):
                _fa_pngs = list(_fa_dir.glob("fig_*.png"))
                if _fa_pngs:
                    import shutil
                    for _fa_png in _fa_pngs:
                        dest = chart_dir / _fa_png.name
                        shutil.copy2(_fa_png, dest)
                        charts.append(dest)
                    _fa_charts_found = True
                    logger.info(
                        "Stage 25: Copied %d FigureAgent charts from %s",
                        len(_fa_pngs), _fa_dir,
                    )
                    break

        # Always generate structured charts from visualize.py (different names)
        from researchclaw.experiment.visualize import generate_all_charts
        _metric_dir = getattr(config.experiment, "metric_direction", "minimize")
        _viz_charts = generate_all_charts(
            run_dir,
            chart_dir,
            metric_key=config.experiment.metric_key,
            metric_direction=_metric_dir,
        )
        charts.extend(_viz_charts)

        if charts:
            artifacts.append("charts/")
            logger.info("Stage 25: Generated %d chart(s) total", len(charts))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chart generation failed: %s", exc)

    # BUG-99: Validate that \includegraphics paths in .tex match actual files.
    # The LLM may write paper referencing a chart name that differs from the
    # actual generated filename (e.g. "performance_comparison.png" vs
    # "experiment_comparison.png").
    try:
        tex_path = stage_dir / "paper.tex"
        if tex_path.exists():
            tex_text = tex_path.read_text(encoding="utf-8")
            # Extract all \includegraphics{path} references
            _fig_refs = re.findall(
                r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", tex_text
            )
            if _fig_refs:
                # Collect all actual chart files in stage_dir/charts/
                _chart_dir = stage_dir / "charts"
                _actual_files: dict[str, str] = {}  # lowercase stem → relative path
                if _chart_dir.is_dir():
                    for _af in _chart_dir.iterdir():
                        if _af.is_file() and _af.suffix.lower() in (
                            ".png", ".jpg", ".jpeg", ".pdf", ".svg",
                        ):
                            _actual_files[_af.stem.lower()] = f"charts/{_af.name}"
                            _actual_files[_af.name.lower()] = f"charts/{_af.name}"

                _fixes: dict[str, str] = {}
                for _ref in _fig_refs:
                    _ref_path = stage_dir / _ref
                    if _ref_path.exists():
                        continue  # File exists, no fix needed
                    # Try fuzzy matching against actual chart files
                    _ref_stem = Path(_ref).stem.lower()
                    _ref_name = Path(_ref).name.lower()
                    # Exact stem match (different extension or directory)
                    if _ref_stem in _actual_files:
                        _fixes[_ref] = _actual_files[_ref_stem]
                        continue
                    if _ref_name in _actual_files:
                        _fixes[_ref] = _actual_files[_ref_name]
                        continue
                    # Fuzzy match: find best match by keyword overlap
                    if _actual_files:
                        _ref_words = set(_ref_stem.replace("-", "_").split("_"))
                        _best_match, _best_overlap = "", 0
                        for _stem, _apath in _actual_files.items():
                            _a_words = set(
                                _stem.replace("-", "_").split("_")
                            )
                            _overlap = len(_ref_words & _a_words)
                            if _overlap > _best_overlap:
                                _best_overlap = _overlap
                                _best_match = _apath
                        if _best_overlap >= 1 and _best_match:
                            _fixes[_ref] = _best_match

                if _fixes:
                    for _old_path, _new_path in _fixes.items():
                        tex_text = tex_text.replace(
                            f"{{{_old_path}}}", f"{{{_new_path}}}"
                        )
                    tex_path.write_text(tex_text, encoding="utf-8")
                    logger.warning(
                        "BUG-99: Fixed %d figure path mismatch(es) in paper.tex: %s",
                        len(_fixes),
                        ", ".join(f"{k} → {v}" for k, v in _fixes.items()),
                    )
                # Warn about any remaining unresolved references
                _still_missing = [
                    r for r in _fig_refs
                    if r not in _fixes and not (stage_dir / r).exists()
                ]
                if _still_missing:
                    logger.warning(
                        "Stage 25: %d figure reference(s) have no matching file: %s",
                        len(_still_missing), _still_missing[:5],
                    )
    except Exception as _fig_exc:  # noqa: BLE001
        logger.debug("Stage 25: Figure path validation skipped: %s", _fig_exc)

    # Charts are assembled after the first compile attempt. Retry once now
    # that all referenced assets and any fuzzy path fixes are in place.
    _late_tex_path = stage_dir / "paper.tex"
    _late_pdf_path = stage_dir / "paper.pdf"
    if _late_tex_path.exists() and not _late_pdf_path.exists():
        try:
            from researchclaw.templates.compiler import compile_latex as _late_compile_latex
            _late_compile = _late_compile_latex(_late_tex_path, max_attempts=2)
            if _late_compile.success and _late_pdf_path.exists():
                if "paper.pdf" not in artifacts:
                    artifacts.append("paper.pdf")
                logger.info("Stage 25: LaTeX compilation PASSED after chart assembly")
            else:
                logger.warning(
                    "Stage 25: Post-chart LaTeX compilation still failed: %s",
                    _late_compile.errors[:3],
                )
        except Exception as _late_compile_exc:  # noqa: BLE001
            logger.warning("Stage 25: Post-chart compile retry failed: %s", _late_compile_exc)

    # --- Code packaging: multi-file directory or single file ---
    exp_final_dir_path = _read_prior_artifact(run_dir, "experiment_final/")
    if exp_final_dir_path and Path(exp_final_dir_path).is_dir():
        import ast

        code_dir = stage_dir / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        all_code_combined = ""
        code_file_names: list[str] = []
        for src in sorted(Path(exp_final_dir_path).glob("*.py")):
            (code_dir / src.name).write_bytes(src.read_bytes())
            all_code_combined += src.read_text(encoding="utf-8") + "\n"
            code_file_names.append(src.name)

        # Detect dependencies from all files
        detected: set[str] = set()
        known_packages = {
            "numpy": "numpy",
            "torch": "torch",
            "tensorflow": "tensorflow",
            "sklearn": "scikit-learn",
            "scikit-learn": "scikit-learn",
            "scipy": "scipy",
            "pandas": "pandas",
            "matplotlib": "matplotlib",
            "seaborn": "seaborn",
            "transformers": "transformers",
            "datasets": "datasets",
            "jax": "jax",
        }
        try:
            tree = ast.parse(all_code_combined)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in known_packages:
                            detected.add(known_packages[top])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    if top in known_packages:
                        detected.add(known_packages[top])
        except SyntaxError:
            pass

        requirements = sorted(detected)
        (code_dir / "requirements.txt").write_text(
            "\n".join(requirements) + ("\n" if requirements else ""),
            encoding="utf-8",
        )

        paper_title = _extract_paper_title(final_paper)
        file_list_md = "\n".join(f"- `{f}`" for f in code_file_names)
        readme = (
            f"# Code Package for {paper_title}\n\n"
            "## Description\n"
            "This directory contains the experiment project used for the paper.\n\n"
            "## Project Files\n"
            f"{file_list_md}\n\n"
            "## How to Run\n"
            "`python main.py`\n\n"
            "## Dependencies\n"
            "Install dependencies with `pip install -r requirements.txt` if needed.\n"
        )
        (code_dir / "README.md").write_text(readme, encoding="utf-8")
        artifacts.append("code/")
        logger.info(
            "Stage 25: Packaged multi-file code release (%d files, %d deps)",
            len(code_file_names),
            len(requirements),
        )
    else:
        # Backward compat: single-file packaging
        code_payload = _read_prior_artifact(run_dir, "experiment_final.py")
        if not code_payload:
            code_payload = _read_prior_artifact(run_dir, "experiment.py")
        if code_payload:
            import ast

            code_dir = stage_dir / "code"
            code_dir.mkdir(parents=True, exist_ok=True)
            (code_dir / "experiment.py").write_text(code_payload, encoding="utf-8")

            detected_single: set[str] = set()
            known_packages_single = {
                "numpy": "numpy",
                "torch": "torch",
                "tensorflow": "tensorflow",
                "sklearn": "scikit-learn",
                "scikit-learn": "scikit-learn",
                "scipy": "scipy",
                "pandas": "pandas",
                "matplotlib": "matplotlib",
                "seaborn": "seaborn",
                "transformers": "transformers",
                "datasets": "datasets",
                "jax": "jax",
            }
            try:
                tree = ast.parse(code_payload)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            top = alias.name.split(".")[0]
                            if top in known_packages_single:
                                detected_single.add(known_packages_single[top])
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        top = node.module.split(".")[0]
                        if top in known_packages_single:
                            detected_single.add(known_packages_single[top])
            except SyntaxError:
                pass

            requirements = sorted(detected_single)
            (code_dir / "requirements.txt").write_text(
                "\n".join(requirements) + ("\n" if requirements else ""),
                encoding="utf-8",
            )
            paper_title = _extract_paper_title(final_paper)
            readme = (
                f"# Code Package for {paper_title}\n\n"
                "## Description\n"
                "This directory contains the final experiment script used for the paper.\n\n"
                "## How to Run\n"
                "`python experiment.py`\n\n"
                "## Dependencies\n"
                "Install dependencies with `pip install -r requirements.txt` if needed.\n"
            )
            (code_dir / "README.md").write_text(readme, encoding="utf-8")
            artifacts.append("code/")
            logger.info(
                "Stage 25: Packaged single-file code release with %d deps",
                len(requirements),
            )
    # WS-5.5: Generate framework diagram prompt for methodology section
    try:
        _framework_prompt = _generate_framework_diagram_prompt(
            final_paper, config, llm=llm
        )
        if _framework_prompt:
            _chart_dir = stage_dir / "charts"
            _chart_dir.mkdir(parents=True, exist_ok=True)
            (_chart_dir / "framework_diagram_prompt.md").write_text(
                _framework_prompt, encoding="utf-8"
            )
            logger.info("Stage 25: Generated framework diagram prompt → charts/framework_diagram_prompt.md")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Stage 25: Framework diagram prompt generation skipped: %s", exc)

    # Reproducibility manifest: capture the exact runtime, execution command,
    # rigor protocol, and bounded dataset file hashes used by this run.
    try:
        import hashlib as _hashlib_repro
        import platform as _platform_repro
        import sys as _sys_repro
        from importlib import metadata as _metadata_repro

        _package_versions: dict[str, str] = {}
        for _pkg in (
            "numpy", "scipy", "scikit-learn", "pandas", "torch",
            "transformers", "datasets", "matplotlib", "pyyaml",
        ):
            try:
                _package_versions[_pkg] = _metadata_repro.version(_pkg)
            except _metadata_repro.PackageNotFoundError:
                pass
        _dataset_files: list[dict[str, object]] = []
        _datasets_dir_raw = str(getattr(config.experiment, "datasets_dir", "") or "").strip()
        _datasets_dir = Path(_datasets_dir_raw) if _datasets_dir_raw else None
        if _datasets_dir is not None and _datasets_dir.is_dir():
            for _data_file in sorted(_datasets_dir.rglob("*")):
                if not _data_file.is_file() or len(_dataset_files) >= 500:
                    continue
                try:
                    _digest = _hashlib_repro.sha256(_data_file.read_bytes()).hexdigest()
                    _dataset_files.append({
                        "path": str(_data_file.relative_to(_datasets_dir)),
                        "size_bytes": _data_file.stat().st_size,
                        "sha256": _digest,
                    })
                except OSError:
                    continue
        _plan_for_repro = {}
        try:
            _plan_for_repro = yaml.safe_load(
                _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
            ) or {}
        except yaml.YAMLError:
            pass
        _provenance_for_repro = _safe_json_loads(
            _read_prior_artifact(run_dir, "experiment_provenance.json") or "{}", {}
        )
        _repro_manifest = {
            "schema_version": "reproducibility-v1",
            "generated": _utcnow_iso(),
            "python": _sys_repro.version,
            "platform": _platform_repro.platform(),
            "package_versions": _package_versions,
            "hardware_profile": _load_hardware_profile(run_dir),
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            "experiment_command": (
                _provenance_for_repro.get("command")
                if isinstance(_provenance_for_repro, dict) else None
            ),
            "evaluation_protocol": (
                _plan_for_repro.get("evaluation_protocol", {})
                if isinstance(_plan_for_repro, dict) else {}
            ),
            "datasets_root": str(_datasets_dir) if _datasets_dir is not None and _datasets_dir.is_dir() else "",
            "dataset_files": _dataset_files,
            "dataset_manifest_truncated": len(_dataset_files) >= 500,
        }
        (stage_dir / "reproducibility_manifest.json").write_text(
            json.dumps(_repro_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        artifacts.append("reproducibility_manifest.json")
    except Exception as _repro_exc:  # noqa: BLE001
        logger.warning("Reproducibility manifest generation failed: %s", _repro_exc)

    # Final exported text must pass the same deterministic claim boundary as
    # the revised draft. Export-time polishing is not allowed to bypass S23.
    _final_claim_integrity = _build_claim_integrity_report(run_dir, final_paper)
    _final_claim_path = stage_dir / "final_claim_integrity_report.json"
    _final_claim_path.write_text(
        json.dumps(_final_claim_integrity, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    artifacts.append("final_claim_integrity_report.json")
    _final_decision = "degraded" if _export_quality_degraded else None
    if _final_claim_integrity.get("status") == "blocked" or _export_quality_degraded:
        _final_decision = "degraded"
        _scope_notice = (
            "\n\n> **Evidence Scope Notice:** Final export is limited by "
            + ("a failed claim-integrity audit" if _final_claim_integrity.get("status") == "blocked" else "PDF/compilation quality findings")
            + ". Treat all empirical conclusions as limited to "
            "the executed datasets, models, metrics, seeds, and runtime conditions. "
            "See `final_claim_integrity_report.json` and `pdf_review.json` before reuse or submission.\n\n"
        )
        if "Evidence Scope Notice" not in final_paper:
            final_paper = _scope_notice + final_paper
            (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")
        _final_signal = {
            "reason": ("final_claim_integrity_blocked" if _final_claim_integrity.get("status") == "blocked" else "export_quality_degraded"),
            "claim_integrity_status": _final_claim_integrity.get("status"),
            "quality_reasons": _export_quality_reasons,
            "recommended_actions": _final_claim_integrity.get("recommended_actions", []),
            "generated": _utcnow_iso(),
        }
        (run_dir / "degradation_signal.json").write_text(
            json.dumps(_final_signal, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    else:
        # Do not let a prior blocked export keep poisoning later successful
        # resumptions with a stale warning.
        (run_dir / "degradation_signal.json").unlink(missing_ok=True)

    return StageResult(
        stage=Stage.EXPORT_PUBLISH,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-25/{a}" for a in artifacts),
        decision=_final_decision,
    )
