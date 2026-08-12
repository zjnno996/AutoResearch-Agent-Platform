"""Read and display human reviews from papers_index.json in readable format.

Usage:
    python backend/read_human_reviews.py                          # all papers
    python backend/read_human_reviews.py --forum <forum_id>        # single paper
    python backend/read_human_reviews.py --list                    # list available papers
    python backend/read_human_reviews.py --markdown               # output as markdown
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
PAPERS_PATH = os.path.join(ROOT, "backend", "eval_cache", "papers", "papers_index.json")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HumanReview:
    """A single human reviewer's feedback on a paper."""
    reviewer_num: int
    recommendation: str
    confidence: str
    strengths: str
    weaknesses: str
    clarity: str
    summary_of_review: str
    summary_of_paper: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def all_text(self) -> str:
        """All free-text fields concatenated, for search/analysis."""
        parts = [
            self.strengths,
            self.weaknesses,
            self.clarity,
            self.summary_of_review,
            self.summary_of_paper,
        ]
        return "\n\n".join(p for p in parts if p.strip())


@dataclass
class PaperReviews:
    """All human reviews for one paper."""
    forum: str
    title: str
    venue: str
    abstract: str
    reviews: list[HumanReview]


# ---------------------------------------------------------------------------
# Data loader
# ---------------------------------------------------------------------------

def load_papers(path: str = PAPERS_PATH) -> list[PaperReviews]:
    """Load all papers from the index file."""
    with open(path, encoding="utf-8") as f:
        raw_list = json.load(f)

    papers: list[PaperReviews] = []
    for raw in raw_list:
        reviews = []
        for i, r in enumerate(raw.get("reviews", [])):
            s_w = r.get("strength_and_weaknesses", "")
            strengths, weaknesses = _split_strength_weakness(s_w)

            reviews.append(HumanReview(
                reviewer_num=i + 1,
                recommendation=r.get("recommendation", ""),
                confidence=r.get("confidence", ""),
                strengths=strengths,
                weaknesses=weaknesses,
                clarity=r.get("clarity", "").strip(),
                summary_of_review=r.get("summary_of_review", "").strip(),
                summary_of_paper=r.get("summary_of_paper", "").strip(),
                raw=r,
            ))

        papers.append(PaperReviews(
            forum=raw.get("forum", ""),
            title=raw.get("title", ""),
            venue=raw.get("venue", ""),
            abstract=raw.get("abstract", "").strip(),
            reviews=reviews,
        ))

    return papers


def _split_strength_weakness(text: str) -> tuple[str, str]:
    """Split 'Strength & Weaknesses' field into strengths and weaknesses."""
    text = text.strip()
    strengths = ""
    weaknesses = ""

    # Try common section headers
    parts = text.split("Weakness")
    if len(parts) < 2:
        parts = text.split("Weakness")

    if len(parts) >= 2:
        strengths = parts[0].strip()
        weaknesses = ("Weakness" + parts[1]).strip()
    else:
        # No clear split — check for "Strength" header
        s_parts = text.split("Strength")
        if len(s_parts) >= 2:
            strengths = ("Strength" + s_parts[1]).strip()
        else:
            strengths = text

    if strengths:
        strengths = strengths.strip()
    if weaknesses:
        weaknesses = weaknesses.strip()

    return strengths, weaknesses


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_plain(paper: PaperReviews) -> str:
    """Plain-text format for one paper."""
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append(f"  {paper.title}")
    lines.append(f"  Forum: {paper.forum}  |  {paper.venue}")
    lines.append("=" * 80)

    if paper.abstract:
        lines.append(f"\n  [Abstract]")
        lines.append(f"  {_wrap(paper.abstract, width=76, indent=2)}")

    for r in paper.reviews:
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  REVIEWER {r.reviewer_num}")
        lines.append(f"  Recommendation: {r.recommendation}")
        if r.confidence:
            lines.append(f"  Confidence: {r.confidence}")
        lines.append("─" * 72)

        sec_num = 0
        for label, text in [("Strengths", r.strengths),
                              ("Weaknesses / Suggestions", r.weaknesses),
                              ("Clarity", r.clarity),
                              ("Summary of Review", r.summary_of_review)]:
            if text:
                sec_num += 1
                lines.append(f"\n  {sec_num}. {label}")
                numbered_text = _numbered(text)
                lines.append(f"  {_wrap(numbered_text, width=74, indent=2)}")

    lines.append("")
    return "\n".join(lines)


