"""Support templates components."""

from __future__ import annotations

import logging
from typing import Any

from researchclaw.pipeline.support.artifacts import _utcnow_iso

logger = logging.getLogger(__name__)


def _generate_neurips_checklist(
    has_experiments: bool = True,
    has_theory: bool = False,
    has_code: bool = True,
) -> str:
    """Generate a NeurIPS-style paper checklist appendix in markdown.

    This checklist is based on the NeurIPS 2025 submission requirements.
    It is appended to the paper before LaTeX conversion.
    """
    items = [
        ("Claims", "Do the main claims accurately reflect the paper's contributions and scope?", "Not assessed"),
        ("Limitations", "Does the paper discuss limitations of the work?", "Not assessed"),
    ]
    if has_theory:
        items.append(
            ("Theory", "Are all assumptions stated and proofs included?", "Not assessed")
        )
    items.extend([
        ("Experiments reproducibility", "Does the paper fully disclose experimental settings?", "Not assessed" if has_experiments else "NA"),
        ("Code and data", "Is code or data provided for reproducibility?", "Not assessed" if has_code else "Not assessed"),
        ("Experimental details", "Are training details and hyperparameters specified?", "Not assessed" if has_experiments else "NA"),
        ("Error bars", "Are error bars or confidence intervals reported?", "Not assessed" if has_experiments else "NA"),
        ("Compute resources", "Are compute requirements documented?", "Not assessed" if has_experiments else "NA"),
        ("Code of ethics", "Does the work comply with the code of ethics?", "Not assessed"),
        ("Broader impacts", "Are potential negative societal impacts discussed?", "Not assessed"),
        ("Licenses", "Are licenses for used assets respected?", "Not assessed"),
        ("New assets", "Are newly released assets documented?", "NA"),
        ("Human subjects", "Were IRB approvals obtained if applicable?", "NA"),
    ])

    lines = [
        "## NeurIPS Paper Checklist",
        "",
    ]
    for label, question, answer in items:
        lines.append(f"**{label}**: {question}")
        lines.append(f"Answer: [{answer}]")
        lines.append("")

    return "\n".join(lines)


def _extract_paper_title(md_text: str) -> str:
    """Extract paper title from markdown text for LaTeX generation.

    Prioritises H1 headings that appear *before* the abstract section and
    look like real titles (>= 4 words, starts with uppercase).  This avoids
    picking up pseudocode comments or algorithm step labels.
    """
    import re as _re

    # Limit search to content before Abstract heading
    abstract_pos = _re.search(
        r"^#{1,2}\s+(Abstract|ABSTRACT)", md_text, _re.MULTILINE
    )
    search_region = md_text[: abstract_pos.start()] if abstract_pos else md_text[:3000]

    # Common generated-paper format:
    #   ## Title
    #   The Actual Paper Title
    # Treat the first non-empty body line as the title.  Previously this
    # valid representation fell through to ``Untitled Paper`` and the LaTeX
    # exporter emitted a manual-title placeholder.
    explicit_title = _re.search(
        r"^#{1,2}\s+Title\s*$\n(?:[ \t]*\n)*[ \t]*(.+?)\s*$",
        search_region,
        _re.MULTILINE | _re.IGNORECASE,
    )
    if explicit_title:
        value = _re.sub(r"^\*\*(.+?)\*\*$", r"\1", explicit_title.group(1).strip())
        if value:
            return value

    _SKIP = {"title", "abstract", "references", "appendix"}
    candidates: list[str] = []

    for line in search_region.splitlines():
        line = line.strip()
        # Match H1 or H2 headings
        hm = _re.match(r"^(#{1,2})\s+(.+)$", line)
        if hm:
            heading = hm.group(2).strip()
            heading_lower = heading.lower()
            # Handle "## Title Actual Paper Title" pattern
            if heading_lower.startswith("title ") and len(heading) > 6:
                heading = heading[6:].strip()
                heading_lower = heading.lower()
            if heading_lower in _SKIP:
                continue
            candidates.append(heading)
        # Bold title line (e.g. **My Paper Title**)
        m = _re.match(r"\*\*(.+?)\*\*$", line)
        if m and len(m.group(1).split()) >= 3:
            candidates.append(m.group(1))

    # Prefer candidates that look like real titles (>= 4 words, capitalised)
    for c in candidates:
        words = c.split()
        if len(words) >= 4 and c[0].isupper():
            return c

    # Fallback: any candidate
    if candidates:
        return candidates[0]

    return "Untitled Paper"


def _default_paper_outline(topic: str) -> str:
    return f"""# Paper Outline

## 1. Title
Focused title on {topic}

## 2. Abstract
- Problem framing
- Method overview
- Key quantitative result

## 3. Introduction
- Motivation
- Gap statement
- Contributions

## 4. Related Work
- Method families
- Evaluation practices

## 5. Method
- Problem setup
- Model/algorithm
- Complexity and constraints

## 6. Experiments
- Datasets and metrics
- Baselines and ablations
- Reproducibility protocol

## 7. Results
- Main table
- Robustness analysis
- Failure cases

## 8. Discussion
- Practical implications
- Limitations

## 9. Conclusion
- Findings and next steps

Generated: {_utcnow_iso()}
"""


def _default_quality_report(threshold: float) -> dict[str, Any]:
    from researchclaw.evaluation.reviews import unavailable_review
    return unavailable_review("Independent manuscript review is unavailable; no quality score was established")
