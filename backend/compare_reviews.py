"""Compare human reviews vs auto-reviews point by point.

Usage:
    python backend/compare_reviews.py                              # all papers
    python backend/compare_reviews.py --forum <forum_id>            # single paper
    python backend/compare_reviews.py --list                        # list available papers
    python backend/compare_reviews.py --missed-only                 # only show missed points
    python backend/compare_reviews.py --covered-only                # only show covered points
    python backend/compare_reviews.py --summary                    # aggregate stats only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "backend", "eval_cache")
AUTO_DIR = os.path.join(CACHE, "auto_reviews")
PARSED_DIR = os.path.join(CACHE, "parsed_human")
COVERAGE_DIR = os.path.join(CACHE, "coverage")
PAPERS_PATH = os.path.join(CACHE, "papers", "papers_index.json")

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


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HumanPoint:
    index: int
    reviewer_index: int
    type: str
    text: str


@dataclass
class AutoPoint:
    index: int
    type: str
    text: str
    dimension: str
    quality_score: int = 0


@dataclass
class MatchResult:
    human_index: int
    auto_index: int | None
    covered: bool
    notes: str = ""


@dataclass
class ComparisonPaper:
    forum: str
    title: str
    overall_score: int
    human_points: list[HumanPoint]
    auto_points: list[AutoPoint]
    matches: list[MatchResult]
    # Derived
    human_by_reviewer: dict[int, list[HumanPoint]]
    # Build mapping: human_idx -> list of matched auto_points
    coverage_map: dict[int, list[int]]  # human_index -> list of auto_indices

    @property
    def covered_count(self) -> int:
        return sum(1 for m in self.matches if m.covered)

    @property
    def total_human(self) -> int:
        return len(self.human_points)

    @property
    def coverage_pct(self) -> float:
        if not self.total_human:
            return 0
        return round(self.covered_count / self.total_human * 100, 1)

    @property
    def matched_auto_indices(self) -> set[int]:
        return {m.auto_index for m in self.matches if m.covered and m.auto_index is not None}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_json(path: str) -> dict | list | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _get_paper_title(forum: str) -> str:
    data = _load_json(PAPERS_PATH)
    if isinstance(data, list):
        for p in data:
            if p.get("forum") == forum:
                return p.get("title", "")
    return ""


def _load_parsed_human_points(forum: str) -> list[HumanPoint]:
    path = os.path.join(PARSED_DIR, f"{forum}.json")
    data = _load_json(path)
    if not data:
        return []
    if not isinstance(data, dict):
        return []

    points: list[HumanPoint] = []
    reviews_raw = data.get("reviews", [])
    for review in reviews_raw:
        r_idx = review.get("reviewer_index", 0)
        for p in review.get("points", []):
            if isinstance(p, dict) and "text" in p:
                points.append(HumanPoint(
                    index=len(points),
                    reviewer_index=r_idx,
                    type=p.get("type", "other"),
                    text=p["text"],
                ))
    return points


def _load_auto_points(forum: str) -> list[AutoPoint]:
    path = os.path.join(AUTO_DIR, f"{forum}.json")
    data = _load_json(path)
    if not data:
        return []
    points = []
    idx = 0
    for dim in data.get("dim_results", []):
        dim_id = dim.get("dimensionId", "")
        type_map = {"strengths": "strength", "weaknesses": "weakness", "suggestions": "suggestion"}
        for stype in ("strengths", "weaknesses", "suggestions"):
            for item in dim.get(stype, []):
                label = type_map.get(stype, stype)
                points.append(AutoPoint(
                    index=idx, type=label, text=item,
                    dimension=dim_id, quality_score=0,
                ))
                idx += 1
    return points


def _load_coverage(forum: str) -> tuple[list[MatchResult], list[AutoPoint], list]:
    """Load coverage data. Returns (matches, full_auto_points, raw_human)."""
    path = os.path.join(COVERAGE_DIR, f"{forum}.json")
    data = _load_json(path)
    if not data:
        return [], [], []

    raw_matches = data.get("matches", [])
    matches = []
    for m in raw_matches:
        matches.append(MatchResult(
            human_index=m.get("human_index", 0),
            auto_index=m.get("auto_index"),
            covered=m.get("covered", False),
            notes=m.get("notes", ""),
        ))

    raw_auto = data.get("auto_points", [])
    auto_points = []
    for i, ap in enumerate(raw_auto):
        auto_points.append(AutoPoint(
            index=i,
            type=ap.get("type", ""),
            text=ap.get("text", ""),
            dimension=ap.get("dimension", ""),
            quality_score=ap.get("_quality_score", 0),
        ))

    raw_human = data.get("human_points", [])
    return matches, auto_points, raw_human


def load_comparison(forum: str) -> ComparisonPaper | None:
    human_points = _load_parsed_human_points(forum)
    if not human_points:
        return None

    auto_points = _load_auto_points(forum)
    matches, _, _ = _load_coverage(forum)

    # Reconcile: ensure human_points length matches matches length
    if len(matches) > len(human_points):
        matches = matches[:len(human_points)]
    elif len(matches) < len(human_points):
        # Pad missing matches as uncovered
        last_idx = len(matches)
        for i in range(last_idx, len(human_points)):
            matches.append(MatchResult(human_index=i, auto_index=None, covered=False))

    # Build coverage_map
    coverage_map: dict[int, list[int]] = {}
    for m in matches:
        if m.covered and m.auto_index is not None:
            coverage_map.setdefault(m.human_index, []).append(m.auto_index)

    # Group human points by reviewer
    human_by_reviewer: dict[int, list[HumanPoint]] = defaultdict(list)
    for hp in human_points:
        human_by_reviewer[hp.reviewer_index].append(hp)

    title = _get_paper_title(forum)

    # Get auto score
    auto_review = _load_json(os.path.join(AUTO_DIR, f"{forum}.json"))
    score = 0
    if auto_review:
        score = auto_review.get("overall_score", 0)

    return ComparisonPaper(
        forum=forum,
        title=title,
        overall_score=score,
        human_points=human_points,
        auto_points=auto_points,
        matches=matches,
        human_by_reviewer=dict(human_by_reviewer),
        coverage_map=coverage_map,
    )


def load_all_comparisons() -> list[ComparisonPaper]:
    if not os.path.isdir(COVERAGE_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(COVERAGE_DIR)):
        if not fname.endswith(".json"):
            continue
        forum = fname.replace(".json", "")
        comp = load_comparison(forum)
        if comp:
            results.append(comp)
    return results


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _wrap(text: str, indent: int = 4, width: int = 72) -> str:
    import textwrap
    prefix = " " * indent
    return textwrap.fill(text.strip(), width=width,
                          initial_indent=prefix,
                          subsequent_indent=prefix,
                          break_long_words=False,
                          replace_whitespace=True)


def format_comparison(comp: ComparisonPaper, filter_mode: str = "all") -> str:
    """Format comparison output.

    filter_mode: "all", "covered", "missed"
    """
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append(f"  {comp.title or comp.forum}")
    lines.append(f"  Forum: {comp.forum}  |  Auto Score: {comp.overall_score}/100")
    lines.append(f"  Coverage: {comp.covered_count}/{comp.total_human} "
                 f"({comp.coverage_pct}%)")
    lines.append(f"  Auto points generated: {len(comp.auto_points)}")
    lines.append("=" * 80)

    # Get reviewer info from papers_index
    reviewer_recs = _get_reviewer_recommendations(comp.forum)

    for reviewer_idx in sorted(comp.human_by_reviewer.keys()):
        points = comp.human_by_reviewer[reviewer_idx]
        rec = reviewer_recs.get(reviewer_idx, "")

        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  REVIEWER {reviewer_idx + 1}" + (f"  |  {rec}" if rec else ""))
        lines.append("─" * 72)

        for hp in points:
            match = next((m for m in comp.matches if m.human_index == hp.index), None)
            is_covered = match.covered if match else False

            if filter_mode == "covered" and not is_covered:
                continue
            if filter_mode == "missed" and is_covered:
                continue

            icon = "✓" if is_covered else "✗"
            tag = hp.type.upper()
            lines.append(f"\n  [{icon}] {tag}:  {hp.text}")

            if is_covered and match and match.auto_index is not None:
                # Show matching auto point(s)
                auto_indices = comp.coverage_map.get(hp.index, [match.auto_index])
                for ai in auto_indices:
                    if ai < len(comp.auto_points):
                        ap = comp.auto_points[ai]
                        dim_label = DIMENSION_LABELS.get(ap.dimension, ap.dimension)
                        lines.append(f"       └ auto [{dim_label}/{ap.type}]: {ap.text}")
            elif not is_covered:
                lines.append(f"       └ (no auto match)")

    # Unmatched auto points (auto points not matching any human point)
    matched_set = comp.matched_auto_indices
    unmatched_auto = [ap for ap in comp.auto_points if ap.index not in matched_set]
    if unmatched_auto and filter_mode != "covered":
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  ADDITIONAL AUTO POINTS ({len(unmatched_auto)} unmatched)")
        lines.append("─" * 72)
        for ap in unmatched_auto[:15]:  # cap display
            dim_label = DIMENSION_LABELS.get(ap.dimension, ap.dimension)
            lines.append(f"  [{dim_label}/{ap.type}]: {ap.text[:100]}")
        if len(unmatched_auto) > 15:
            lines.append(f"  ... and {len(unmatched_auto) - 15} more")

    lines.append("")
    return "\n".join(lines)


def _get_reviewer_recommendations(forum: str) -> dict[int, str]:
    """Extract reviewer recommendations from papers_index."""
    data = _load_json(PAPERS_PATH)
    if not isinstance(data, list):
        return {}
    for p in data:
        if p.get("forum") == forum:
            recs = {}
            for i, r in enumerate(p.get("reviews", [])):
                recs[i] = r.get("recommendation", "")
            return recs
    return {}


def format_summary(comps: list[ComparisonPaper]) -> str:
    """Aggregate comparison summary across papers."""
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append(f"  HUMAN vs AUTO REVIEW COMPARISON SUMMARY")
    lines.append(f"  Papers: {len(comps)}")
    lines.append("=" * 80)

    total_human = 0
    total_covered = 0
    total_auto = 0
    coverage_by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "covered": 0})

    for comp in comps:
        total_human += comp.total_human
        total_covered += comp.covered_count
        total_auto += len(comp.auto_points)
        for hp in comp.human_points:
            match = next((m for m in comp.matches if m.human_index == hp.index), None)
            is_covered = match.covered if match else False
            coverage_by_type[hp.type]["total"] += 1
            if is_covered:
                coverage_by_type[hp.type]["covered"] += 1

    lines.append(f"\n  Overall Coverage: {total_covered}/{total_human} "
                 f"({round(total_covered / max(total_human, 1) * 100, 1)}%)")
    lines.append(f"  Total Auto Points: {total_auto}")
    lines.append(f"  Avg Human Points/Paper: {round(total_human / max(len(comps), 1), 1)}")

    lines.append(f"\n  Coverage by type:")
    for t in ["strength", "weakness", "suggestion"]:
        d = coverage_by_type.get(t, {"total": 0, "covered": 0})
        pct = round(d["covered"] / max(d["total"], 1) * 100, 1) if d["total"] else 0
        lines.append(f"    {t:<15}  {d['covered']:>3}/{d['total']:<3} ({pct}%)")

    lines.append(f"\n  Per-paper breakdown:")
    lines.append(f"  {'Title':<56} {'Cov%':>5} {'Human':>5} {'S':>3} {'W':>3} {'Sg':>3} {'Auto':>5} {'S':>3} {'W':>3} {'Sg':>3}")
    lines.append(f"  " + "-" * 96)
    for comp in sorted(comps, key=lambda c: c.coverage_pct, reverse=True):
        h_str = sum(1 for hp in comp.human_points if hp.type == "strength")
        h_wk = sum(1 for hp in comp.human_points if hp.type == "weakness")
        h_sg = sum(1 for hp in comp.human_points if hp.type == "suggestion")
        a_str = sum(1 for ap in comp.auto_points if ap.type == "strength")
        a_wk = sum(1 for ap in comp.auto_points if ap.type == "weakness")
        a_sg = sum(1 for ap in comp.auto_points if ap.type == "suggestion")
        title_short = (comp.title[:53] + "...") if len(comp.title) > 56 else comp.title
        lines.append(f"  {title_short:<56} {comp.coverage_pct:>4.1f}% {comp.total_human:>4} {h_str:>3} {h_wk:>3} {h_sg:>3} {len(comp.auto_points):>4} {a_str:>3} {a_wk:>3} {a_sg:>3}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def list_papers():
    """List available comparison papers."""
    comps = load_all_comparisons()
    print(f"{'FORUM':<20} {'COV%':>5} {'SCORE':>5}  {'Title':<50} {'Human':>5} {'S':>3} {'W':>3} {'Sg':>3} {'Auto':>5} {'S':>3} {'W':>3} {'Sg':>3}")
    print("-" * 120)
    for c in sorted(comps, key=lambda c: c.coverage_pct, reverse=True):
        h_str = sum(1 for hp in c.human_points if hp.type == "strength")
        h_wk = sum(1 for hp in c.human_points if hp.type == "weakness")
        h_sg = sum(1 for hp in c.human_points if hp.type == "suggestion")
        a_str = sum(1 for ap in c.auto_points if ap.type == "strength")
        a_wk = sum(1 for ap in c.auto_points if ap.type == "weakness")
        a_sg = sum(1 for ap in c.auto_points if ap.type == "suggestion")
        title = (c.title[:47] + "...") if len(c.title) > 50 else c.title
        print(f"{c.forum:<20} {c.coverage_pct:>4.1f}% {c.overall_score:>4}  {title:<50} {c.total_human:>4} {h_str:>3} {h_wk:>3} {h_sg:>3} {len(c.auto_points):>4} {a_str:>3} {a_wk:>3} {a_sg:>3}")
    if comps:
        print(f"\nTotal: {len(comps)} papers")


def main():
    parser = argparse.ArgumentParser(description="Compare human vs auto reviews")
    parser.add_argument("--forum", help="Show specific forum ID")
    parser.add_argument("--list", action="store_true", help="List available papers")
    parser.add_argument("--missed-only", action="store_true", help="Only show missed points")
    parser.add_argument("--covered-only", action="store_true", help="Only show covered points")
    parser.add_argument("--summary", action="store_true", help="Aggregate stats only")
    parser.add_argument("-o", "--output", help="Save output to file")
    args = parser.parse_args()

    if args.list:
        list_papers()
        return

    if args.summary:
        comps = load_all_comparisons()
        print(format_summary(comps))
        return

    if args.forum:
        comp = load_comparison(args.forum)
        if not comp:
            print(f"No data found for forum: {args.forum}")
            return
        filter_mode = "missed" if args.missed_only else ("covered" if args.covered_only else "all")
        output = format_comparison(comp, filter_mode=filter_mode)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Saved to {args.output}")
        else:
            print(output)
        return

    # Default: show summary + detailed per paper
    comps = load_all_comparisons()
    if not comps:
        print("No comparison data found.")
        return
    output = format_summary(comps)
    output += "\n" + "=" * 80 + "\n  DETAILED COMPARISONS\n" + "=" * 80 + "\n"
    for c in comps:
        output += "\n" + format_comparison(c, filter_mode="all")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