def format_markdown(paper: PaperReviews) -> str:
    """Markdown format for one paper."""
    lines: list[str] = []
    lines.append(f"# {paper.title}")
    lines.append(f"")
    lines.append(f"- **Forum**: `{paper.forum}`")
    lines.append(f"- **Venue**: {paper.venue}")
    lines.append(f"")

    if paper.abstract:
        lines.append(f"## Abstract")
        lines.append(f"")
        lines.append(f"{paper.abstract}")
        lines.append(f"")

    for r in paper.reviews:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## Reviewer {r.reviewer_num}")
        lines.append(f"")
        lines.append(f"- **Recommendation**: {r.recommendation}")
        if r.confidence:
            lines.append(f"- **Confidence**: {r.confidence}")
        lines.append(f"")

        if r.strengths:
            lines.append(f"### Strengths")
            lines.append(f"")
            lines.append(f"{r.strengths}")
            lines.append(f"")

        if r.weaknesses:
            lines.append(f"### Weaknesses / Suggestions")
            lines.append(f"")
            lines.append(f"{r.weaknesses}")
            lines.append(f"")

        if r.clarity:
            lines.append(f"### Clarity")
            lines.append(f"")
            lines.append(f"{r.clarity}")
            lines.append(f"")

        if r.summary_of_review:
            lines.append(f"### Summary of Review")
            lines.append(f"")
            lines.append(f"{r.summary_of_review}")
            lines.append(f"")

    lines.append("")
    return "\n".join(lines)


def _numbered(text: str) -> str:
    """Convert + / - bullet points and existing 'N.' lists to clean numbered items.
    Also strips standalone 'Strength/Weakness' header lines that are
    redundant with our own section titles.
    """
    skip_headers = {"strength:", "weakness:", "strengths:", "weaknesses:",
                    "strength", "weakness", "strengths", "weaknesses",
                    "positive", "negative", "pros:", "cons:", "pro:", "con:"}

    lines = text.split("\n")
    result: list[str] = []
    counter = 0

    for line in lines:
        stripped = line.strip()
        lower = stripped.lower().rstrip(".")

        # Skip redundant header lines
        if lower in skip_headers:
            continue

        # Bullet points: "+ item" or "- item"
        if re.match(r'^[+\-]\s', stripped):
            counter += 1
            content = re.sub(r'^[+\-]\s+', '', stripped)
            result.append(f"{counter}. {content}")
            continue

        # Already numbered "N. ..." or "N) ..." at line start — keep number but reset counter
        m = re.match(r'^(\d+)[\.\)]\s*(.*)', stripped)
        if m:
            n = int(m.group(1))
            content = m.group(2).strip()
            if content:
                counter = n
                result.append(f"{n}. {content}")
            else:
                result.append("")
            continue

        # Otherwise keep as-is (but preserve blank lines)
        result.append(stripped if stripped else "")

    # Post-process: ensure each numbered item starts on its own line
    # with a blank line before it (except the first item)
    final: list[str] = []
    prev_blank = True
    for line in result:
        is_numbered = bool(re.match(r'^\d+\.\s', line))
        if is_numbered and not prev_blank:
            final.append("")
        final.append(line)
        prev_blank = (line == "")
    return "\n".join(final)


