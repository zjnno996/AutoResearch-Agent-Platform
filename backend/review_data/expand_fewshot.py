"""Build an expanded few-shot library from real conference review data.

Pipeline:
  1. Load reviews from Reviewer2 + PeerRead sources (with cache)
  2. For each review, classify its primary dimension by keyword scoring
  3. Stratify by quality (high ≥ 7, medium 4–7, low < 4)
  4. Select the best 8–12 examples per dimension
  5. Write the expanded fewshot_library.py

Usage:
    python review_data/expand_fewshot.py [--force-download]
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Ensure we can import from review_data
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from review_data.schema import Review, DimensionReview
from review_data.sources import peerread, reviewer2


# =============================================================================
# Dimension keyword signatures
# =============================================================================

# Each dimension gets high-weight keywords (direct signal) and low-weight keywords (supporting)
DIMENSION_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "methodology": {
        "high": [
            "methodology", "method", "approach", "architecture", "design",
            "implementation", "algorithm", "framework", "pipeline",
            "theoretical", "formulation", "model design", "technical",
            "solution", "proposed method", "formula", "equation",
            "design choice", "architectural", "module", "component",
        ],
        "low": [
            "motivation", "assumption", "justification", "reasonable",
            "well-designed", "sound", "rigorous", "principle",
        ],
    },
    "novelty": {
        "high": [
            "novel", "novelty", "original", "innovative", "contribution",
            "incremental", "trivial", "new paradigm", "new approach",
            "first work", "first to", "state-of-the-art", "groundbreaking",
            "significant contribution", "technical contribution",
            "prior work", "known technique", "existing method",
        ],
        "low": [
            "creative", "insight", "interesting idea", "fresh",
            "commonplace", "standard technique", "variation",
        ],
    },
    "experiment": {
        "high": [
            "experiment", "evaluation", "baseline", "benchmark",
            "result", "dataset", "ablation", "comparison",
            "performance", "improvement", "gain", "accuracy",
            "metric", "state-of-the-art comparison", "experimental setup",
            "quantitative", "empirical", "human evaluation",
            "statistical", "significance", "confidence interval",
            "test set", "training set", "validat", "cross-validation",
        ],
        "low": [
            "table", "figure", "analysis", "report", "measure",
            "consistent", "competitive", "superior",
        ],
    },
    "writing": {
        "high": [
            "writing", "clarity", "organization", "presentation",
            "readability", "grammar", "notation", "figure",
            "table quality", "structure", "flow", "accessible",
            "well-written", "difficult to follow", "typo",
            "proofread", "section", "exposition", "prose",
        ],
        "low": [
            "language", "english", "polish", "formatting",
            "visual", "caption", "figure quality",
        ],
    },
    "related_work": {
        "high": [
            "related work", "prior work", "previous work", "existing work",
            "citation", "literature", "positioning", "context",
            "missing reference", "related literature", "not cited",
            "discussion of prior", "comparison with prior",
            "acknowledge", "prior art",
        ],
        "low": [
            "background", "survey", "overview", "reference",
            "well-cited", "comprehensive",
        ],
    },
    "reproducibility": {
        "high": [
            "reproducible", "reproducibility", "code", "code release",
            "hyperparameter", "implementation detail",
            "open source", "configuration", "random seed",
            "training detail", "environment", "dependency",
            "supplementary material", "reproduce",
            "missing detail", "insufficient detail",
        ],
        "low": [
            "documentation", "appendix", "shared", "available",
            "detail", "specification",
        ],
    },
    "ethics": {
        "high": [
            "ethics", "bias", "fairness", "privacy", "ethical",
            "societal impact", "dual-use", "misuse",
            "demographic", "environmental", "carbon",
            "negative impact", "harm", "responsible",
            "transparency", "accountability",
        ],
        "low": [
            "social", "broader impact", "discuss",
        ],
    },
}


def _calc_dimension_scores(text: str) -> dict[str, float]:
    """Score each dimension by keyword density in text."""
    text_lower = text.lower()
    scores: dict[str, float] = {}
    for dim_id, kw_groups in DIMENSION_KEYWORDS.items():
        score = 0.0
        for kw in kw_groups["high"]:
            count = text_lower.count(kw.lower())
            score += count * 2.0
        for kw in kw_groups["low"]:
            count = text_lower.count(kw.lower())
            score += count * 0.5
        # Normalize by text length
        if len(text_lower) > 0:
            score = score / max(1.0, len(text_lower) / 500.0)  # per ~500 chars
        scores[dim_id] = score
    return scores


def _classify_dimension(review: Review) -> str | None:
    """Classify a review into its primary dimension based on text content.

    Uses the combined text of strengths, weaknesses, suggestions, and comment.
    Returns the dimension_id with the highest keyword score, or None if below threshold.
    """
    text_parts = [
        review.comment_to_author,
        " ".join(review.strengths),
        " ".join(review.weaknesses),
        " ".join(review.suggestions),
    ]
    text = " ".join(p for p in text_parts if p)
    if not text or len(text) < 50:
        return None

    scores = _calc_dimension_scores(text)

    # Also boost based on paper keywords matching
    for kw in review.paper_keywords:
        kw_lower = kw.lower()
        for dim_id, kw_groups in DIMENSION_KEYWORDS.items():
            if any(kw_term in kw_lower for kw_term in kw_groups["high"]):
                scores[dim_id] += 1.0

    # Return dimension with highest score, if above threshold
    best_dim = max(scores, key=scores.get)
    best_score = scores[best_dim]
    second_score = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0

    # Require: best score >= 1.0 AND best is at least 20% higher than second
    if best_score >= 1.0 and (best_score - second_score) > 0.3:
        return best_dim
    # If close, check if it's at least clearly above threshold
    if best_score >= 2.0:
        return best_dim

    return None


# =============================================================================
# Quality helpers
# =============================================================================

def _quality_stratum(score: float | None) -> str:
    """Assign quality stratum based on overall_score (0-10 scale)."""
    if score is None:
        return "unknown"
    if score >= 7.0:
        return "high"
    elif score >= 4.0:
        return "medium"
    else:
        return "low"


def _review_quality_score(review: Review) -> float:
    """Compute a combined quality score for ranking.

    Factors: overall_score (0-10), presence of structured fields, text length, evidence anchoring.
    """
    base = review.overall_score or 5.0

    # Bonus for structured content
    bonus = 0.0
    if len(review.strengths) >= 2:
        bonus += 0.5
    if len(review.weaknesses) >= 2:
        bonus += 0.5
    if len(review.suggestions) >= 2:
        bonus += 0.5

    # Bonus for evidence anchoring (section/table/figure citations)
    evidence_pattern = re.compile(r"[§\[]?\s*(\d+\.?\d*)\s*[\]\)]?|Fig(?:ure)?\.?\s*\d+|Table\s*\d+|Eq\.?\s*\d+")
    all_text = " ".join(
        review.strengths + review.weaknesses + review.suggestions + [review.comment_to_author]
    )
    evidence_count = len(evidence_pattern.findall(all_text))
    bonus += min(2.0, evidence_count * 0.3)

    return base + bonus


def _select_best_per_dimension(
    classified: dict[str, list[tuple[Review, float]]],
    target_per_dim: int = 10,
    min_high: int = 3,
    min_medium: int = 2,
    min_low: int = 2,
) -> dict[str, list[Review]]:
    """Select the best set of examples per dimension, stratified by quality."""
    result: dict[str, list[Review]] = {}

    for dim_id, candidates in classified.items():
        candidates.sort(key=lambda x: x[1], reverse=True)

        # Stratify
        high: list[Review] = []
        medium: list[Review] = []
        low: list[Review] = []
        unknown: list[Review] = []

        for review, _score in candidates:
            stratum = _quality_stratum(review.overall_score)
            if stratum == "high":
                high.append(review)
            elif stratum == "medium":
                medium.append(review)
            elif stratum == "low":
                low.append(review)
            else:
                unknown.append(review)

        selected: list[Review] = []
        seen_texts: set[str] = set()

        def _add_unique(reviews: list[Review], count: int) -> None:
            added = 0
            for r in reviews:
                key = (r.comment_to_author or "")[:100]
                if key not in seen_texts:
                    selected.append(r)
                    seen_texts.add(key)
                    added += 1
                    if added >= count:
                        break

        _add_unique(high, min_high)
        _add_unique(medium, min_medium)
        _add_unique(low, min_low)
        _add_unique(unknown, 1)

        # Fill remaining slots with best available
        remaining = target_per_dim - len(selected)
        if remaining > 0:
            for review, _score in candidates:
                key = (review.comment_to_author or "")[:100]
                if key not in seen_texts:
                    selected.append(review)
                    seen_texts.add(key)
                    if len(selected) >= target_per_dim:
                        break

        result[dim_id] = selected[:target_per_dim]

    return result


# =============================================================================
# Load all source data
# =============================================================================

def load_all_reviews(force_download: bool = False) -> dict[str, list[Review]]:
    """Load all available reviews from all sources, keyed by source name."""
    all_reviews: dict[str, list[Review]] = {}
    use_cache = not force_download

    print("=" * 60)
    print("Loading review data from all sources...")
    print("=" * 60)

    # Reviewer2
    for name in reviewer2.DATASETS:
        try:
            ds = reviewer2.load_dataset(name, use_cache=use_cache)
            all_reviews[f"reviewer2/{name}"] = ds.reviews
            print(f"  ✓ {name}: {len(ds.reviews)} reviews")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    # PeerRead
    for name in peerread.DATASETS:
        try:
            ds = peerread.load_dataset(name, use_cache=use_cache)
            all_reviews[f"peerread/{name}"] = ds.reviews
            print(f"  ✓ {name}: {len(ds.reviews)} reviews")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    total = sum(len(v) for v in all_reviews.values())
    print(f"\nTotal: {total} reviews from {len(all_reviews)} datasets")
    return all_reviews


# =============================================================================
# Classification
# =============================================================================

def classify_all(
    all_reviews: dict[str, list[Review]],
    min_text_len: int = 100,
    min_strengths: int = 1,
) -> dict[str, list[tuple[Review, float]]]:
    """Classify all reviews into dimensions.

    Returns dict[dim_id, list[(Review, quality_score)]] sorted by quality.
    """
    classified: dict[str, list[tuple[Review, float]]] = defaultdict(list)
    unclassified_count = 0
    dim_counts: Counter[str] = Counter()

    total = sum(len(v) for v in all_reviews.values())
    for source_name, reviews in all_reviews.items():
        for review in reviews:
            # Basic quality filters
            text_len = len(review.comment_to_author or "")
            if text_len < min_text_len:
                continue
            if not review.strengths or len(review.strengths) < min_strengths:
                continue
            if review.overall_score is None:
                continue

            dim = _classify_dimension(review)
            if dim is None:
                unclassified_count += 1
                continue

            quality = _review_quality_score(review)
            classified[dim].append((review, quality))
            dim_counts[dim] += 1

    print(f"\nClassification results:")
    for dim_id in sorted(dim_counts.keys()):
        print(f"  {dim_id}: {dim_counts[dim_id]} reviews")
    print(f"  (unclassified: {unclassified_count})")
    return classified


# =============================================================================
# Generate fewshot_library.py
# =============================================================================

EXPORT_HEADER = '''"""Curated few-shot review examples for each review dimension — auto-expanded from real data.

