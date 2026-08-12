"""PeerRead dataset loader.

PeerRead (Allen AI) provides parsed reviews from ICLR/NeurIPS/ACL/CVPR.
Data format: JSON with papers and their reviews.

Source: https://github.com/allenai/PeerRead
Each paper has:
  - paper.title, paper.abstract
  - reviews: list of parsed reviews
    - review.comments: full text
    - review.recommendation: "accept", "reject"
    - review.rating: numeric score
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from ..schema import Review, ReviewDataset

DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "peerread"
PEERREAD_REPO = "https://raw.githubusercontent.com/allenai/PeerRead/master"

# Available datasets: {name: relative_path_in_repo}
DATASETS = {
    "iclr_2017": "data/ICLR_2017/iclr_2017.json",
    "iclr_2018": "data/ICLR_2018/iclr_2018.json",
    "neurips_2018": "data/NIPS_2018/nips_2018.json",
    "acl_2018": "data/ACL_2018/acl_2018.json",
    "arxiv": "data/arxiv.cs.CL_2017/arxiv.cs.CL_2017.json",
    "dblp": "data/dblp.cs.CV_2018/dblp.cs.CV_2018.json",
}

RATING_SCALE: dict[str, float] = {
    "iclr_2017": 10.0,
    "iclr_2018": 10.0,
    "neurips_2018": 10.0,
    "acl_2018": 5.0,
    "arxiv": 5.0,
    "dblp": 5.0,
}

RECOMMENDATION_MAP = {
    "accept": "accept",
    "strong accept": "strong-accept",
    "weak accept": "weak-accept",
    "weak accept:": "weak-accept",
    "borderline accept": "weak-accept",
    "poster": "weak-accept",
    "oral": "accept",
    "reject": "reject",
    "strong reject": "reject",
    "weak reject": "weak-reject",
    "clear reject": "reject",
}


def _ensure_cache_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _dataset_path(dataset_name: str) -> Path:
    """Path to local cached JSON file."""
    return _ensure_cache_dir() / f"{dataset_name}.json"


def download_dataset(dataset_name: str) -> str:
    """Download PeerRead dataset JSON if not cached.

    Returns the JSON string.
    """
    if dataset_name not in DATASETS:
        raise ValueError(f"Unknown dataset '{dataset_name}'. Options: {', '.join(DATASETS.keys())}")

    local_path = _dataset_path(dataset_name)
    if local_path.exists():
        return local_path.read_text(encoding="utf-8")

    rel_path = DATASETS[dataset_name]
    url = f"{PEERREAD_REPO}/{rel_path}"
    print(f"[peerread] Downloading {url}...")

    req = urllib.request.Request(url, headers={"User-Agent": "ClawAI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Failed to download PeerRead dataset '{dataset_name}': {e}") from e

    local_path.write_text(text, encoding="utf-8")
    return text


def _parse_recommendation(raw: str) -> str:
    """Normalize recommendation string."""
    if not raw:
        return ""
    raw = raw.strip().lower()
    return RECOMMENDATION_MAP.get(raw, raw)


def _parse_rating(raw: Any, dataset_name: str) -> float | None:
    """Parse rating, normalizing to 0-10 scale."""
    if raw is None:
        return None
    try:
        rating = float(raw)
    except (ValueError, TypeError):
        return None

    scale = RATING_SCALE.get(dataset_name, 10.0)
    if scale < 10:
        rating = rating * (10.0 / scale)
    return min(10.0, max(1.0, rating))


def load_dataset(dataset_name: str, use_cache: bool = True) -> ReviewDataset:
    """Load a PeerRead dataset into normalized Review objects.

    Args:
        dataset_name: Key from DATASETS dict, e.g. "iclr_2017", "neurips_2018"
        use_cache: Use cached download

    Returns:
        ReviewDataset with normalized reviews
    """
    if dataset_name not in DATASETS:
        raise ValueError(f"Unknown dataset '{dataset_name}'. Options: {', '.join(DATASETS.keys())}")

    raw_text = download_dataset(dataset_name)
    papers = json.loads(raw_text)

    reviews_list: list[Review] = []
    venue_name = dataset_name.split("_")[0].upper()
    year = int(dataset_name.split("_")[1]) if "_" in dataset_name else 0

    for paper in papers:
        # Paper-level metadata
        paper_data = paper.get("paper", {}) if isinstance(paper, dict) else {}
        paper_title = paper_data.get("title", paper.get("title", ""))
        paper_abstract = paper_data.get("abstract", paper.get("abstract", ""))

        # Raw reviews
        raw_reviews = paper.get("reviews", []) if isinstance(paper, dict) else []

        for r in raw_reviews:
            if not isinstance(r, dict):
                continue

            review_text = r.get("comments", "") or r.get("review", "") or ""

            # Structured fields (some PeerRead datasets have these)
            strengths = r.get("strengths", []) or []
            if isinstance(strengths, str):
                strengths = [s.strip() for s in strengths.split("\n") if s.strip()]
            weaknesses = r.get("weaknesses", []) or []
            if isinstance(weaknesses, str):
                weaknesses = [s.strip() for s in weaknesses.split("\n") if s.strip()]

            # Recommendation & rating
            raw_rec = r.get("recommendation", "") or r.get("decision", "") or ""
            recommendation = _parse_recommendation(raw_rec)

            raw_rating = r.get("rating", r.get("score", None))
            rating = _parse_rating(raw_rating, dataset_name)

            # Confidence
            confidence = None
            raw_conf = r.get("confidence", None)
            try:
                if raw_conf is not None:
                    confidence = float(raw_conf)
            except (ValueError, TypeError):
                pass

            review = Review(
                source="peerread",
                source_id=paper.get("id", "") or paper.get("paper_id", ""),
                paper_title=paper_title,
                paper_venue=venue_name,
                paper_year=year,
                paper_keywords=[],
                overall_score=rating,
                recommendation=recommendation,
                confidence=confidence,
                comment_to_author=review_text,
                strengths=strengths if isinstance(strengths, list) else [],
                weaknesses=weaknesses if isinstance(weaknesses, list) else [],
            )
            reviews_list.append(review)

    ds = ReviewDataset(
        name=dataset_name,
        source="peerread",
        reviews=reviews_list,
    )

    print(f"[peerread] Loaded {len(reviews_list)} reviews from {dataset_name}")
    return ds


def load_all(use_cache: bool = True) -> ReviewDataset:
    """Load ALL available PeerRead datasets into one combined dataset."""
    all_reviews: list[Review] = []
    for name in DATASETS:
        try:
            ds = load_dataset(name, use_cache=use_cache)
            all_reviews.extend(ds.reviews)
        except Exception as exc:
            print(f"[peerread] Skipping {name}: {exc}")

    return ReviewDataset(
        name="peerread_all",
        source="peerread",
        reviews=all_reviews,
    )
