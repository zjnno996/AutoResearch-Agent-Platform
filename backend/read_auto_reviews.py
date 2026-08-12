"""Read and display auto-reviews in a readable format, similar to read_human_reviews.py.

Usage:
    python backend/read_auto_reviews.py                           # all papers
    python backend/read_auto_reviews.py --forum <forum_id>         # single paper
    python backend/read_auto_reviews.py --list                     # list available papers
    python backend/read_auto_reviews.py --dim <dim_id>             # filter by dimension
    python backend/read_auto_reviews.py --markdown                 # output as markdown
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTO_DIR = os.path.join(ROOT, "backend", "eval_cache", "auto_reviews")
PAPERS_PATH = os.path.join(ROOT, "backend", "eval_cache", "papers", "papers_index.json")
PARSED_DIR = os.path.join(ROOT, "backend", "eval_cache", "parsed_human")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

DIMENSION_LABELS: dict[str, str] = {
    "methodology": "Methodology",
    "novelty": "Novelty / Contribution",
    "experiment": "Experiments",
    "writing": "Writing & Clarity",
    "related_work": "Related Work",
    "reproducibility": "Reproducibility",
    "ethics": "Ethics",
    "skeptic": "Skeptic (Cross-check)",
    "deep_dive": "Deep Dive (Technical)",
    "patch": "Patch (Fixes)",
}

DIMENSION_ORDER = [
    "methodology", "novelty", "experiment", "writing", "related_work",
    "reproducibility", "ethics", "skeptic", "deep_dive", "patch",
]


@dataclass
class AutoDimensionResult:
    dim_id: str
    label: str
    score: int
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    suggestions: list[str]

    @property
    def all_text(self) -> str:
        return "\n\n".join(filter(None, [self.summary] + self.strengths + self.weaknesses + self.suggestions))


@dataclass
class AutoReview:
    forum: str
    title: str
    overall_score: int
    overall_summary: str
    dimensions: list[AutoDimensionResult]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_title(forum: str) -> str:
    """Get paper title from papers_index by forum."""
    try:
        with open(PAPERS_PATH, encoding="utf-8") as f:
            papers = json.load(f)
        for p in papers:
            if p.get("forum") == forum:
                return p.get("title", "")
    except Exception:
        pass
    return ""


def load_auto_reviews(auto_dir: str = AUTO_DIR) -> list[AutoReview]:
    """Load all auto-review JSON files."""
    if not os.path.isdir(auto_dir):
        print(f"Auto-review directory not found: {auto_dir}", file=sys.stderr)
        return []

    results: list[AutoReview] = []
    for fname in sorted(os.listdir(auto_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(auto_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"Error loading {fname}: {e}", file=sys.stderr)
            continue

        forum = raw.get("forum", fname.replace(".json", ""))
        title = raw.get("title", "") or _load_title(forum)

        dims = []
        for dim_raw in raw.get("dim_results", []):
            did = dim_raw.get("dimensionId", "")
            dims.append(AutoDimensionResult(
                dim_id=did,
                label=DIMENSION_LABELS.get(did, did.replace("_", " ").title()),
                score=dim_raw.get("score", 0),
                summary=dim_raw.get("summary") or "",
                strengths=dim_raw.get("strengths", []),
                weaknesses=dim_raw.get("weaknesses", []),
                suggestions=dim_raw.get("suggestions", []),
            ))

        # Sort dimensions by standard order
        dim_order = {d: i for i, d in enumerate(DIMENSION_ORDER)}
        dims.sort(key=lambda d: dim_order.get(d.dim_id, 999))

        results.append(AutoReview(
            forum=forum,
            title=title,
            overall_score=raw.get("overall_score", 0),
            overall_summary=_extract_summary(raw.get("overall_summary", "")),
            dimensions=dims,
        ))

    return results


def _extract_summary(raw_val: Any) -> str:
    """Extract a summary string from various formats."""
    if isinstance(raw_val, dict):
        for key in ("overallAssessment", "overallSummary", "summary"):
            val = raw_val.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    if isinstance(raw_val, str):
        return raw_val
    return str(raw_val) if raw_val else ""


def find_auto_reviews(reviews: list[AutoReview], forum: str = "",
                       keyword: str = "") -> list[AutoReview]:
    results = list(reviews)
    if forum:
        results = [r for r in results if r.forum == forum]
    if keyword:
        kw = keyword.lower()
        results = [r for r in results if kw in r.title.lower()]
    return results


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _numbered(text: str) -> str:
    """Convert bullet-point text to numbered items."""
    lines = text.strip().split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^[+\-]\s', stripped):
            content = re.sub(r'^[+\-]\s+', '', stripped)
            result.append(f"• {content}")
        else:
            result.append(stripped if stripped else "")
    return "\n".join(result)


def _wrap_list(items: list[str], indent: int = 4) -> str:
    """Format a list of items as numbered text."""
    if not items:
        return ""
    prefix = " " * indent
    lines = []
    for i, item in enumerate(items, 1):
        wrapped = _wrap_text(f"({i}) {item}", width=76 - indent, indent=0)
        for j, wline in enumerate(wrapped.split("\n")):
            lines.append(f"{prefix}{wline}" if j > 0 else f"{prefix}{wline}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _wrap_text(text: str, width: int = 76, indent: int = 0) -> str:
    import textwrap
    prefix = " " * indent
    paragraphs = re.split(r"\n\n+", text.strip())
    wrapped = []
    for p in paragraphs:
        p_clean = re.sub(r"\s+", " ", p).strip()
        wrapped.append(textwrap.fill(p_clean, width=width,
                                      initial_indent=prefix,
                                      subsequent_indent=prefix,
                                      break_long_words=False,
                                      replace_whitespace=True))
    return "\n\n".join(wrapped)


def format_plain(rev: AutoReview) -> str:
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append(f"  {rev.title}")
    lines.append(f"  Forum: {rev.forum}  |  AUTO-REVIEW")
    lines.append(f"  Overall Score: {rev.overall_score}/100")
    lines.append("=" * 80)

    if rev.overall_summary:
        lines.append(f"\n  Overview")
        lines.append(f"  {_wrap_text(rev.overall_summary, width=76, indent=2)}")

    for dim in rev.dimensions:
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  {dim.label}  (score: {dim.score})")
        lines.append("─" * 72)

        if dim.summary:
            lines.append(f"\n  Summary")
            lines.append(f"  {_wrap_text(dim.summary, width=74, indent=2)}")

        for label_key, items in [("Strengths", dim.strengths),
                                  ("Weaknesses", dim.weaknesses),
                                  ("Suggestions", dim.suggestions)]:
            if items:
                lines.append(f"\n  ◆ {label_key}")
                lines.append(f"  {_wrap_list(items, indent=4)}")

    lines.append("")
    return "\n".join(lines)


def format_markdown(rev: AutoReview) -> str:
    lines: list[str] = []
    lines.append(f"# {rev.title}")
    lines.append(f"")
    lines.append(f"- **Forum**: `{rev.forum}`")
    lines.append(f"- **Auto-Review Score**: {rev.overall_score}/100")
    lines.append(f"")

    if rev.overall_summary:
        lines.append(f"## Overview")
        lines.append(f"")
        lines.append(f"{rev.overall_summary}")
        lines.append(f"")

    for dim in rev.dimensions:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## {dim.label}  (score: {dim.score})")
        lines.append(f"")

        if dim.summary:
            lines.append(f"{dim.summary}")
            lines.append(f"")

        for label_key, items in [("Strengths", dim.strengths),
                                  ("Weaknesses", dim.weaknesses),
                                  ("Suggestions", dim.suggestions)]:
            if items:
                lines.append(f"### {label_key}")
                lines.append(f"")
                for item in items:
                    lines.append(f"- {item}")
                    lines.append(f"")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Read auto-reviews")
    parser.add_argument("--forum", help="Show only a specific forum ID")
    parser.add_argument("--keyword", help="Filter papers by keyword in title")
    parser.add_argument("--dim", help="Show specific dimension only")
    parser.add_argument("--list", action="store_true", help="List available papers")
    parser.add_argument("--markdown", action="store_true", help="Output as markdown")
    parser.add_argument("-o", "--output", help="Save output to file")
    args = parser.parse_args()

    reviews = load_auto_reviews()

    if args.list:
        print(f"{'FORUM':<20} {'SCORE'}  {'TITLE'}")
        print("-" * 80)
        for r in reviews:
            print(f"{r.forum:<20} {r.overall_score:<5}  {r.title[:55]}")
        return

    filtered = find_auto_reviews(reviews, forum=args.forum or "",
                                  keyword=args.keyword or "")

    if not filtered:
        print("No papers match the given criteria.")
        return

    formatter = format_markdown if args.markdown else format_plain
    output = "\n\n".join(formatter(r) for r in filtered)

    if args.dim:
        # Filter output to only show the specified dimension
        dim = args.dim.lower()
        filtered_out = []
        for r in filtered:
            r.dimensions = [d for d in r.dimensions if d.dim_id == dim]
            if r.dimensions:
                filtered_out.append(r)
        if not filtered_out:
            print(f"No dimension '{args.dim}' found. Options: {', '.join(DIMENSION_ORDER)}")
            return
        output = "\n\n".join(formatter(r) for r in filtered_out)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
