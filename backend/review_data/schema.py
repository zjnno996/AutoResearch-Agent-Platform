"""Normalized schema for peer review data across all sources.

Every source (Reviewer2, PeerRead, OpenReview) is converted to this format.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ReviewItem:
    """A single review comment point (strength, weakness, or suggestion)."""

    text: str
    cited_section: str = ""  # e.g. "§3.2", "Table 1"


@dataclass
class DimensionReview:
    """A review scored along a specific dimension.

    For source data that doesn't have per-dimension scores (most real reviews
    are overall), we infer or leave the dimension open.
    """

    dimension_id: str = ""  # e.g. "methodology", "novelty"
    dimension_label: str = ""

    score: float | None = None  # 0-10 normalized (OpenReview/PeerRead scale)
    summary: str = ""
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    analysis: str = ""  # Rationale / evidence cited


@dataclass
class Review:
    """A complete review for one paper, normalized across all sources.

    This is the single canonical format used throughout the system.
    """

    # Metadata
    source: str  # "reviewer2", "peerread", "openreview", etc.
    source_id: str = ""  # Original ID in the source dataset
    paper_title: str = ""
    paper_venue: str = ""
    paper_year: int = 0
    paper_keywords: list[str] = field(default_factory=list)

    # The reviewer's overall assessment
    overall_score: float | None = None  # 0-10 or 0-100, normalised
    recommendation: str = ""  # "accept", "weak-accept", "reject", etc.
    confidence: float | None = None  # Reviewer confidence 1-5

    # The review text parsed into sections
    comment_to_author: str = ""  # Full free-text review
    comment_to_ac: str = ""  # Private comment (if available)

    # Structured strengths / weaknesses (if annotated)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    # Per-dimension breakdown (if available, e.g. Reviewer2)
    dimensions: list[DimensionReview] = field(default_factory=list)

    @property
    def score_100(self) -> float | None:
        """Return score normalized to 0-100."""
        if self.overall_score is None:
            return None
        return self.overall_score * 10 if self.overall_score <= 10 else self.overall_score

    @property
    def score_label(self) -> str:
        """Categorical label for example quality."""
        s = self.overall_score or self.score_100 or 0
        if s >= 80 or (s > 7 and self.overall_score is not None and self.overall_score <= 10):
            return "high_quality"
        elif s >= 50 or s >= 5:
            return "medium_quality"
        else:
            return "low_quality"

    def to_fewshot_block(self, max_chars: int = 800) -> str:
        """Format as a compact few-shot example string for prompt injection.

        Uses the comment_to_author or structured fields, truncated to max_chars.
        """
        parts: list[str] = []

        if self.strengths:
            parts.append("Strengths:\n" + "\n".join(f"- {s}" for s in self.strengths[:3]))
        if self.weaknesses:
            parts.append("Weaknesses:\n" + "\n".join(f"- {w}" for w in self.weaknesses[:3]))
        if self.suggestions:
            parts.append("Suggestions:\n" + "\n".join(f"- {s}" for s in self.suggestions[:3]))

        if not parts and self.comment_to_author:
            # Use raw text if no structured fields
            text = self.comment_to_author[:max_chars]
            return text

        result = "\n\n".join(parts)
        return result[:max_chars]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewDataset:
    """A collection of reviews from one source."""

    name: str
    source: str
    reviews: list[Review] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.reviews)

    def filter_by_quality(self, min_score: float = 0.0, max_score: float = 10.0) -> ReviewDataset:
        """Filter to reviews within a score range."""
        return ReviewDataset(
            name=f"{self.name}_filtered",
            source=self.source,
            reviews=[r for r in self.reviews if r.overall_score is not None
                     and min_score <= r.overall_score <= max_score]
        )

    def sample(self, n: int, seed: int = 42) -> ReviewDataset:
        """Random sample of reviews."""
        import random
        rng = random.Random(seed)
        sampled = rng.sample(self.reviews, min(n, len(self.reviews)))
        return ReviewDataset(name=f"{self.name}_sample_{n}", source=self.source, reviews=sampled)

    def describe(self) -> None:
        """Print statistics about this dataset."""
        import statistics

        print(f"\n=== Dataset: {self.name} (source: {self.source}) ===")
        print(f"Total reviews: {len(self.reviews)}")

        scores = [r.overall_score for r in self.reviews if r.overall_score is not None]
        if scores:
            print(f"Score range: {min(scores):.1f} - {max(scores):.1f}")
            print(f"Score mean ± std: {statistics.mean(scores):.1f} ± {statistics.stdev(scores):.2f}")
        else:
            print("Scores: none")

        with_strengths = sum(1 for r in self.reviews if r.strengths)
        with_weaknesses = sum(1 for r in self.reviews if r.weaknesses)
        with_suggestions = sum(1 for r in self.reviews if r.suggestions)
        print(f"Reviews with strengths: {with_strengths} ({with_strengths/max(len(self.reviews),1)*100:.0f}%)")
        print(f"Reviews with weaknesses: {with_weaknesses} ({with_weaknesses/max(len(self.reviews),1)*100:.0f}%)")
        print(f"Reviews with suggestions: {with_suggestions} ({with_suggestions/max(len(self.reviews),1)*100:.0f}%)")

        venues = set(r.paper_venue for r in self.reviews if r.paper_venue)
        if venues:
            print(f"Venues: {', '.join(sorted(venues))}")

        keywords = [kw for r in self.reviews for kw in r.paper_keywords if kw]
        if keywords:
            top_kw = statistics.mode(keywords) if len(keywords) > 5 else keywords[0] if keywords else ""
            print(f"Total keywords: {len(keywords)} unique")
        print()
