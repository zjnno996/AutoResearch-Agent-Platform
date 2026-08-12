"""Export review results to LaTeX and PDF."""

from __future__ import annotations

import base64
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _score_label(score: int) -> str:
    if score >= 85:
        return "Exceptional"
    if score >= 80:
        return "Strong Accept"
    if score >= 70:
        return "Good"
    if score >= 60:
        return "Marginal"
    return "Weak/Reject"


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("summary") or item.get("issue") or item.get("suggestion") or "").strip()
    return str(item or "").strip()


def _dimension_label(item: dict[str, Any]) -> str:
    return str(item.get("label") or item.get("dimensionId") or item.get("dimension") or "").strip()


def export_latex(record: dict[str, Any]) -> str:
    """Generate a LaTeX review sheet from a review record."""
    results = record.get("results") or []
    meta = record.get("meta") or {}
    report = record.get("reportSummary") or meta.get("reportSummary") or {}
    overall = record.get("overallScore", 0)
    summary = record.get("overallSummary") or {}
    ts = record.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt = ts

    lines = [
        r"\documentclass[11pt,a4paper]{article}",
        r"\usepackage[margin=2.5cm]{geometry}",
        r"\usepackage{xcolor}",
        r"\usepackage{hyperref}",
        r"\usepackage{booktabs}",
        r"\usepackage{fancyhdr}",
        r"\pagestyle{fancy}",
        rf"\lhead{{\small ClawAI Auto Review}}",
        rf"\rhead{{\small \today}}",
        r"\begin{document}",
        "",
        r"\section*{Paper Review Summary}",
        "",
        rf"\textbf{{Paper}}: {record.get('fileName', 'Unknown')} \\",
        rf"\textbf{{Model}}: {record.get('model', 'N/A')} \\",
        rf"\textbf{{Date}}: {dt} \\",
        rf"\textbf{{Overall Score}}: \textbf{{{overall}/100}} — {_score_label(overall)} \\",
        rf"\textbf{{Dimensions}}: {record.get('dimensionCount', 0)}",
        "",
    ]

    # Overall summary section
    if report:
        lines.append(r"\subsection*{Executive Summary}")
        if report.get("overallComment"):
            lines.append(str(report.get("overallComment", "")))
            lines.append("")
        for title, key in (
            ("Strengths", "strengths"),
            ("Weaknesses", "weaknesses"),
            ("Suggestions", "suggestions"),
        ):
            items = report.get(key) or []
            if not items:
                continue
            lines.append(rf"\paragraph{{{title}}}")
            lines.append(r"\begin{itemize}")
            for item in items:
                text = _item_text(item)
                dim = _dimension_label(item) if isinstance(item, dict) else ""
                if text:
                    lines.append(rf"  \item {f'[{dim}] ' if dim else ''}{text}")
            lines.append(r"\end{itemize}")
        actions = report.get("priorityActions") or []
        if actions:
            lines.append(r"\subsection*{Priority Actions}")
            lines.append(r"\begin{enumerate}")
            for action in actions:
                issue = str(action.get("issue") or action.get("suggestion") or "").strip()
                suggestion = str(action.get("suggestion") or "").strip()
                evidence = str(action.get("evidence") or "").strip()
                dim = _dimension_label(action)
                conf = action.get("confidence")
                suffix = f" (confidence {float(conf) * 100:.0f}\\%)" if isinstance(conf, (int, float)) else ""
                if issue:
                    lines.append(rf"  \item {f'[{dim}] ' if dim else ''}{issue}{suffix}")
                if suggestion:
                    lines.append(rf"    \\ \textbf{{Suggestion}}: {suggestion}")
                if evidence:
                    lines.append(rf"    \\ \textbf{{Evidence}}: {evidence}")
            lines.append(r"\end{enumerate}")
        tasks = report.get("modificationTasks") or []
        if tasks:
            lines.append(r"\subsection*{Modification Acceptance Plan}")
            lines.append(r"\begin{enumerate}")
            for task in tasks:
                title = str(task.get("title") or task.get("action") or "").strip()
                location = str(task.get("location") or "").strip()
                action_text = str(task.get("action") or "").strip()
                deliverable = str(task.get("expectedDeliverable") or "").strip()
                acceptance = str(task.get("acceptanceCriteria") or "").strip()
                if title:
                    lines.append(rf"  \item {title}")
                if location:
                    lines.append(rf"    \\ \textbf{{Location}}: {location}")
                if action_text:
                    lines.append(rf"    \\ \textbf{{Action}}: {action_text}")
                if deliverable:
                    lines.append(rf"    \\ \textbf{{Expected deliverable}}: {deliverable}")
                if acceptance:
                    lines.append(rf"    \\ \textbf{{Acceptance}}: {acceptance}")
            lines.append(r"\end{enumerate}")
        lines.append("")
    elif summary:
        assess = summary.get("overallAssessment", "")
        rec = summary.get("recommendation", "")
        conf = summary.get("confidence", "")
        lines.append(r"\subsection*{Executive Summary}")
        lines.append(f"{assess}")
        lines.append("")
        lines.append(r"\begin{itemize}")
        for s in summary.get("topStrengths", []):
            lines.append(rf"  \item[+] {s}")
        for w in summary.get("topWeaknesses", []):
            lines.append(rf"  \item[-] {w}")
        lines.append(r"\end{itemize}")
        lines.append(rf"\textbf{{Recommendation}}: {rec}  \quad "
                     rf"\textbf{{Confidence}}: {conf}")
        lines.append("")

    # Per-dimension reviews
    lines.append(r"\newpage")
    lines.append(r"\section*{Dimension Reviews}")
    lines.append("")

    dimension_rows = report.get("dimensions") if report else None
    for r in (dimension_rows or results):
        dim_id = str(r.get("label") or r.get("dimensionId", "?"))
        score = r.get("score", 0)
        summary_text = r.get("summary", "")[:500]
        strengths = r.get("strengths", []) or []
        weaknesses = r.get("weaknesses", []) or []
        suggestions = r.get("suggestions", []) or []
        quality_flags = r.get("_quality_flags", []) or []

        lines.append(r"\subsection*{%s  —  %d/100}" % (dim_id.replace("_", " ").title(), score))
        lines.append(f"{summary_text}")
        lines.append("")

        if strengths:
            lines.append(r"\paragraph{Strengths}")
            lines.append(r"\begin{itemize}")
            for s in strengths:
                lines.append(rf"  \item {s}")
            lines.append(r"\end{itemize}")
        if weaknesses:
            lines.append(r"\paragraph{Weaknesses}")
            lines.append(r"\begin{itemize}")
            for w in weaknesses:
                lines.append(rf"  \item {w}")
            lines.append(r"\end{itemize}")
        if suggestions:
            lines.append(r"\paragraph{Suggestions}")
            lines.append(r"\begin{itemize}")
            for s in suggestions:
                lines.append(rf"  \item {s}")
            lines.append(r"\end{itemize}")
        if quality_flags:
            lines.append(r"\paragraph{⚠ Quality Flags}")
            for f in quality_flags:
                lines.append(rf"\texttt{{{f}}}\\")
        lines.append(r"\medskip")
        lines.append("")

    # Token usage
    token_usage = meta.get("token_usage") or {}
    if token_usage:
        lines.append(r"\subsection*{Token Usage}")
        lines.append(r"\begin{tabular}{lrr}")
        lines.append(r"\toprule")
        lines.append(r"Metric & Value \\")
        lines.append(r"\midrule")
        lines.append(rf"Prompt tokens & {token_usage.get('prompt_tokens', 0):,} \\")
        lines.append(rf"Completion tokens & {token_usage.get('completion_tokens', 0):,} \\")
        lines.append(rf"Total tokens & {token_usage.get('total_tokens', 0):,} \\")
        if "summary_prompt_tokens" in token_usage:
            lines.append(rf"Summary generation & "
                         rf"{token_usage.get('summary_prompt_tokens', 0) + token_usage.get('summary_completion_tokens', 0)} tokens \\")
        lines.append(r"\bottomrule")
        lines.append(r"\end{tabular}")
        lines.append("")

    lines.append(r"\vfill")
    lines.append(r"\noindent\small Generated by ClawAI Auto Review on \today")
    lines.append(r"\end{document}")

    return "\n".join(lines)