Auto-generated by expand_fewshot.py from {total} source reviews
across {src_count} datasets.

Contains high-quality review examples that demonstrate the expected
level of detail, evidence anchoring, and critical thinking for each
of the 7 review dimensions.

Each example is normalized into our Review schema so it can be used
with the RAG engine or injected directly as few-shot prompts.
"""

from __future__ import annotations

from .schema import Review, ReviewDataset, DimensionReview


def _make(
    dim_id: str,
    score: float,
    summary: str,
    strengths: list[str],
    weaknesses: list[str],
    suggestions: list[str],
    paper_title: str = "",
    paper_venue: str = "",
    paper_keywords: list[str] | None = None,
) -> Review:
    """Build a Review object quickly."""
    strengths_fixed = [s[:200] for s in strengths[:3]]
    while len(strengths_fixed) < 3:
        strengths_fixed.append("(See paper for details)")
    weaknesses_fixed = [w[:200] for w in weaknesses[:3]]
    while len(weaknesses_fixed) < 3:
        weaknesses_fixed.append("(See paper for details)")
    suggestions_fixed = [s[:200] for s in suggestions[:3]]
    while len(suggestions_fixed) < 3:
        suggestions_fixed.append("(See paper for details)")

    return Review(
        source="curated",
        paper_title=paper_title,
        paper_venue=paper_venue,
        paper_keywords=paper_keywords or [],
        overall_score=score,
        comment_to_author=summary,
        strengths=strengths_fixed,
        weaknesses=weaknesses_fixed,
        suggestions=suggestions_fixed,
        dimensions=[DimensionReview(dimension_id=dim_id, score=score, summary=summary)],
    )


# Score reference:
# 9-10/10 = exceptional (strong accept)
# 7-8/10  = good (accept)
# 5-6/10  = marginal (borderline)
# 3-4/10  = weak (reject)
# 1-2/10  = poor (strong reject)
'''

DIM_COMMENTS = {
    "methodology": "METHODOLOGY — soundness of design, technical correctness",
    "novelty": "NOVELTY — originality, significance of contribution",
    "experiment": "EXPERIMENT — evaluation thoroughness, baselines, ablations",
    "writing": "WRITING — clarity, organization, figures, presentation",
    "related_work": "RELATED WORK — literature coverage, positioning",
    "reproducibility": "REPRODUCIBILITY — code, details, transparency",
    "ethics": "ETHICS — bias, fairness, societal impact",
}


def _escape(s: str) -> str:
    """Escape a string for Python source inclusion."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_review_as_dict(review: Review, dim_id: str) -> str:
    """Format a review as a _make() call in Python source."""
    summary = (review.comment_to_author or "")[:300]
    score = min(10.0, max(1.0, review.overall_score or 5.0))

    strengths = review.strengths[:3]
    weaknesses = review.weaknesses[:3]
    suggestions = review.suggestions[:3]
    title = review.paper_title[:100]
    keywords = review.paper_keywords[:5]

    # Format as a readable _make() call
    lines = ["    _make("]
    lines.append(f'        dim_id="{dim_id}",')
    lines.append(f"        score={score:.0f},")
    lines.append(f'        summary="{_escape(summary)}",')
    lines.append("        strengths=[")
    for s in strengths:
        lines.append(f'            "{_escape(s[:200])}",')
    lines.append("        ],")
    lines.append("        weaknesses=[")
    for w in weaknesses:
        lines.append(f'            "{_escape(w[:200])}",')
    lines.append("        ],")
    lines.append("        suggestions=[")
    for s in suggestions:
        lines.append(f'            "{_escape(s[:200])}",')
    lines.append("        ],")
    if title:
        lines.append(f'        paper_title="{_escape(title)}",')
    if keywords:
        kws = ", ".join(f'"{_escape(k[:40])}"' for k in keywords)
        lines.append(f"        paper_keywords=[{kws}],")
    lines.append("    ),")

    return "\n".join(lines)