def _wrap(text: str, width: int = 76, indent: int = 2) -> str:
    """Simple word-wrap that preserves existing line breaks."""
    prefix = " " * indent
    import textwrap
    paragraphs = re.split(r"\n\n+", text.strip())
    wrapped = []
    for p in paragraphs:
        wrapped.append(textwrap.fill(p, width=width, initial_indent=prefix,
                                     subsequent_indent=prefix, break_long_words=False,
                                     replace_whitespace=True))
    return "\n\n".join(wrapped)


# ---------------------------------------------------------------------------
# Search / filter utilities
# ---------------------------------------------------------------------------

def find_papers(papers: list[PaperReviews], keyword: str = "",
                forum: str = "") -> list[PaperReviews]:
    """Filter papers by keyword in title/abstract or exact forum."""
    result = list(papers)

    if forum:
        result = [p for p in result if p.forum == forum]
    if keyword:
        kw = keyword.lower()
        result = [
            p for p in result
            if kw in p.title.lower() or kw in p.abstract.lower()
        ]

    return result


def search_comments(papers: list[PaperReviews], query: str) -> list[tuple[PaperReviews, HumanReview, str]]:
    """Search all human review text for a query string.
    Returns (paper, review, matched_text) tuples.
    """
    q = query.lower()
    hits: list[tuple[PaperReviews, HumanReview, str]] = []
    for p in papers:
        for r in p.reviews:
            all_text = r.all_text.lower()
            if q in all_text:
                # Find the relevant sentence(s) containing the match
                for field_name in ["strengths", "weaknesses", "clarity",
                                    "summary_of_review", "summary_of_paper"]:
                    val = getattr(r, field_name, "")
                    if q in val.lower():
                        hits.append((p, r, f"[{field_name}] {val[:300]}"))
    return hits


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def compute_stats(papers: list[PaperReviews]) -> dict[str, Any]:
    """Aggregate statistics over papers."""
    total_papers = len(papers)
    total_reviews = sum(len(p.reviews) for p in papers)
    rec_counts: dict[str, int] = {}
    for p in papers:
        for r in p.reviews:
            rec = r.recommendation.split(":")[0].strip()
            rec_counts[rec] = rec_counts.get(rec, 0) + 1

    return {
        "total_papers": total_papers,
        "total_reviews": total_reviews,
        "avg_reviews_per_paper": round(total_reviews / max(total_papers, 1), 1),
        "recommendation_distribution": dict(sorted(rec_counts.items(),
                                                    key=lambda x: int(x[0]) if x[0].isdigit() else 99)),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Read human reviews from papers_index.json")
    parser.add_argument("--forum", help="Show only a specific forum ID")
    parser.add_argument("--keyword", help="Filter papers by keyword in title/abstract")
    parser.add_argument("--search", help="Search all review text for a query string")
    parser.add_argument("--list", action="store_true", help="List available papers")
    parser.add_argument("--markdown", action="store_true", help="Output in markdown format")
    parser.add_argument("--stats", action="store_true", help="Show aggregate statistics")
    parser.add_argument("-o", "--output", help="Save output to file")
    args = parser.parse_args()

    papers = load_papers()

    if args.list:
        print(f"{'FORUM':<20} {'TITLE'}")
        print("-" * 80)
        for p in papers:
            print(f"{p.forum:<20} {p.title[:58]}")
        return

    if args.stats:
        stats = compute_stats(papers)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    if args.search:
        hits = search_comments(papers, args.search)
        print(f"Found {len(hits)} matches for '{args.search}':\n")
        for p, r, snippet in hits:
            print(f"── {p.title}  |  Reviewer {r.reviewer_num}")
            print(f"    {snippet}\n")
        return

    # Show papers
    filtered = find_papers(papers, keyword=args.keyword or "", forum=args.forum or "")

    if not filtered:
        print("No papers match the given criteria.")
        return

    formatter = format_markdown if args.markdown else format_plain
    output = "\n\n".join(formatter(p) for p in filtered)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved to {args.output}")
    else:
        # Print page by page to stdout
        for p in filtered:
            print(formatter(p))
            print()


if __name__ == "__main__":
    main()