def _clean_text(text: str) -> str:
    """Replace unicode chars that aren't in latin-1 with ASCII equivalents."""
    replacements = {
        "—": "--", "–": "-",
        "“": '"', "”": '"',
        "‘": "'", "’": "'",
        "…": "...",
        " ": " ",
        "•": "-",
        "≤": "<=", "≥": ">=",
        "≈": "~=",
        "α": "alpha", "β": "beta",
        "μ": "mu", "σ": "sigma",
        "θ": "theta",
        "∂": "d",
        "∑": "Sigma",
        "∏": "Pi",
        "→": "->",
        "←": "<-",
        "°": " deg",
        "×": "x",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Remove any remaining non-latin-1 chars
    return text.encode("latin-1", errors="replace").decode("latin-1")


def export_pdf(record: dict[str, Any]) -> bytes:
    """Generate a PDF review sheet from a review record.
    Returns the PDF as bytes.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        # Fallback: generate LaTeX only (caller should handle)
        raise RuntimeError("fpdf2 not available — use export_latex instead")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Paper Review Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Meta
    pdf.set_font("Helvetica", "", 10)
    meta_items = [
        ("Paper", record.get("fileName", "Unknown")),
        ("Model", record.get("model", "N/A")),
        ("Date", record.get("timestamp", "")[:10]),
        ("Overall Score", f"{record.get('overallScore', 0)}/100"),
    ]
    for label, value in meta_items:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(35, 6, _clean_text(f"{label}:"), new_x="RIGHT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(pdf.l_margin + 35)
        pdf.multi_cell(0, 6, _clean_text(str(value)))

    pdf.ln(4)

    # Executive summary
    report = record.get("reportSummary") or record.get("meta", {}).get("reportSummary") or {}
    summary = record.get("overallSummary") or {}
    if report:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Executive Summary", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5, _clean_text(str(report.get("overallComment", ""))))
        pdf.ln(2)

        for title, key in (
            ("Strengths", "strengths"),
            ("Weaknesses", "weaknesses"),
            ("Suggestions", "suggestions"),
        ):
            items = report.get(key) or []
            if not items:
                continue
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 6, title, new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            for item in items[:5]:
                text = _item_text(item)
                dim = _dimension_label(item) if isinstance(item, dict) else ""
                if text:
                    pdf.set_x(pdf.l_margin + 5)
                    pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 5, 4.5,
                                   _clean_text(f"- {f'[{dim}] ' if dim else ''}{text}"))

        actions = report.get("priorityActions") or []
        if actions:
            if pdf.get_y() > 235:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 6, "Priority Actions", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            for index, action in enumerate(actions[:5], start=1):
                dim = _dimension_label(action)
                issue = str(action.get("issue") or action.get("suggestion") or "").strip()
                suggestion = str(action.get("suggestion") or "").strip()
                evidence = str(action.get("evidence") or "").strip()
                if issue:
                    pdf.multi_cell(0, 4.5, _clean_text(f"{index}. {f'[{dim}] ' if dim else ''}{issue}"))
                if suggestion:
                    pdf.set_x(pdf.l_margin + 5)
                    pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 5, 4.5,
                                   _clean_text(f"Suggestion: {suggestion}"))
                if evidence:
                    pdf.set_x(pdf.l_margin + 5)
                    pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 5, 4.5,
                                   _clean_text(f"Evidence: {evidence}"))
        tasks = report.get("modificationTasks") or []
        if tasks:
            if pdf.get_y() > 220:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 6, "Modification Acceptance Plan", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            for index, task in enumerate(tasks[:5], start=1):
                title = str(task.get("title") or task.get("action") or "").strip()
                location = str(task.get("location") or "").strip()
                deliverable = str(task.get("expectedDeliverable") or "").strip()
                acceptance = str(task.get("acceptanceCriteria") or "").strip()
                if title:
                    pdf.multi_cell(0, 4.5, _clean_text(f"{index}. {title}"))
                for label, value in (
                    ("Location", location),
                    ("Expected deliverable", deliverable),
                    ("Acceptance", acceptance),
                ):
                    if value:
                        pdf.set_x(pdf.l_margin + 5)
                        pdf.multi_cell(
                            pdf.w - pdf.l_margin - pdf.r_margin - 5,
                            4.5,
                            _clean_text(f"{label}: {value}"),
                        )
        pdf.ln(4)
    elif summary and summary.get("overallAssessment"):
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Executive Summary", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5, _clean_text(summary["overallAssessment"]))
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, _clean_text(f"Recommendation: {summary.get('recommendation', 'N/A')}"),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # Per-dimension results
    results = report.get("dimensions") if report else None
    if not results:
        results = record.get("results") or []
    for r in results:
        dim_id = r.get("label") or r.get("dimensionId", "?")
        score = r.get("score", 0)
        summary_text = r.get("summary", "")[:400]

        # Check if we need a new page
        if pdf.get_y() > 240:
            pdf.add_page()

        pdf.set_font("Helvetica", "B", 11)
        label = dim_id.replace("_", " ").title()
        pdf.cell(0, 7, _clean_text(f"{label} -- {score}/100"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 4.5, _clean_text(summary_text))
        pdf.ln(2)

        # Strengths
        strengths = r.get("strengths") or []
        if strengths:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, "Strengths:", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            for s in strengths[:3]:
                pdf.set_x(pdf.l_margin + 5)
                pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 5, 4.5,
                              _clean_text(f"- {s}"))
        # Weaknesses
        weaknesses = r.get("weaknesses") or []
        if weaknesses:
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, "Weaknesses:", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            for w in weaknesses[:3]:
                pdf.set_x(pdf.l_margin + 5)
                pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 5, 4.5,
                              _clean_text(f"- {w}"))
        # Suggestions
        suggestions = r.get("suggestions") or []
        if suggestions:
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, "Suggestions:", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            for s in suggestions[:3]:
                pdf.set_x(pdf.l_margin + 5)
                pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 5, 4.5,
                              _clean_text(f"- {s}"))
        pdf.ln(3)

    # Token usage
    token_usage = record.get("meta", {}).get("token_usage") or {}
    if token_usage:
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, "Token Usage", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for k, v in token_usage.items():
            pdf.cell(0, 5, _clean_text(f"  {k}: {v:,}"), new_x="LMARGIN", new_y="NEXT")

    return pdf.output()
