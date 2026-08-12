"""Reviewer2 dataset loader.

Reviewer2 (NeuLab @ CMU) provides annotated review data from NeurIPS/ICLR.
Each entry has structured strengths/weaknesses/suggestions and per-paper metadata.

Data format: JSONL, each line is one review with:
  - review_text: the full review
  - strengths/weaknesses/suggestions: extracted lists
  - rating: numeric score
  - confidence: reviewer confidence
  - paper_title, paper_abstract, paper_keywords

Source: https://github.com/neulab/Reviewer2
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from ..schema import Review, ReviewDataset, DimensionReview

DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "reviewer2"

# URLs for Reviewer2 datasets
DATASETS: dict[str, str] = {
    "iclr_2023": "https://raw.githubusercontent.com/neulab/Reviewer2/main/data/iclr_2023.jsonl",
    "iclr_2022": "https://raw.githubusercontent.com/neulab/Reviewer2/main/data/iclr_2022.jsonl",
    "iclr_2021": "https://raw.githubusercontent.com/neulab/Reviewer2/main/data/iclr_2021.jsonl",
    "neurips_2022": "https://raw.githubusercontent.com/neulab/Reviewer2/main/data/neurips_2022.jsonl",
    "neurips_2021": "https://raw.githubusercontent.com/neulab/Reviewer2/main/data/neurips_2021.jsonl",
}

# Score normalization maps
RECOMMENDATION_MAP: dict[str, tuple[str, float | None]] = {
    "accept": ("accept", 8.0),
    "weak accept": ("weak-accept", 7.0),
    "weak accept:": ("weak-accept", 7.0),
    "borderline accept": ("weak-accept", 6.0),
    "borderline": ("borderline", 5.0),
    "borderline reject": ("weak-reject", 4.0),
    "weak reject": ("weak-reject", 4.0),
    "weak reject:": ("weak-reject", 4.0),
    "reject": ("reject", 2.0),
    "strong reject": ("reject", 1.0),
}


def _ensure_cache_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _dataset_path(dataset_name: str) -> Path:
    return _ensure_cache_dir() / f"{dataset_name}.jsonl"


def download_dataset(dataset_name: str) -> str:
    """Download Reviewer2 dataset JSONL if not cached.

    Returns the concatenated JSONL text.
    """
    if dataset_name not in DATASETS:
        raise ValueError(f"Unknown dataset '{dataset_name}'. Options: {', '.join(DATASETS.keys())}")

    local_path = _dataset_path(dataset_name)
    if local_path.exists():
        return local_path.read_text(encoding="utf-8")

    url = DATASETS[dataset_name]
    print(f"[reviewer2] Downloading {url}...")

    req = urllib.request.Request(url, headers={"User-Agent": "ClawAI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Failed to download Reviewer2 dataset '{dataset_name}': {e}") from e

    local_path.write_text(text, encoding="utf-8")
    return text


def _normalize_rating(
    rating: Any,
    recommendation: str = "",
) -> tuple[float | None, str]:
    """Parse and normalize rating to 0-10, return (rating, recommendation)."""
    # First try to parse numeric rating
    if rating is not None:
        try:
            parsed = float(rating)
            # Reviewer2 ratings are typically 1-10 or 1-5
            if parsed <= 5:
                parsed = parsed * 2
            return min(10.0, max(1.0, parsed)), recommendation
        except (ValueError, TypeError):
            pass

    # Fall back to recommendation-based score
    if recommendation:
        rec_lower = recommendation.strip().lower()
        if rec_lower in RECOMMENDATION_MAP:
            mapped_rec, mapped_score = RECOMMENDATION_MAP[rec_lower]
            if mapped_score is not None:
                return mapped_score, mapped_rec
            return None, mapped_rec

    return None, recommendation


def load_dataset(dataset_name: str, use_cache: bool = True) -> ReviewDataset:
    """Load a Reviewer2 dataset into normalized Review objects.

    Args:
        dataset_name: Key from DATASETS dict, e.g. "iclr_2023", "neurips_2022"
        use_cache: Use cached download

    Returns:
        ReviewDataset with normalized reviews
    """
    text = download_dataset(dataset_name)

    # Parse venue/year from name
    parts = dataset_name.split("_")
    venue = parts[0].upper() if parts else ""
    year = int(parts[1]) if len(parts) > 1 else 0

    reviews_list: list[Review] = []
    lines = text.strip().split("\n")

    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(entry, dict):
            continue

        paper_title = entry.get("paper_title", entry.get("title", ""))
        paper_abstract = entry.get("paper_abstract", entry.get("abstract", ""))
        keywords = entry.get("paper_keywords", entry.get("keywords", []))
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",") if k.strip()]

        review_text = entry.get("review_text", entry.get("review", ""))
        strengths_raw = entry.get("strengths", [])
        weaknesses_raw = entry.get("weaknesses", [])
        suggestions_raw = entry.get("suggestions", entry.get("suggestion", []))

        # Normalize to lists
        strengths = _normalize_list(strengths_raw)
        weaknesses = _normalize_list(weaknesses_raw)
        suggestions = _normalize_list(suggestions_raw)

        # Rating
        raw_rating = entry.get("rating", entry.get("score", None))
        raw_recommendation = entry.get("recommendation", entry.get("decision", ""))
        rating, recommendation = _normalize_rating(raw_rating, raw_recommendation)

        # Confidence (Reviewer2 has 1-5)
        confidence = None
        raw_conf = entry.get("confidence", None)
        try:
            if raw_conf is not None:
                confidence = float(raw_conf)
        except (ValueError, TypeError):
            pass

        review = Review(
            source="reviewer2",
            source_id=entry.get("id", entry.get("review_id", "")),
            paper_title=paper_title,
            paper_venue=venue,
            paper_year=year,
            paper_keywords=keywords,
            overall_score=rating,
            recommendation=recommendation,
            confidence=confidence,
            comment_to_author=review_text,
            strengths=strengths,
            weaknesses=weaknesses,
            suggestions=suggestions,
        )

        # If review has per-dimension scores (some entries)
        dimensions_raw = entry.get("dimensions", {})
        if dimensions_raw and isinstance(dimensions_raw, dict):
            for dim_id, dim_data in dimensions_raw.items():
                if isinstance(dim_data, dict):
                    dim_review = DimensionReview(
                        dimension_id=dim_id,
                        score=dim_data.get("score"),
                        summary=dim_data.get("summary", ""),
                    )
                    review.dimensions.append(dim_review)

        reviews_list.append(review)

    ds = ReviewDataset(
        name=dataset_name,
        source="reviewer2",
        reviews=reviews_list,
    )

    print(f"[reviewer2] Loaded {len(reviews_list)} reviews from {dataset_name}")
    return ds


def _normalize_list(raw: Any) -> list[str]:
    """Normalize a value to a list of strings."""
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if item and str(item).strip()]
    if isinstance(raw, str):
        # Split by newline or bullet
        items = re.split(r"[\n•\-]", raw)
        return [item.strip() for item in items if item.strip()]
    return []


def load_all(use_cache: bool = True) -> ReviewDataset:
    """Load ALL available Reviewer2 datasets into one combined dataset."""
    import re

    all_reviews: list[Review] = []
    for name in DATASETS:
        try:
            ds = load_dataset(name, use_cache=use_cache)
            all_reviews.extend(ds.reviews)
        except Exception as exc:
            print(f"[reviewer2] Skipping {name}: {exc}")

    return ReviewDataset(
        name="reviewer2_all",
        source="reviewer2",
        reviews=all_reviews,
    )