def generate_library(
    selected: dict[str, list[Review]],
    total_reviews: int,
    src_count: int,
    output_path: str = "fewshot_library.py",
) -> None:
    """Generate the expanded fewshot_library.py file."""
    lines = EXPORT_HEADER.format(total=total_reviews, src_count=src_count)
    lines += "\n\n"

    example_counts: dict[str, int] = {}

    for dim_id in [
        "methodology", "novelty", "experiment", "writing",
        "related_work", "reproducibility", "ethics",
    ]:
        examples = selected.get(dim_id, [])
        var_name = f"{dim_id.upper()}_EXAMPLES"
        comment = DIM_COMMENTS.get(dim_id, dim_id)

        lines += f"# =============================================================================\n"
        lines += f"# {comment}\n"
        lines += f"# ({len(examples)} examples)\n"
        lines += f"# =============================================================================\n\n"
        lines += f"{var_name} = [\n"

        for review in examples:
            lines += _format_review_as_dict(review, dim_id) + "\n"

        lines += "]\n\n"
        example_counts[dim_id] = len(examples)

    # Combined dataset
    lines += "# =============================================================================\n"
    lines += "# Combined dataset\n"
    lines += "# =============================================================================\n\n"
    lines += "ALL_DIMENSION_EXAMPLES: dict[str, list[Review]] = {\n"
    for dim_id in [
        "methodology", "novelty", "experiment", "writing",
        "related_work", "reproducibility", "ethics",
    ]:
        var_name = f"{dim_id.upper()}_EXAMPLES"
        lines += f'    "{dim_id}": {var_name},\n'
    lines += "}\n\n"

    # Utility functions (same as before but with the updated function signatures)
    lines += '''
def get_examples_for_dimension(
    dim_id: str,
    min_score: float | None = None,
    max_score: float | None = None,
) -> list[Review]:
    """Get few-shot examples for a specific dimension, optionally filtered by score."""
    examples = ALL_DIMENSION_EXAMPLES.get(dim_id, [])
    if min_score is not None:
        examples = [e for e in examples if e.overall_score is not None and e.overall_score >= min_score]
    if max_score is not None:
        examples = [e for e in examples if e.overall_score is not None and e.overall_score <= max_score]
    return examples


def get_all_examples() -> list[Review]:
    """Get all few-shot examples across all dimensions."""
    all_examples: list[Review] = []
    for examples in ALL_DIMENSION_EXAMPLES.values():
        all_examples.extend(examples)
    return all_examples


def format_fewshot_block(examples: list[Review], max_chars: int = 1500) -> str:
    """Format a list of review examples as a few-shot prompt block."""
    if not examples:
        return ""

    parts: list[str] = []
    for i, ex in enumerate(examples[:3]):
        score_str = f"Score: {ex.overall_score:.0f}/10" if ex.overall_score is not None else ""
        block = f"Reference Example {i+1}: {score_str}\\n"
        if ex.strengths:
            block += "Strengths:\\n" + "\\n".join(f"- {s}" for s in ex.strengths[:2]) + "\\n"
        if ex.weaknesses:
            block += "Weaknesses:\\n" + "\\n".join(f"- {w}" for w in ex.weaknesses[:2]) + "\\n"
        if ex.suggestions:
            block += "Suggestions:\\n" + "\\n".join(f"- {s}" for s in ex.suggestions[:1]) + "\\n"
        if len(block) > 600:
            block = block[:600] + "...\\n"
        parts.append(block.strip())

    result = "\\n\\n".join(parts)
    return result[:max_chars]


def get_fewshot_dataset() -> ReviewDataset:
    """Get all curated examples as a ReviewDataset."""
    return ReviewDataset(
        name="curated_fewshot",
        source="curated",
        reviews=get_all_examples(),
    )
'''

    output_path = str(SCRIPT_DIR / output_path)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(lines)

    total_examples = sum(example_counts.values())
    print(f"\n{'=' * 60}")
    print(f"Generated {output_path}")
    print(f"Total: {total_examples} examples across 7 dimensions")
    print(f"{'=' * 60}")
    for dim_id, count in sorted(example_counts.items()):
        print(f"  {dim_id}: {count} examples")
    print()


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    force = "--force-download" in sys.argv

    # Step 1: Load all data
    all_reviews = load_all_reviews(force_download=force)
    total = sum(len(v) for v in all_reviews.values())
    src_count = len(all_reviews)

    if total == 0:
        print("ERROR: No review data loaded. Check network connectivity.")
        sys.exit(1)

    # Step 2: Classify
    print(f"\n{'=' * 60}")
    print("Classifying reviews into dimensions...")
    print("=" * 60)
    classified = classify_all(all_reviews)

    # Print classification stats
    for dim_id in [
        "methodology", "novelty", "experiment", "writing",
        "related_work", "reproducibility", "ethics",
    ]:
        items = classified.get(dim_id, [])
        if items:
            scores = [s for _, s in items]
            avg_quality = sum(scores) / len(scores)
            print(f"  {dim_id}: {len(items)} candidates, avg quality {avg_quality:.1f}")

    # Step 3: Select best examples per dimension
    print(f"\n{'=' * 60}")
    print("Selecting best examples per dimension...")
    print("=" * 60)
    selected = _select_best_per_dimension(
        classified,
        target_per_dim=10,
        min_high=3,
        min_medium=3,
        min_low=2,
    )

    for dim_id in [
        "methodology", "novelty", "experiment", "writing",
        "related_work", "reproducibility", "ethics",
    ]:
        examples = selected.get(dim_id, [])
        score_counts: Counter[str] = Counter()
        for r in examples:
            score_counts[_quality_stratum(r.overall_score)] += 1
        print(f"  {dim_id}: {len(examples)} selected "
              f"(high={score_counts.get('high',0)}, "
              f"medium={score_counts.get('medium',0)}, "
              f"low={score_counts.get('low',0)})")

    # Step 4: Generate library
    generate_library(selected, total, src_count)
